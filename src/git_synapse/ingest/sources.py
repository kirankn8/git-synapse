"""What a pasted URL means."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

KNOWN_HOSTS: dict[str, tuple[str, str]] = {
    "github.com": ("github", "https://api.github.com"),
    "gitlab.com": ("gitlab", "https://gitlab.com/api/v4"),
    "bitbucket.org": ("bitbucket", "https://api.bitbucket.org/2.0"),
}

_TRAILING = {
    # GitHub / gitea
    "tree", "blob", "commits", "commit", "pulls", "pull", "issues", "actions",
    "releases", "tags", "branches", "settings", "wiki", "compare", "graphs",
    # GitLab
    "-", "merge_requests",
    # Bitbucket
    "src", "branch", "pull-requests", "downloads",
}

_SSH = re.compile(r"^(?:(?P<user>[\w.-]+)@)?(?P<host>[\w.-]+):(?P<path>[\w./~-]+?)/?$")


class SourceError(ValueError):
    """The string is not something we can turn into a repository or an owner."""


@dataclass(frozen=True)
class Source:
    """What a URL resolved to."""

    provider: str
    host: str
    owner: str
    repo: str | None
    api_url: str | None
    web_url: str
    clone_url: str

    @property
    def is_repo(self) -> bool:
        return self.repo is not None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}" if self.repo else self.owner

    @property
    def has_api(self) -> bool:
        """Whether an owner can be enumerated and metadata fetched."""
        return self.api_url is not None


def _clean(segment: str) -> str:
    return segment.removesuffix(".git").strip()


def _split_path(path: str) -> list[str]:
    parts = [p for p in path.strip("/").split("/") if p]
    # Trim a browser deep link back to the repository it is inside.
    for i, part in enumerate(parts):
        if part in _TRAILING and i >= 2:
            return parts[:i]
    return parts


def parse(raw: str) -> Source:
    """Resolve a pasted string to a repository or an owner."""
    text = (raw or "").strip()
    if not text:
        raise SourceError("paste a repository or organisation URL")

    scheme_url = text
    ssh = _SSH.match(text)
    if ssh and "//" not in text:
        # git@host:owner/repo.git -- not a URL, so urlsplit cannot read it.
        scheme_url = f"https://{ssh.group('host')}/{ssh.group('path')}"
    elif "://" not in text:
        bare = text.strip("/")
        if not re.fullmatch(r"[\w.-]+(/[\w.-]+){0,1}", bare):
            raise SourceError(f"{raw!r} is not a URL or an owner/repo name")
        scheme_url = f"https://github.com/{bare}"

    parts_url = urlsplit(scheme_url)
    host = (parts_url.hostname or "").lower()
    if not host:
        raise SourceError(f"{raw!r} has no host")

    parts = _split_path(parts_url.path)
    if not parts:
        raise SourceError(
            f"{raw!r} names a host but no organisation or repository")

    provider, api_url = KNOWN_HOSTS.get(host, ("git", None))

    if provider == "gitlab" and len(parts) > 2:
        owner, repo = "/".join(parts[:-1]), _clean(parts[-1])
    elif len(parts) >= 2:
        owner, repo = parts[0], _clean(parts[1])
    else:
        owner, repo = parts[0], None

    base = f"https://{host}"
    if parts_url.port:
        base = f"https://{host}:{parts_url.port}"
    web = f"{base}/{owner}/{repo}" if repo else f"{base}/{owner}"
    return Source(
        provider=provider,
        host=host,
        owner=owner,
        repo=repo,
        api_url=api_url,
        web_url=web,
        clone_url=f"{web}.git" if repo else "",
    )
