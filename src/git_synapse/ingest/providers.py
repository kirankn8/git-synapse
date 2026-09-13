"""Asking a host what it has."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from git_synapse.config import get_config
from git_synapse.ingest.github import GitHubClient, RepoRecord
from git_synapse.ingest.sources import Source

log = logging.getLogger(__name__)

PAGE = 100

LIST_CAP = 300


@dataclass
class Page:
    """One page of a listing, and whether the host says there is more."""

    records: list[RepoRecord]
    has_more: bool
    total: int | None = None


class Provider:
    """What every host must answer. Subclasses override what they can."""

    name = "git"

    def __init__(self, source: Source, patient: bool = True, token: str = "") -> None:
        """`patient` is whether a rate limit is waited out or reported."""
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
        """One page, for a picker that draws as it loads."""
        rows = self.list_repos(owner)
        start = (page - 1) * PAGE
        return Page(rows[start:start + PAGE], has_more=len(rows) > start + PAGE,
                    total=len(rows))

    def supports_listing(self) -> bool:
        return type(self).list_repos is not Provider.list_repos

    def fetch_parent(self, full_name: str) -> str:
        """The repository ``full_name`` was forked from, or "" if unknown."""
        return ""


class GitProvider(Provider):
    """No API. Everything is derived from the URL itself."""

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
            cfg = dataclasses.replace(cfg, token=token, token_file="")
        self._client = GitHubClient(cfg, patient=patient)

    def close(self) -> None:
        self._client.close()

    def _record(self, payload: dict[str, Any]) -> RepoRecord:
        import dataclasses

        return dataclasses.replace(RepoRecord.from_api(payload), host=self.source.host)

    def get_repo(self, owner: str, name: str) -> RepoRecord:
        return self._record(self._client._get(f"/repos/{owner}/{name}").json())

    def fetch_parent(self, full_name: str) -> str:
        return self._client.fetch_parent(full_name)

    def list_page(self, owner: str, page: int = 1) -> Page:
        """One page of an owner's repositories."""
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
            total=int(last.group(1)) * PAGE if last else (
                (page - 1) * PAGE + len(rows) if 'rel="next"' not in link else None),
        )

    def list_repos(self, owner: str) -> list[RepoRecord]:
        import dataclasses

        try:
            rows = self._client.list_account_repos(owner, kind="org")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            rows = self._client.list_account_repos(owner, kind="user")
        return [dataclasses.replace(r, host=self.source.host) for r in rows]


class GitLabProvider(Provider):
    """GitLab groups nest, so a project is addressed by its full path."""

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
            is_fork="forked_from_project" in p,
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
        """One page of a group's projects, subgroups included."""
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
