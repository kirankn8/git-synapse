"""The orgs and users whose repositories get discovered."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from git_synapse.config import SelectionConfig, get_config
from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

# GitHub login: alphanumeric with single hyphens, at most 39 characters.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

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

class AccountError(ValueError):
    """A rejected account definition. Carries a message fit to show a user."""


_PUBLIC_FIELDS = (
    "id", "login", "kind", "provider", "host", "api_url", "enabled",
    "credential_hint", "include_private", "include_forks", "include_archived",
    "only_repos", "skip_repos", "last_discovered_at", "last_discover_error",
    "repo_count", "created_at", "updated_at",
)


def _account_dict(account: Any, session: Any) -> dict:
    """Serialize one mapped account without exposing its sealed credential."""
    Repo = models().Repo
    values = {name: getattr(account, name) for name in _PUBLIC_FIELDS}
    values["has_credential"] = account.credential is not None
    values["live_repo_count"] = session.query(Repo).filter_by(
        account_id=account.id, is_enabled=True,
    ).count()
    return values


def validate_login(login: str) -> str:
    """Return the trimmed login, or raise if no host could own that name."""
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
    with session_scope() as session:
        Account = models().Account
        query = session.query(Account)
        if enabled_only:
            query = query.filter_by(enabled=True)
        return [_account_dict(row, session) for row in query.order_by(Account.login).all()]


def get_account(account_id: int) -> dict | None:
    """One account by id, or None."""
    with session_scope() as session:
        row = session.get(models().Account, account_id)
        return _account_dict(row, session) if row else None


def find_by_login(login: str, host: str | None = None) -> dict | None:
    """One source by login on a host, matched case-insensitively."""
    with session_scope() as session:
        Account = models().Account
        for row in session.query(Account).all():
            if row.login.lower() == login.lower() and (
                host is None or row.host.lower() == host.lower()
            ):
                return _account_dict(row, session)
        return None


def add_account(login: str, kind: str = "org", **fields: Any) -> dict:
    """Create an account, or raise :class:`AccountError` if it already exists."""
    login = validate_login(login)
    kind = validate_kind(kind)
    host = (fields.get("host") or "github.com").strip().lower()
    if find_by_login(login, host) is not None:
        raise AccountError(f"{login} is already configured on {host}")

    with session_scope() as session:
        Account = models().Account
        account = Account(
            login=login,
            kind=kind,
            provider=(fields.get("provider") or "github").strip().lower(),
            host=host,
            api_url=(fields.get("api_url") or "").strip() or None,
            enabled=bool(fields.get("enabled", True)),
            include_private=bool(fields.get("include_private", True)),
            include_forks=bool(fields.get("include_forks", False)),
            include_archived=bool(fields.get("include_archived", True)),
            only_repos=_names(fields.get("only_repos")),
            skip_repos=_names(fields.get("skip_repos")),
        )
        session.add(account)
        session.flush()
        account_id = account.id
    log.info("account added: %s (%s)", login, kind)
    return get_account(account_id)  # type: ignore[return-value]


def update_account(account_id: int, **fields: Any) -> dict:
    """Patch the given columns of one account and return the updated row."""
    sets: dict[str, Any] = {}
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
        sets[key] = value

    if not sets:
        row = get_account(account_id)
        if row is None:
            raise AccountError(f"account {account_id} not found")
        return row

    with session_scope() as session:
        Account = models().Account
        account = session.get(Account, account_id)
        if account is None:
            raise AccountError(f"account {account_id} not found")
        for key, value in sets.items():
            setattr(account, key, value)
        account.updated_at = datetime.now(UTC)
    return get_account(account_id)  # type: ignore[return-value]


def remove_account(account_id: int) -> bool:
    """Delete an account. Its repositories and their history are kept."""
    with session_scope() as session:
        row = session.get(models().Account, account_id)
        if row is None:
            return False
        session.delete(row)
    if row is not None:
        log.info("account removed: %d", account_id)
    return row is not None


def record_discovery(account_id: int, repo_count: int | None = None,
                     error: str | None = None) -> None:
    """Stamp the outcome of a discovery pass so the UI can show what happened."""
    with session_scope() as session:
        row = session.get(models().Account, account_id)
        if row is None:
            return
        row.last_discovered_at = datetime.now(UTC)
        row.last_discover_error = error
        if repo_count is not None:
            row.repo_count = repo_count
        row.updated_at = datetime.now(UTC)
    if repo_count is None:
        return


def refresh_repo_counts() -> None:
    """Set every source's count from the repositories that actually exist."""
    with session_scope() as session:
        Account, Repo = models().Account, models().Repo
        for account in session.query(Account).all():
            account.repo_count = session.query(Repo).filter_by(
                account_id=account.id, is_enabled=True,
            ).count()
            account.updated_at = datetime.now(UTC)


def config_for(account: dict) -> SelectionConfig:
    """What this account takes, as `select_repos` wants it."""
    return SelectionConfig(
        include_private=account["include_private"],
        include_forks=account["include_forks"],
        include_archived=account["include_archived"],
        only_repos=tuple(account["only_repos"]),
        skip_repos=tuple(account["skip_repos"]),
    )


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
    """A 404 on a private repository is indistinguishable from one that does not exist -- deliberately, so an outsider cannot enumerate what is there."""

    want = _CREDENTIAL_FOR.get(source.provider)
    if want and not get_config().providers.token_for(source.provider):
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
    """How much budget is left and when it comes back, where the host says."""
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
    """Why the host refused, in terms of the thing the reader can change."""

    if source.provider == "github" and not get_config().providers.token_for("github"):
        return (
            "GitHub allows 60 requests an hour without a token, and this "
            "deployment has none — one listing of a large organisation spends "
            "that. Set GITHUB_TOKEN to raise it to 5,000."
            + _budget_note(source)
        )
    if source.provider == "gitlab" and not get_config().providers.token_for("gitlab"):
        return (f"{source.host} is rate-limiting us. Set GITLAB_TOKEN to raise "
                "the budget.")
    return (
        f"{source.host} is rate-limiting us, which a very large owner will do — "
        "listing thousands of repositories takes hundreds of requests."
        + _budget_note(source)
    )


def set_credential(account_id: int, token: str | None) -> dict | None:
    """Store or clear one source's access token."""
    from git_synapse import vault

    with session_scope() as session:
        account = session.get(models().Account, account_id)
        if account is not None:
            account.credential = vault.seal(token.strip()) if token else None
            account.credential_hint = vault.hint(token) if token else None
            account.updated_at = datetime.now(UTC)
    return get_account(account_id)


def credential_for(account: dict | None) -> str:
    """The token to use for this source: its own, else the environment's."""
    from git_synapse import vault

    if not account:
        return ""
    return vault.open_(account.get("credential"))


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
    """Work out what a pasted URL is, and what could be tracked from it."""
    from git_synapse.ingest import providers, sources

    source = sources.parse(url)
    existing = find_by_login(source.owner, source.host)
    secret = token.strip() or credential_for(_with_credential(existing))

    def _key(record: Any) -> str:
        """What to put in the allowlist, and what the picker selects on."""
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
    with session_scope() as session:
        row = session.get(models().Account, account["id"])
        return {"credential": row.credential} if row else None


def add_from_url(url: str, repos: list[str] | None = None,
                 token: str = "", **fields: Any) -> dict:
    """Track something a person pasted."""
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
        include_forks=False,
        include_archived=True,
        include_private=True,
        skip_repos=[],
    )
    if stored:
        return set_credential(row["id"], stored) or row
    return row
