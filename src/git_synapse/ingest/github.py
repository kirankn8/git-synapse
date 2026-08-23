"""Repository discovery: enumerate an org's repos and capture the full record.

Everything GitHub returns about a repository is persisted -- named columns for
the fields the UI filters and sorts on, plus the untouched JSON payload in
``repo.raw_github`` so that a field nobody thought to model today is still
available tomorrow without a re-crawl.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from git_synapse.config import GitHubConfig, get_config

log = logging.getLogger(__name__)

#: GitHub caps page size at 100 for the repos endpoint.
PAGE_SIZE = 100
#: Total attempts per request before giving up.
MAX_RETRIES = 5


@dataclass
class RepoRecord:
    """A repository as discovered from the GitHub API.

    Mirrors the named columns of the ``repo`` table. ``raw`` carries the
    complete API payload.
    """

    github_id: int
    owner: str
    name: str
    full_name: str
    description: str | None = None
    homepage: str | None = None
    html_url: str | None = None
    clone_url: str | None = None
    ssh_url: str | None = None
    default_branch: str | None = None
    primary_language: str | None = None
    languages: dict[str, int] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    license_spdx: str | None = None
    visibility: str | None = None
    is_private: bool = False
    is_fork: bool = False
    is_archived: bool = False
    is_template: bool = False
    is_disabled: bool = False
    disk_usage_kb: int | None = None
    stargazers: int = 0
    watchers: int = 0
    forks_count: int = 0
    open_issues: int = 0
    github_created_at: datetime | None = None
    github_updated_at: datetime | None = None
    github_pushed_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "RepoRecord":
        """Build a record from a GitHub REST repository object."""
        owner = (payload.get("owner") or {}).get("login") or ""
        license_obj = payload.get("license") or {}
        return cls(
            github_id=int(payload["id"]),
            owner=owner,
            name=payload["name"],
            full_name=payload.get("full_name") or f"{owner}/{payload['name']}",
            description=payload.get("description"),
            homepage=payload.get("homepage"),
            html_url=payload.get("html_url"),
            clone_url=payload.get("clone_url"),
            ssh_url=payload.get("ssh_url"),
            default_branch=payload.get("default_branch"),
            primary_language=payload.get("language"),
            topics=list(payload.get("topics") or []),
            license_spdx=license_obj.get("spdx_id"),
            visibility=payload.get("visibility"),
            is_private=bool(payload.get("private")),
            is_fork=bool(payload.get("fork")),
            is_archived=bool(payload.get("archived")),
            is_template=bool(payload.get("is_template")),
            is_disabled=bool(payload.get("disabled")),
            disk_usage_kb=payload.get("size"),
            stargazers=int(payload.get("stargazers_count") or 0),
            watchers=int(payload.get("subscribers_count") or payload.get("watchers_count") or 0),
            forks_count=int(payload.get("forks_count") or 0),
            open_issues=int(payload.get("open_issues_count") or 0),
            github_created_at=_parse_ts(payload.get("created_at")),
            github_updated_at=_parse_ts(payload.get("updated_at")),
            github_pushed_at=_parse_ts(payload.get("pushed_at")),
            raw=payload,
        )

    def authed_clone_url(self, token: str) -> str:
        """Clone URL with the token embedded, so private repos fetch without a prompt.

        The token never reaches disk: git is invoked with this URL in argv only
        for the initial clone, and the remote stored in the mirror is rewritten
        to the plain URL by :mod:`git_synapse.ingest.gitops`.
        """
        base = self.clone_url or f"https://github.com/{self.full_name}.git"
        if not token:
            return base
        return base.replace("https://", f"https://x-access-token:{token}@", 1)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp, tolerating the trailing Z."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("could not parse timestamp %r", value)
        return None


class GitHubClient:
    """Thin REST client with retry, rate-limit awareness and pagination."""

    def __init__(self, cfg: GitHubConfig | None = None, timeout: float = 30.0) -> None:
        self.cfg = cfg or get_config().github
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "git-synapse-change-coupling/1.0",
        }
        # current_token() rather than cfg.token: the credential lives in a file
        # the host rotates, and reading the frozen env copy sent unauthenticated
        # requests that quietly returned only the org's 59 public repositories
        # instead of all 272.
        token = self.cfg.current_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=self.cfg.api_url, headers=headers, timeout=timeout, follow_redirects=True
        )

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with retry on rate limits, 5xx and transport errors.

        Honours GitHub's ``Retry-After`` and ``x-ratelimit-reset`` headers rather
        than backing off blindly, so a secondary-rate-limit trip costs the
        minimum necessary wait.
        """
        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                wait = min(2**attempt, 30)
                log.warning("GET %s failed (%s); retry %d in %ds", path, exc, attempt, wait)
                time.sleep(wait)
                continue

            if response.status_code < 400:
                return response

            if response.status_code in (403, 429):
                wait = self._rate_limit_wait(response)
                log.warning(
                    "GET %s rate limited (%s); sleeping %ds",
                    path,
                    response.status_code,
                    wait,
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = min(2**attempt, 30)
                log.warning("GET %s returned %s; retry in %ds", path, response.status_code, wait)
                time.sleep(wait)
                continue

            response.raise_for_status()

        raise RuntimeError(f"GET {path} failed after {MAX_RETRIES} attempts: {last_error}")

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> int:
        """Seconds to wait before retrying a rate-limited request."""
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), 300)
        reset = response.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            delta = int(reset) - int(time.time())
            if 0 < delta <= 900:
                return delta + 1
        return 60

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Walk every page of a list endpoint and return the concatenated items."""
        out: list[dict[str, Any]] = []
        page = 1
        params = dict(params or {})
        params["per_page"] = PAGE_SIZE
        while True:
            params["page"] = page
            response = self._get(path, params)
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        return out

    def rate_limit(self) -> dict[str, Any]:
        """Current rate-limit budget, surfaced in the UI's status panel."""
        return self._get("/rate_limit").json()

    def list_org_repos(self, org: str | None = None) -> list[RepoRecord]:
        """List every repository in the org that the token can see."""
        org = org or self.cfg.org
        log.info("discovering repositories in org %s", org)
        payloads = self._paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "pushed"})
        records = [RepoRecord.from_api(p) for p in payloads]
        log.info("discovered %d repositories in %s", len(records), org)
        return records

    def fetch_languages(self, full_name: str) -> dict[str, int]:
        """Byte counts per language for one repo.

        A separate request per repository, so this is optional: the pipeline
        skips it when discovering hundreds of repos unless explicitly asked.
        """
        try:
            return self._get(f"/repos/{full_name}/languages").json()
        except Exception as exc:  # noqa: BLE001 - languages are a nice-to-have
            log.warning("could not fetch languages for %s: %s", full_name, exc)
            return {}


def select_repos(records: list[RepoRecord], cfg: GitHubConfig | None = None) -> list[RepoRecord]:
    """Apply the configured include/exclude filters to a discovered list.

    An explicit ``ONLY_REPOS`` allowlist overrides every other filter, which
    makes it easy to reproduce a single repo's ingest while debugging.
    """
    cfg = cfg or get_config().github

    if cfg.only_repos:
        wanted = {name.lower() for name in cfg.only_repos}
        selected = [r for r in records if r.name.lower() in wanted or r.full_name.lower() in wanted]
        log.info("ONLY_REPOS set: %d of %d repos selected", len(selected), len(records))
        return selected

    skip = {name.lower() for name in cfg.skip_repos}
    selected = []
    for record in records:
        if record.name.lower() in skip or record.full_name.lower() in skip:
            continue
        if record.is_disabled:
            continue
        if record.is_private and not cfg.include_private:
            continue
        if record.is_fork and not cfg.include_forks:
            continue
        if record.is_archived and not cfg.include_archived:
            continue
        selected.append(record)

    log.info("%d of %d repositories selected after filters", len(selected), len(records))
    return selected
