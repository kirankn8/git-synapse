"""Settings a running deployment can change, stored in the database.

Environment variables configure a deployment; they cannot be changed from the
UI without a restart, and a restart is not something a reader should need in
order to slow a cron down. So the schedule follows the model accounts already
use: the environment supplies the seed, the database holds the live value, and
the database wins once something has written one.

Only settings that are genuinely operational live here. Anything that changes
what the numbers *mean* -- pair support, rename similarity, the measure set --
stays in the environment, where it is versioned with the deployment and cannot
be altered between two readings of the same table.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from git_synapse.db.orm import models, session_scope

log = logging.getLogger(__name__)

#: Settings the API is allowed to write, and how to validate each. Anything not
#: named here is rejected: an open key/value endpoint is an invitation to store
#: configuration nothing reads.
#: Schedules, which are cron expressions.
SCHEDULES = ("refresh_cron", "discover_cron")

#: Access policy, which is one of auth.ACCESS_MODES. Kept apart from the
#: schedules because the two are validated and reported differently, and a
#: single list had /api/settings describing "dashboard_auth" as a cron.
ACCESS = ("dashboard_auth", "mcp_auth")

WRITABLE = SCHEDULES + ACCESS


def _key(name: str) -> str:
    return f"setting:{name}"


def get(name: str) -> str | None:
    """The stored override for one setting, or None when unset."""
    with session_scope() as session:
        Meta = models().Meta
        value = session.scalar(select(Meta.value).where(Meta.key == _key(name)))
    return str(value) if value is not None else None


def set(name: str, value: str) -> None:  # noqa: A001 - reads better than set_
    """Store an override. Callers validate; this only persists."""
    if name not in WRITABLE:
        raise ValueError(f"{name!r} is not a writable setting")
    with session_scope() as session:
        Meta = models().Meta
        statement = pg_insert(Meta).values(key=_key(name), value=value)
        session.execute(statement.on_conflict_do_update(
            index_elements=[Meta.key],
            set_={"value": statement.excluded.value},
        ))


def clear(name: str) -> None:
    """Drop an override, so the environment value applies again."""
    with session_scope() as session:
        Meta = models().Meta
        session.execute(delete(Meta).where(Meta.key == _key(name)))


def effective(name: str, fallback: str) -> str:
    """The value in force: the stored override, else the environment's.

    Every read goes to the database rather than a cached copy. The schedule is
    read once a minute by one process, so the query costs nothing, and a cache
    here would mean a change in the UI taking effect at a time nobody could
    predict.
    """
    try:
        stored = get(name)
    except Exception:  # noqa: BLE001  # pragma: no cover - a read must not take the
        # scheduler down; the environment value is always a safe answer.
        log.warning("could not read setting %s; using the configured value", name)
        return fallback
    return stored or fallback
