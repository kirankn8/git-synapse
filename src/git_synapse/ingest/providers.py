"""Asking a host what it has.

Coupling needs nothing but a clone: the atomic fact is *this commit touched
this file*, and `git log` yields it identically wherever the repository came
from. Everything a provider API adds -- stars, languages, the fork and archived
flags, the list of repositories under an owner -- is convenience on top.

That ordering is the design. :class:`GitProvider` is the fallback and it needs
no API at all, so a self-hosted host nobody has written a client for still
ingests; a provider client, where one exists, only enriches what is already
sufficient. The alternative -- refuse what we cannot introspect -- would refuse
exactly the deployments this is most useful in.

Each client implements two questions:

* `get_repo(owner, name)` -- one repository, without listing anything else.
  This is what a pasted repository URL uses, and it is why adding
  `microsoft/vscode` costs one request rather than the 83 pages it takes to
  enumerate an organisation of 8,296.
* `list_repos(owner)` -- everything under an owner, for the picker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from git_synapse.config import get_config
from git_synapse.ingest.github import GitHubClient, RepoRecord
from git_synapse.ingest.sources import Source

log = logging.getLogger(__name__)

#: What a host returns in one request. Every one of them caps at 100 and
#: silently ignores a larger number -- asking GitHub for 500 returns 100 and a
#: "there is more" link -- so this is a fact about the hosts, not a choice.
PAGE = 100

#: The cap on a *single blocking call*. Nothing is lost past it: the caller
#: takes the next page, and the picker keeps asking in the background while the
#: reader is already looking at the first hundred. Fetching all 83 pages of an
#: 8,296-repository organisation before drawing anything would be twenty-five
#: seconds of blank screen.
LIST_CAP = 300


@dataclass
class Page:
    """One page of a listing, and whether the host says there is more.

    `total` is the owner's true repository count where the host will say --
    from a `rel="last"` link, a header, or a field. None means unknown, which
    must be rendered as unknown: stating the number fetched so far as the total
    is stating our own progress as a fact about somebody else's organisation.
    """

    records: list[RepoRecord]
    has_more: bool
    total: int | None = None


class Provider:
    """What every host must answer. Subclasses override what they can."""

    name = "git"

    def __init__(self, source: Source, patient: bool = True, token: str = "") -> None:
        """`patient` is whether a rate limit is waited out or reported.

        `token` is this source's own credential where it has one, overriding
        the deployment-wide credential in the environment. One organisation's
        read-only token has no business being the one used against another
        organisation's private repositories.

        False for anything a person is waiting on. An organisation of 8,296
        repositories takes 83 listings, which is enough to trip GitHub's
        secondary limit -- and a handler that answers that by sleeping a minute
        is indistinguishable from one that has hung.
        """
        self.source = source
        self.patient = patient
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        raise NotImplementedError

    def list_repos(self, owner: str) -> list[RepoRecord]:
        raise NotImplementedError

    def list_page(self, owner: str, page: int = 1) -> Page:
        """One page, for a picker that draws as it loads.

        The default walks `list_repos` and slices, which is correct for any
        client that cannot do better; the three real ones override it so that
        page five costs one request rather than five.
        """
        rows = self.list_repos(owner)
        start = (page - 1) * PAGE
        return Page(rows[start:start + PAGE], has_more=len(rows) > start + PAGE,
                    total=len(rows))

    def supports_listing(self) -> bool:
        return type(self).list_repos is not Provider.list_repos

    def fetch_parent(self, full_name: str) -> str:
        """The repository ``full_name`` was forked from, or "" if unknown.

        Answered from the listing by every host that puts it there, which is
        why the default is to add nothing: only GitHub withholds it and has to
        ask again.
        """
        return ""


class GitProvider(Provider):
    """No API. Everything is derived from the URL itself.

    The record is deliberately sparse rather than guessed: `is_fork` and
    `is_archived` default false because we do not know, and inventing a value
    that a filter then acts on would be worse than admitting ignorance. The
    ingest fills in what git can actually prove -- default branch, commit
    counts, dates -- once the mirror exists.
    """

    name = "git"

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        host = self.source.host
        return RepoRecord(
            github_id=None,
            owner=owner,
            name=name,
            full_name=f"{owner}/{name}",
            provider="git",
            host=host,
            html_url=f"https://{host}/{owner}/{name}",
            clone_url=f"https://{host}/{owner}/{name}.git",
            visibility="unknown",
        )


class GitHubProvider(Provider):
    name = "github"

    def __init__(self, source: Source, patient: bool = True, token: str = "") -> None:
        super().__init__(source, patient, token)
        import dataclasses

        self._kind: str | None = None
        cfg = get_config().providers.github
        if source.api_url and source.api_url != cfg.api_url:
            cfg = dataclasses.replace(cfg, api_url=source.api_url)
        if token:
            # `token_file` too: current_token() prefers the file, and leaving it
            # set would silently ignore the credential this source carries.
            cfg = dataclasses.replace(cfg, token=token, token_file="")
        self._client = GitHubClient(cfg, patient=patient)

    def close(self) -> None:
        self._client.close()

    def _record(self, payload: dict[str, Any]) -> RepoRecord:
        import dataclasses

        # from_api knows GitHub's payload but not which GitHub: an Enterprise
        # install answers the same shape on a different host.
        return dataclasses.replace(RepoRecord.from_api(payload), host=self.source.host)

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        return self._record(self._client._get(f"/repos/{owner}/{name}").json())

    def fetch_parent(self, full_name: str) -> str:
        return self._client.fetch_parent(full_name)

    def list_page(self, owner: str, page: int = 1) -> Page:
        """One page of an owner's repositories.

        Whether an owner is an organisation or a person is not knowable from a
        URL, so the org endpoint is tried and a 404 falls back -- as part of
        the real request rather than a probe before it. A probe would be an
        extra request on every single lookup, which on an anonymous GitHub is
        one of only sixty an hour.
        """
        import dataclasses
        import re as _re

        params = {"per_page": PAGE, "page": page, "type": "all", "sort": "pushed"}
        kinds = [self._kind] if self._kind else ["orgs", "users"]
        for i, kind in enumerate(kinds):
            try:
                response = self._client._get(f"/{kind}/{owner}/repos", params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404 or i == len(kinds) - 1:
                    raise
                continue
            self._kind = kind
            break

        rows = [dataclasses.replace(RepoRecord.from_api(p), host=self.source.host)
                for p in response.json()]
        link = response.headers.get("link", "")
        last = _re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
        return Page(
            rows,
            has_more='rel="next"' in link,
            # The last page number times the page size is an upper bound, not
            # the count; the final page is rarely full. Good enough to say
            # "about 8,300", which is what a progress line needs.
            total=int(last.group(1)) * PAGE if last else (
                (page - 1) * PAGE + len(rows) if 'rel="next"' not in link else None),
        )

    def list_repos(self, owner: str) -> list[RepoRecord]:
        # An owner may be an organisation or a person, and the caller pasting a
        # URL has no way to know which. Try the org endpoint and fall back,
        # rather than making that someone's problem to answer.
        import dataclasses

        try:
            rows = self._client.list_account_repos(owner, kind="org")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            rows = self._client.list_account_repos(owner, kind="user")
        return [dataclasses.replace(r, host=self.source.host) for r in rows]


class GitLabProvider(Provider):
    """GitLab groups nest, so a project is addressed by its full path.

    `owner` here may itself contain slashes (`gitlab-org/security`), which is
    why the path is URL-encoded whole rather than assembled from two segments.
    """

    name = "gitlab"

    def __init__(self, source: Source, patient: bool = True, token: str = "") -> None:
        super().__init__(source, patient, token)
        headers = {"User-Agent": "git-synapse-change-coupling/1.0"}
        secret = token or get_config().providers.for_host("gitlab").current_token()
        if secret:
            headers["PRIVATE-TOKEN"] = secret
        self._client = httpx.Client(base_url=source.api_url or "https://gitlab.com/api/v4",
                                    headers=headers, timeout=30.0, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def _record(self, p: dict[str, Any]) -> RepoRecord:
        ns = (p.get("namespace") or {}).get("full_path") or ""
        return RepoRecord(
            github_id=None,
            owner=ns,
            name=p.get("path") or "",
            full_name=p.get("path_with_namespace") or f"{ns}/{p.get('path')}",
            provider="gitlab",
            host=self.source.host,
            description=p.get("description"),
            html_url=p.get("web_url"),
            clone_url=p.get("http_url_to_repo"),
            ssh_url=p.get("ssh_url_to_repo"),
            default_branch=p.get("default_branch"),
            topics=list(p.get("topics") or []),
            visibility=p.get("visibility"),
            is_private=p.get("visibility") != "public",
            # GitLab calls it a fork relationship; the key is absent when there
            # is none, which is the only signal the list endpoint gives.
            is_fork="forked_from_project" in p,
            # GitLab names the parent in the listing itself, so a fork costs no
            # extra request here the way it does on GitHub.
            parent_full_name=(
                (p.get("forked_from_project") or {}).get("path_with_namespace") or ""
            ),
            is_archived=bool(p.get("archived")),
            stargazers=int(p.get("star_count") or 0),
            forks_count=int(p.get("forks_count") or 0),
            raw=p,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        from urllib.parse import quote

        encoded = quote(f"{owner}/{name}", safe="")
        return self._record(self._get(f"/projects/{encoded}").json())

    def list_page(self, owner: str, page: int = 1) -> Page:
        """One page of a group's projects, subgroups included.

        GitLab reports the totals in headers, and omits them once a set is
        large enough that counting it would be expensive -- so a missing header
        means unknown, not zero.
        """
        from urllib.parse import quote

        encoded = quote(owner, safe="")
        params = {"per_page": PAGE, "page": page, "include_subgroups": "true",
                  "order_by": "star_count", "sort": "desc"}
        try:
            response = self._get(f"/groups/{encoded}/projects", params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            # A user, not a group. Their projects have no subgroups to include.
            response = self._get(f"/users/{encoded}/projects",
                                 {"per_page": PAGE, "page": page})
        rows = [self._record(p) for p in response.json()]
        total = response.headers.get("x-total")
        return Page(
            rows,
            has_more=bool(response.headers.get("x-next-page")) or len(rows) == PAGE,
            total=int(total) if total and total.isdigit() else None,
        )

    def list_repos(self, owner: str) -> list[RepoRecord]:
        from urllib.parse import quote

        encoded = quote(owner, safe="")
        out: list[RepoRecord] = []
        for page in range(1, 20):
            try:
                payload = self._get(
                    f"/groups/{encoded}/projects",
                    {"per_page": 100, "page": page, "include_subgroups": "true",
                     "order_by": "star_count", "sort": "desc"},
                ).json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404 or page > 1:
                    raise
                payload = self._get(f"/users/{encoded}/projects",
                                    {"per_page": 100, "page": page}).json()
            if not payload:
                break
            out.extend(self._record(p) for p in payload)
            if len(payload) < 100 or len(out) >= LIST_CAP:
                break
        return out


class BitbucketProvider(Provider):
    """Bitbucket calls an owner a workspace and pages with an opaque `next`."""

    name = "bitbucket"

    def __init__(self, source: Source, patient: bool = True, token: str = "") -> None:
        super().__init__(source, patient, token)
        headers = {"User-Agent": "git-synapse-change-coupling/1.0"}
        bitbucket = get_config().providers.for_host("bitbucket")
        auth = None
        secret = token or bitbucket.current_token()
        # Basic auth needs both halves; a token alone cannot be presented.
        if bitbucket.user and secret:
            auth = (bitbucket.user, secret)
        self._client = httpx.Client(base_url=source.api_url or "https://api.bitbucket.org/2.0",
                                    headers=headers, auth=auth, timeout=30.0,
                                    follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def _record(self, p: dict[str, Any]) -> RepoRecord:
        full = p.get("full_name") or ""
        owner, _, name = full.partition("/")
        clone = ""
        for link in (p.get("links") or {}).get("clone") or []:
            if link.get("name") == "https":
                clone = link.get("href") or ""
        return RepoRecord(
            github_id=None,
            owner=owner,
            name=name or p.get("slug") or "",
            full_name=full,
            provider="bitbucket",
            host=self.source.host,
            description=p.get("description"),
            html_url=((p.get("links") or {}).get("html") or {}).get("href"),
            clone_url=clone,
            default_branch=(p.get("mainbranch") or {}).get("name"),
            primary_language=p.get("language") or None,
            visibility="private" if p.get("is_private") else "public",
            is_private=bool(p.get("is_private")),
            disk_usage_kb=int((p.get("size") or 0) / 1024) or None,
            raw=p,
        )

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        response = self._client.get(f"/repositories/{owner}/{name}")
        response.raise_for_status()
        return self._record(response.json())

    def list_page(self, owner: str, page: int = 1) -> Page:
        """Bitbucket pages by number and reports the total in `size`."""
        response = self._client.get(f"/repositories/{owner}",
                                    params={"pagelen": PAGE, "page": page})
        response.raise_for_status()
        payload = response.json()
        rows = [self._record(p) for p in payload.get("values") or []]
        size = payload.get("size")
        return Page(rows, has_more=bool(payload.get("next")),
                    total=int(size) if isinstance(size, int) else None)

    def list_repos(self, owner: str) -> list[RepoRecord]:
        out: list[RepoRecord] = []
        url: str | None = f"/repositories/{owner}?pagelen=100"
        while url and len(out) < LIST_CAP:
            response = self._client.get(url)
            response.raise_for_status()
            payload = response.json()
            out.extend(self._record(p) for p in payload.get("values") or [])
            url = payload.get("next")
        return out


_BY_NAME: dict[str, type[Provider]] = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "bitbucket": BitbucketProvider,
    "git": GitProvider,
}


def for_source(source: Source, patient: bool = True, token: str = "") -> Provider:
    """The client for wherever this came from, falling back to plain git."""
    if not source.has_api:
        return GitProvider(source, patient, token)
    return _BY_NAME.get(source.provider, GitProvider)(source, patient, token)
