"""Settings a running deployment can change, stored in the database."""
from __future__ import annotations

import logging

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

SCHEDULES = ("refresh_cron", "discover_cron")

ACCESS = ("dashboard_auth", "mcp_auth")

WRITABLE = SCHEDULES + ACCESS


def _key(name: str) -> str:
    return f"setting:{name}"


def get(name: str) -> str | None:
    """The stored override for one setting, or None when unset."""
    with session_scope() as session:
        Meta = models().Meta
        row = session.query(Meta).filter_by(key=_key(name)).one_or_none()
    return str(row.value) if row is not None else None


def set(name: str, value: str) -> None:  # noqa: A001 - reads better than set_
    """Store an override. Callers validate; this only persists."""
    if name not in WRITABLE:
        raise ValueError(f"{name!r} is not a writable setting")
    with session_scope() as session:
        Meta = models().Meta
        row = session.query(Meta).filter_by(key=_key(name)).one_or_none()
        if row is None:
            session.add(Meta(key=_key(name), value=value))
        else:
            row.value = value


def clear(name: str) -> None:
    """Drop an override, so the environment value applies again."""
    with session_scope() as session:
        Meta = models().Meta
        row = session.query(Meta).filter_by(key=_key(name)).one_or_none()
        if row is not None:
            session.delete(row)


def effective(name: str, fallback: str) -> str:
    """The value in force: the stored override, else the environment's."""
    try:
        stored = get(name)
    except Exception:  # noqa: BLE001  # pragma: no cover - a read must not take the
        # scheduler down; the environment value is always a safe answer.
        log.warning("could not read setting %s; using the configured value", name)
        return fallback
    return stored or fallback
