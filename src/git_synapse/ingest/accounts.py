"""The orgs and users whose repositories get discovered.

Onboarding an account is a database write, not a redeploy. Each account carries
its own include/exclude filters because the reason to skip forks in one org
rarely applies to the next, and the environment variables that used to carry
them globally are seeded here once and then ignored.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from typing import Any

from git_synapse.config import GitHubConfig, get_config
from git_synapse.db.engine import execute, query, query_one

log = logging.getLogger(__name__)

#: GitHub logins: alphanumeric with single hyphens, 39 characters at most.
#: Validated here so a typo fails at the API boundary with a clear message
#: rather than as a puzzling 404 from discovery an hour later.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

#: How a source is enumerated. 'org' and 'user' are GitHub's two endpoints;
#: 'group' and 'workspace' are GitLab's and Bitbucket's names for the same
#: thing; 'repo' enumerates nothing and holds exactly the repositories named in
#: its allowlist.
KINDS = ("org", "user", "group", "workspace", "repo")

#: Columns a caller may set. Anything else is bookkeeping this module owns.
_WRITABLE = (
    "login",
    "kind",
    "provider",
    "host",
    "api_url",
    "enabled",
    "include_private",
    "include_forks",
    "include_archived",
    "only_repos",
    "skip_repos",
)

_COLUMNS = """
    a.id, a.login, a.kind, a.provider, a.host, a.api_url, a.enabled,
    a.credential_hint, (a.credential IS NOT NULL) AS has_credential,
    a.include_private, a.include_forks, a.include_archived,
    a.only_repos, a.skip_repos,
    a.last_discovered_at, a.last_discover_error, a.repo_count,
    a.created_at, a.updated_at
