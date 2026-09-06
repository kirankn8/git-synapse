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
from typing import Any

import httpx

from git_synapse.config import get_config
from git_synapse.ingest.github import GitHubClient, RepoRecord
from git_synapse.ingest.sources import Source

log = logging.getLogger(__name__)

#: A picker is a list a person reads, and nobody reads past a few hundred. The
#: cap keeps a paste of a 8,296-repository organisation from becoming ninety
#: sequential requests before anything appears on screen; the ordering below
#: makes the cut the *least* interesting repositories rather than an arbitrary
#: page boundary, and "track everything" needs no list at all.
LIST_CAP = 300


class ProviderError(RuntimeError):
    """The host refused, or answered something we cannot read."""


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

    def supports_listing(self) -> bool:
        return type(self).list_repos is not Provider.list_repos


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

        cfg = get_config().github
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
        secret = token or get_config().providers.gitlab_token
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
        cfg = get_config().providers
        auth = None
        secret = token or cfg.bitbucket_token
        if cfg.bitbucket_user and secret:
            auth = (cfg.bitbucket_user, secret)
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
