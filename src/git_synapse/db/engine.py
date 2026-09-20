"""SQLAlchemy-only database lifecycle helpers."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import DBAPIError, OperationalError

from git_synapse.config import get_config
from git_synapse.db.orm import get_engine, models, session_scope

log = logging.getLogger(__name__)

SCHEMA_VERSION = 34
SCHEMA_RETRIES = 5


def close_pool() -> None:
    """Dispose the process-wide SQLAlchemy engine."""
    from git_synapse.db.orm import close

    close()


def _is_schema_retryable(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (OperationalError, DBAPIError)):
            message = str(current).lower()
            if any(word in message for word in ("deadlock", "lock timeout", "could not obtain lock")):
                return True
        current = current.__cause__ or current.__context__
    return False


def wait_for_database(timeout_s: float = 120.0, interval_s: float = 1.0) -> None:
    """Block until the configured database accepts an ORM session."""
    cfg = get_config().db
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with session_scope() as session:
                session.connection()
            log.info("database reachable after %d attempt(s)", attempt)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(interval_s)
    raise RuntimeError(
        f"database at {cfg.host}:{cfg.port} unreachable after {timeout_s:.0f}s: {last_error}"
    )


def recorded_schema_version() -> int | None:
    """Return the recorded schema version, or ``None`` before bootstrap."""
    Meta = models().Meta
    try:
        with session_scope() as session:
            row = session.query(Meta).filter_by(key="schema_version").one_or_none()
            value = row.value if row is not None else None
        return int(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def schema_drift() -> int:
    """Return how many versions the database is ahead of this process."""
    recorded = recorded_schema_version()
    return max(0, recorded - SCHEMA_VERSION) if recorded is not None else 0


def _ensure_extensions() -> None:
    """Install the extensions the schema's indexes are declared against.

    schema.py declares three GIN trigram indexes, which need gin_trgm_ops from
    pg_trgm. Nothing created it, so the schema only applied to a database where
    someone had installed it by hand; a fresh one failed on the first index.
    """
    from sqlalchemy import text

    wanted = ("pg_trgm", "btree_gin")
    try:
        with get_engine().begin() as conn:
            # CREATE EXTENSION takes an exclusive lock even when it changes
            # nothing, so two processes applying the schema at once block on each
            # other. Reading the catalogue first keeps the usual case lock-free.
            have = {row[0] for row in conn.execute(
                text("select extname from pg_extension"))}
            for extension in (e for e in wanted if e not in have):
                # Managed Postgres may forbid this to an unprivileged role. If the
                # extension is already there the indexes still build; if it is not,
                # create_all fails next with a clearer message than this one.
                conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not ensure extension %s: %s", "/".join(wanted), exc)


def apply_schema(force: bool = False) -> None:
    """Create the canonical ORM metadata and record its version."""
    recorded = recorded_schema_version()
    if recorded is not None and recorded > SCHEMA_VERSION:
        log.error(
            "process expects schema version %d but database is at %d; rebuild all services",
            SCHEMA_VERSION,
            recorded,
        )
    if not force and recorded is not None and recorded >= SCHEMA_VERSION:
        return

    from git_synapse.db.schema import metadata

    last: Exception | None = None
    for attempt in range(1, SCHEMA_RETRIES + 1):
        try:
            _ensure_extensions()
            metadata.create_all(get_engine(), checkfirst=True)
            Meta = models().Meta
            with session_scope() as session:
                row = session.get(Meta, "schema_version")
                if row is None:
                    session.add(Meta(key="schema_version", value=SCHEMA_VERSION))
                else:
                    row.value = SCHEMA_VERSION
                    row.updated_at = datetime.now(UTC)
            log.info("schema applied (version %d)", SCHEMA_VERSION)
            return
        except Exception as exc:
            if not _is_schema_retryable(exc):
                raise
            last = exc
            wait = min(2**attempt, 15)
            log.warning(
                "schema apply blocked by concurrent work (attempt %d/%d); retry in %ds",
                attempt,
                SCHEMA_RETRIES,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"could not apply schema after {SCHEMA_RETRIES} attempts: {last}")


def get_watermark(key: str) -> str | None:
    """Read a derived-stage watermark through the ORM."""
    Meta = models().Meta
    with session_scope() as session:
        row = session.query(Meta).filter_by(key=f"watermark:{key}").one_or_none()
        value = row.value if row is not None else None
    return str(value) if value is not None else None


def set_watermark(key: str, value: str, session: Any = None) -> None:
    """Write a watermark in the caller's transaction when a session is given."""
    Meta = models().Meta

    def write(target: Any) -> None:
        row = target.get(Meta, f"watermark:{key}")
        if row is None:
            target.add(Meta(key=f"watermark:{key}", value=value))
        else:
            row.value = value
            row.updated_at = datetime.now(UTC)

    if session is not None:
        write(session)
    else:
        with session_scope() as target:
            write(target)