"""


class AccountError(ValueError):
    """A rejected account definition. Carries a message fit to show a user."""


def validate_login(login: str) -> str:
    """Return the trimmed login, or raise if no host could own that name.

    GitLab groups nest -- `gitlab-org/security` is one owner -- so a slash is
    allowed and each segment is checked on its own.
    """
    value = (login or "").strip().strip("/")
    if value and "/" in value:
        for segment in value.split("/"):
            validate_login(segment)
        return value
    if not _LOGIN.match(value):
        raise AccountError(
            f"{login!r} is not a valid login: letters, digits and "
            "single hyphens only, up to 39 characters"
        )
    return value


def validate_kind(kind: str) -> str:
    """Return the kind, or raise if it is not one this system can list."""
    value = (kind or "org").strip().lower()
    if value not in KINDS:
        raise AccountError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    return value


def _names(values: Any) -> list[str]:
    """Normalise a repo allow/deny list from JSON, CSV or None to a clean list."""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    return [str(v).strip() for v in values if str(v).strip()]


def list_accounts(enabled_only: bool = False) -> list[dict]:
    """Every configured account, newest last, with its live repository count."""
    where = "WHERE a.enabled" if enabled_only else ""
    return query(
        f"""
        SELECT {_COLUMNS},
               (SELECT count(*) FROM repo r WHERE r.account_id = a.id) AS live_repo_count
          FROM account a
          {where}
      ORDER BY a.login
        """
    )


def get_account(account_id: int) -> dict | None:
    """One account by id, or None."""
    return query_one(
        f"""
        SELECT {_COLUMNS},
               (SELECT count(*) FROM repo r WHERE r.account_id = a.id) AS live_repo_count
          FROM account a
         WHERE a.id = %s
        """,
        (account_id,),
    )


def find_by_login(login: str, host: str | None = None) -> dict | None:
    """One source by login on a host, matched case-insensitively.

    The host is part of the lookup because the same login exists on more than
    one: an internal GitLab group commonly carries the company's GitHub org
    name, and they are two different places.
    """
    if host is None:
        return query_one(
            f"SELECT {_COLUMNS} FROM account a WHERE lower(a.login) = lower(%s)",
            (login,),
        )
    return query_one(
        f"SELECT {_COLUMNS} FROM account a"
        " WHERE lower(a.login) = lower(%s) AND lower(a.host) = lower(%s)",
        (login, host),
    )


def add_account(login: str, kind: str = "org", **fields: Any) -> dict:
    """Create an account, or raise :class:`AccountError` if it already exists."""
    login = validate_login(login)
    kind = validate_kind(kind)
    host = (fields.get("host") or "github.com").strip().lower()
    if find_by_login(login, host) is not None:
        raise AccountError(f"{login} is already configured on {host}")

    row = query_one(
        f"""
        INSERT INTO account (login, kind, provider, host, api_url, enabled,
                             include_private, include_forks, include_archived,
                             only_repos, skip_repos)
        VALUES (%(login)s, %(kind)s, %(provider)s, %(host)s, %(api_url)s, %(enabled)s,
                %(include_private)s, %(include_forks)s, %(include_archived)s,
                %(only_repos)s, %(skip_repos)s)
        RETURNING {_COLUMNS.replace("a.", "")}
        """,
        {
            "login": login,
            "kind": kind,
            "provider": (fields.get("provider") or "github").strip().lower(),
            "host": (fields.get("host") or "github.com").strip().lower(),
            "api_url": (fields.get("api_url") or "").strip() or None,
            "enabled": bool(fields.get("enabled", True)),
            "include_private": bool(fields.get("include_private", True)),
            # Off unless asked for. A fork's history is its parent's history,
            # so tracking both files the same commits twice and puts a second
            # copy of every coupling in the corpus.
            "include_forks": bool(fields.get("include_forks", False)),
            "include_archived": bool(fields.get("include_archived", True)),
            "only_repos": _names(fields.get("only_repos")),
            "skip_repos": _names(fields.get("skip_repos")),
        },
    )
    log.info("account added: %s (%s)", login, kind)
    return row


def update_account(account_id: int, **fields: Any) -> dict:
    """Patch the given columns of one account and return the updated row."""
    sets: list[str] = []
    params: dict[str, Any] = {"id": account_id}
    for key in _WRITABLE:
        if key not in fields:
            continue
        value = fields[key]
        if key == "login":
            value = validate_login(value)
            current = get_account(account_id) or {}
            existing = find_by_login(value, fields.get("host") or current.get("host"))
            if existing is not None and existing["id"] != account_id:
                raise AccountError(f"{value} is already configured")
        elif key == "kind":
            value = validate_kind(value)
        elif key in ("only_repos", "skip_repos"):
            value = _names(value)
        elif key == "api_url":
            value = (value or "").strip() or None
        elif key.startswith("include_") or key == "enabled":
            value = bool(value)
        sets.append(f"{key} = %({key})s")
        params[key] = value

    if not sets:
        row = get_account(account_id)
        if row is None:
            raise AccountError(f"account {account_id} not found")
        return row

    row = query_one(
        f"""
        UPDATE account SET {", ".join(sets)}, updated_at = now()
         WHERE id = %(id)s
     RETURNING {_COLUMNS.replace("a.", "")}
        """,
        params,
    )
    if row is None:
        raise AccountError(f"account {account_id} not found")
    return row


def remove_account(account_id: int) -> bool:
    """Delete an account. Its repositories and their history are kept.

    The mined statistics are the expensive part and remain valid whether or not
    the account that discovered them is still listed, so the foreign key clears
    rather than cascades. Repositories simply stop being refreshed.
    """
    row = query_one("DELETE FROM account WHERE id = %s RETURNING id", (account_id,))
    if row is not None:
        log.info("account removed: %d", account_id)
    return row is not None


def record_discovery(account_id: int, repo_count: int, error: str | None = None) -> None:
    """Stamp the outcome of a discovery pass so the UI can show what happened."""
    query_one(
        """
        UPDATE account
           SET last_discovered_at = now(), repo_count = %s,
               last_discover_error = %s, updated_at = now()
         WHERE id = %s
     RETURNING id
        """,
        (repo_count, error, account_id),
    )


def config_for(account: dict) -> GitHubConfig:
    """The global GitHub config with this account's overrides applied.

    Returned as a :class:`GitHubConfig` so the existing filter and client code
    works unchanged whether it is driven by an account row or the environment.
    """
    cfg = get_config().github
    return dataclasses.replace(
        cfg,
        org=account["login"],
        api_url=account.get("api_url") or cfg.api_url,
        include_private=account["include_private"],
        include_forks=account["include_forks"],
        include_archived=account["include_archived"],
        only_repos=tuple(account.get("only_repos") or ()),
        skip_repos=tuple(account.get("skip_repos") or ()),
    )


def seed_from_env() -> dict | None:
    """Adopt GITHUB_ORG as an account, once, if nothing is configured.

    The environment is a seed for the account table, not a source discovery
    reads: a deployment can be brought up with one organisation already listed,
    and everything after that is a write to the table.
    """
    if list_accounts():
        return None
    cfg = get_config().github
    if not (cfg.org or "").strip():
        return None
    try:
        account = add_account(
            cfg.org,
            kind="org",
            include_private=cfg.include_private,
            include_forks=cfg.include_forks,
            include_archived=cfg.include_archived,
            only_repos=list(cfg.only_repos),
            skip_repos=list(cfg.skip_repos),
        )
    except AccountError as exc:
        log.warning("could not seed account from GITHUB_ORG=%s: %s", cfg.org, exc)
        return None
    log.info("seeded account %s from GITHUB_ORG", cfg.org)
    return account


def _is_rate_limited(exc: Exception) -> bool:
    """Whether a host refused because we asked too much, too fast."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (403, 429)


