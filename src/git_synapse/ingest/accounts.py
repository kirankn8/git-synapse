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
from typing import Any

from git_synapse.config import GitHubConfig, get_config
from git_synapse.db.engine import query, query_one

log = logging.getLogger(__name__)

#: GitHub logins: alphanumeric with single hyphens, 39 characters at most.
#: Validated here so a typo fails at the API boundary with a clear message
#: rather than as a puzzling 404 from discovery an hour later.
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

KINDS = ("org", "user")

#: Columns a caller may set. Anything else is bookkeeping this module owns.
_WRITABLE = (
    "login",
    "kind",
    "api_url",
    "enabled",
    "include_private",
    "include_forks",
    "include_archived",
    "only_repos",
    "skip_repos",
)

_COLUMNS = """
    a.id, a.login, a.kind, a.api_url, a.enabled,
    a.include_private, a.include_forks, a.include_archived,
    a.only_repos, a.skip_repos,
    a.last_discovered_at, a.last_discover_error, a.repo_count,
    a.created_at, a.updated_at
"""


class AccountError(ValueError):
    """A rejected account definition. Carries a message fit to show a user."""


def validate_login(login: str) -> str:
    """Return the trimmed login, or raise if GitHub could not own that name."""
    value = (login or "").strip()
    if not _LOGIN.match(value):
        raise AccountError(
            f"{login!r} is not a valid GitHub login: letters, digits and "
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


def find_by_login(login: str) -> dict | None:
    """One account by login, matched case-insensitively as GitHub does."""
    return query_one(
        f"SELECT {_COLUMNS} FROM account a WHERE lower(a.login) = lower(%s)",
        (login,),
    )


def add_account(login: str, kind: str = "org", **fields: Any) -> dict:
    """Create an account, or raise :class:`AccountError` if it already exists."""
    login = validate_login(login)
    kind = validate_kind(kind)
    if find_by_login(login) is not None:
        raise AccountError(f"{login} is already configured")

    row = query_one(
        f"""
        INSERT INTO account (login, kind, api_url, enabled,
                             include_private, include_forks, include_archived,
                             only_repos, skip_repos)
        VALUES (%(login)s, %(kind)s, %(api_url)s, %(enabled)s,
                %(include_private)s, %(include_forks)s, %(include_archived)s,
                %(only_repos)s, %(skip_repos)s)
        RETURNING {_COLUMNS.replace("a.", "")}
        """,
        {
            "login": login,
            "kind": kind,
            "api_url": (fields.get("api_url") or "").strip() or None,
            "enabled": bool(fields.get("enabled", True)),
            "include_private": bool(fields.get("include_private", True)),
            "include_forks": bool(fields.get("include_forks", True)),
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
            existing = find_by_login(value)
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
