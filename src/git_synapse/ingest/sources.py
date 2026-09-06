"""What a pasted URL means.

Adding something to scan should be one field. A person has a URL in their
clipboard -- the page they were just looking at -- and asking them to decompose
it into a provider, a login, a kind and an allowlist is asking them to do work
the string already contains.

So this turns any of these into a :class:`Source`::

    https://github.com/microsoft/vscode        one repository
    https://github.com/microsoft               every repository under an owner
    git@gitlab.com:gitlab-org/gitlab.git       ssh form, one repository
    https://bitbucket.org/team/repo/src/main/  a deep link, trimmed back
    https://git.internal.corp/team/svc.git     a host we have no API for

The last one matters. Cloning needs no API, so a host nobody has written a
client for is still perfectly ingestible -- it simply arrives with less
metadata. Refusing it would be refusing the case the abstraction exists for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Hosts we can ask questions of, and the API each answers on. A host that is
#: not here is not rejected; it is cloned, which is all that coupling needs.
KNOWN_HOSTS: dict[str, tuple[str, str]] = {
    "github.com": ("github", "https://api.github.com"),
    "gitlab.com": ("gitlab", "https://gitlab.com/api/v4"),
    "bitbucket.org": ("bitbucket", "https://api.bitbucket.org/2.0"),
}

#: Path segments a provider puts *after* the repository, which a person pasting
#: from their browser will bring along. Everything from one of these onward is
#: not part of the repository's address.
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
    """What a URL resolved to.

    `repo` is None for an owner, which is the difference that decides whether
    the caller is offered a list to choose from or a single repository to
    confirm.
    """

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
        """Whether an owner can be enumerated and metadata fetched.

        False is a normal state, not a degraded one: the repository still
        mirrors, parses and scores identically.
        """
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
    """Resolve a pasted string to a repository or an owner.

    Accepts the browser URL, the clone URL, the ssh remote, and a bare
    ``owner/repo`` on the assumption of GitHub -- which is how people write it
    in prose, and refusing it would be pedantry.
    """
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

    # GitLab nests groups arbitrarily deep (gitlab-org/security/gitlab), so the
    # repository is the last segment and everything before it is the owner.
    # Everywhere else an owner is one segment and a repository is the second.
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
        # Always the https form: the mirror is read-only and anonymous unless a
        # token is configured, and an ssh remote would need a key in the
        # container that nothing else here requires.
        clone_url=f"{web}.git" if repo else "",
    )