#: What each host wants before it will admit a private repository exists.
_CREDENTIAL_FOR = {
    "github": "GITHUB_TOKEN, with `repo` scope",
    "gitlab": "GITLAB_TOKEN, with `read_api`",
    "bitbucket": "BITBUCKET_USER and BITBUCKET_TOKEN",
}


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (401, 404)


def _not_found_message(source: Any) -> str:
    """A 404 on a private repository is indistinguishable from one that does
    not exist -- deliberately, so an outsider cannot enumerate what is there.

    Which means we cannot tell the reader which it is, and must not guess. Both
    possibilities, and the one thing that separates them, is the whole answer.
    """
    from git_synapse.config import get_config

    tokens = {
        "github": get_config().github.current_token(),
        "gitlab": get_config().providers.gitlab_token,
        "bitbucket": get_config().providers.bitbucket_token,
    }
    want = _CREDENTIAL_FOR.get(source.provider)
    if want and not tokens.get(source.provider):
        return (
            f"{source.host} says there is nothing at {source.full_name}. That "
            "means it does not exist, or it is private — a host answers both "
            "the same way so that outsiders cannot discover what is there. "
            f"This deployment has no credential for {source.host}, so if it is "
            f"private we cannot see it: set {want} and try again."
        )
    return (
        f"{source.host} says there is nothing at {source.full_name}. Check the "
        "spelling — and if it is private, that the configured credential can "
        "actually see it."
    )


def _budget_note(source: Any) -> str:
    """How much budget is left and when it comes back, where the host says.

    GitHub's `/rate_limit` is itself exempt from the rate limit, so asking is
    free -- and "resets in 12 minutes" is something a reader can act on, while
    "rate limited" leaves them guessing whether to wait a minute or an hour.
    """
    if source.provider != "github":
        return ""
    try:
        from git_synapse.ingest.github import GitHubClient

        with GitHubClient(patient=False) as client:
            core = client.rate_limit()["resources"]["core"]
        remaining, limit, reset = core["remaining"], core["limit"], core["reset"]
    except Exception:  # noqa: BLE001 - a nicety; never worth failing over
        return ""

    minutes = max(0, round((reset - time.time()) / 60))
    when = "in under a minute" if minutes < 1 else f"in about {minutes} minutes"
    return f" {remaining} of {limit} requests left; the budget refills {when}."


def _rate_limit_message(source: Any) -> str:
    """Why the host refused, in terms of the thing the reader can change.

    "Rate limited" alone sends someone to wait it out. Anonymous GitHub allows
    sixty requests an hour, which one listing of a large organisation can
    exhaust, and the fix there is a token rather than patience -- so say which
    of the two situations this is, and when waiting would actually work.
    """
    from git_synapse.config import get_config

    if source.provider == "github" and not get_config().github.current_token():
        return (
            "GitHub allows 60 requests an hour without a token, and this "
            "deployment has none — one listing of a large organisation spends "
            "that. Set GITHUB_TOKEN to raise it to 5,000."
            + _budget_note(source)
        )
    if source.provider == "gitlab" and not get_config().providers.gitlab_token:
        return (f"{source.host} is rate-limiting us. Set GITLAB_TOKEN to raise "
                "the budget.")
    return (
        f"{source.host} is rate-limiting us, which a very large owner will do — "
        "listing thousands of repositories takes hundreds of requests."
        + _budget_note(source)
    )


def set_credential(account_id: int, token: str | None) -> dict | None:
    """Store or clear one source's access token.

    The plaintext is never written and never read back out of here: callers get
    the hint, which is enough to recognise a token and useless for anything
    else.
    """
    from git_synapse import vault

    if token:
        execute("UPDATE account SET credential = %s, credential_hint = %s,"
                " updated_at = now() WHERE id = %s",
                (vault.seal(token.strip()), vault.hint(token), account_id))
    else:
        execute("UPDATE account SET credential = NULL, credential_hint = NULL,"
                " updated_at = now() WHERE id = %s", (account_id,))
    return get_account(account_id)


def credential_for(account: dict | None) -> str:
    """The token to use for this source: its own, else the environment's.

    A source that carries one overrides the deployment-wide credential, which
    is the point -- one organisation's read-only token has no business being
    the one used against another's private repositories.
    """
    from git_synapse import vault

    if not account:
        return ""
    return vault.open_(account.get("credential"))


#: Owner listings, kept briefly so that looking the same one up twice does not
#: spend the budget twice. The window is short because it exists to cover one
#: person's back-and-forth -- paste, look, adjust, look again -- not to serve
#: stale data: on an anonymous GitHub, three glances at an organisation is
#: three of the sixty requests available that hour.
#:
#: Only listings are cached. A single repository costs one request, which is
#: cheap enough that a stale answer would be the worse trade.
_LISTINGS: dict[tuple[str, str], tuple[float, dict]] = {}
LISTING_TTL_SECONDS = 300


def _cached_listing(key: tuple[str, str]) -> dict | None:
    hit = _LISTINGS.get(key)
    if hit is None:
        return None
    at, payload = hit
    if time.time() - at > LISTING_TTL_SECONDS:
        del _LISTINGS[key]
        return None
    return payload


def resolve_url(url: str, limit: int = 300, token: str = "", page: int = 1) -> dict:
    """Work out what a pasted URL is, and what could be tracked from it.

    This is what makes adding something one field instead of six. The caller
    hands over whatever was in their clipboard and gets back either

    * ``kind="repo"`` -- a single repository, already fetched, ready to confirm;
      or
    * ``kind="owner"`` -- an owner and the repositories under it, for a person
      to choose from.

    A repository URL costs one request. It deliberately does *not* enumerate
    the owner: pasting one repository from an organisation of 8,296 is a
    question about one repository, and answering it by paging through
    eighty-three listings would be the slowest possible way to say yes.

    An owner comes back **one page at a time**. Every host caps a listing at a
    hundred, so 8,296 repositories is 83 requests and roughly twenty-five
    seconds -- which as a single blocking call is twenty-five seconds of blank
    screen. The caller draws the first hundred immediately and asks for the
    next while the reader is already reading.
    """
    from git_synapse.ingest import providers, sources

    source = sources.parse(url)
    existing = find_by_login(source.owner, source.host)
    # A token the caller is holding but has not committed to yet: the point of
    # the lookup is to find out whether it works before anything is written.
    secret = token.strip() or credential_for(_with_credential(existing))

    def _key(record: Any) -> str:
        """What to put in the allowlist, and what the picker selects on.

        The repository's path *relative to the owner*, which is its `name`
        everywhere except GitLab, where groups nest: `gitlab-org` contains both
        `gitlab-org/gitlab-runner` and `gitlab-org/ci-cd/gitlab-runner`, two
        different projects with the same name. Keying on the name alone ticks
        both boxes for one choice and puts one entry in the allowlist that then
        matches both.
        """
        prefix = f"{source.owner}/"
        full = record.full_name or f"{record.owner}/{record.name}"
        return full[len(prefix):] if full.startswith(prefix) else record.name

    def _repo(record: Any) -> dict:
        return {
            "name": record.name,
            "key": _key(record),
            "full_name": record.full_name,
            "description": record.description,
            "language": record.primary_language,
            "stars": record.stargazers,
            "is_fork": record.is_fork,
            "is_archived": record.is_archived,
            "is_private": record.is_private,
            "size_kb": record.disk_usage_kb,
            "html_url": record.html_url,
            # What the picker should tick on arrival. Forks and archived
            # repositories are offered but not pre-selected: a fork's history is
            # its parent's, and an archive cannot change again, so both are
            # usually noise -- but "usually" is not "never", which is why they
            # are shown at all rather than filtered away.
            "suggested": not (record.is_fork or record.is_archived),
            "already_tracked": bool(
                existing and _key(record) in (existing.get("only_repos") or [])
            ),
        }

    base = {
        "url": url,
        "provider": source.provider,
        "host": source.host,
        "owner": source.owner,
        "web_url": source.web_url,
        "has_api": source.has_api,
        "existing_account_id": existing["id"] if existing else None,
        # An owner already tracked wholesale has nothing to choose: everything
        # under it is in scope, including repositories created tomorrow.
        "tracks_everything": bool(existing and not (existing.get("only_repos") or [])),
    }

    if source.is_repo:
        try:
            with providers.for_source(source, patient=False, token=secret) as client:
                record = client.get_repo(source.owner, source.repo)
        except Exception as exc:
            if _is_rate_limited(exc):
                raise AccountError(_rate_limit_message(source)) from exc
            if _is_missing(exc):
                raise AccountError(_not_found_message(source)) from exc
            raise
        return {**base, "kind": "repo", "repos": [_repo(record)], "total": 1,
                "truncated": False}

    # A token changes what is visible, so it must not read another caller's
    # anonymous answer.
    cache_key = (source.host, source.owner.lower(), page) if not secret else None
    if cache_key is not None:
        cached = _cached_listing(cache_key)
        if cached is not None:
            return {**cached, **base, "cached": True}

    with providers.for_source(source, patient=False, token=secret) as client:
        if not client.supports_listing():
            raise AccountError(
                f"{source.host} has no API we can list, so paste the URL of a "
                "single repository instead — it will still be cloned and "
                "analysed in full.")
        try:
            found = client.list_page(source.owner, page)
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            # Not a dead end. Choosing from a list is one way to answer this
            # question; tracking the whole owner is the other, and that needs
            # no list at all -- so say what happened and offer the door that
            # is still open rather than refusing outright.
            log.info("could not list %s: %s", source.owner, exc)
            return {
                **base, "kind": "owner", "repos": [], "total": None,
                "truncated": False,
                "listing_error": _rate_limit_message(source),
            }

    records = sorted(found.records, key=lambda r: (-(r.stargazers or 0), r.name.lower()))
    payload = {
        **base,
        "kind": "owner",
        "repos": [_repo(r) for r in records],
        # What the owner actually has, where the host says so. Unknown stays
        # unknown: reporting how many we have fetched as the total would state
        # our own progress as a fact about somebody else's organisation.
        "total": found.total,
        "page": page,
        "has_more": found.has_more,
        "cached": False,
    }
    if cache_key is not None:
        _LISTINGS[cache_key] = (time.time(), payload)
    return payload


def _with_credential(account: dict | None) -> dict | None:
    """Re-read the row including its ciphertext, which `_COLUMNS` omits."""
    if not account:
        return None
    return query_one("SELECT credential FROM account WHERE id = %s", (account["id"],))


def add_from_url(url: str, repos: list[str] | None = None,
                 token: str = "", **fields: Any) -> dict:
    """Track something a person pasted.

    ``repos`` names what to track; an empty list means everything under the
    owner, now and in future, which is the one case an allowlist cannot
    express. Adding to a source that already exists extends its allowlist
    rather than failing, because pasting a second repository from the same
    owner is obviously an addition and not a mistake.
    """
    from git_synapse.ingest import sources

    source = sources.parse(url)
    wanted = _names(repos if repos is not None else
                    ([source.repo] if source.is_repo else []))
    stored = token.strip()

    kind = "repo" if wanted else ("user" if source.provider == "github" else "group")
    existing = find_by_login(source.owner, source.host)
    if existing is not None:
        if stored:
            set_credential(existing["id"], stored)
        current = list(existing.get("only_repos") or [])
        if not current:
            # Already tracking the whole owner; naming a subset would narrow it.
            return get_account(existing["id"]) or existing
        merged = current + [r for r in wanted if r not in current]
        return update_account(existing["id"],
                              only_repos=[] if not wanted else merged,
                              kind=existing["kind"] if wanted else "org")

    row = add_account(
        source.owner,
        kind=kind if wanted else ("org" if source.provider == "github" else kind),
        provider=source.provider,
        host=source.host,
        api_url=fields.get("api_url") or source.api_url,
        only_repos=wanted,
        # Filters only bite when nothing was named. An allowlist is already an
        # explicit answer -- `_discover_account` fetches those by name and
        # never runs them through `select_repos` -- so these describe the
        # whole-owner case alone: no forks, because a fork's history is its
        # parent's, and everything else the credential can see.
        include_forks=False,
        include_archived=True,
        include_private=True,
        skip_repos=[],
    )
    if stored:
        return set_credential(row["id"], stored) or row
    return row
