"""PostgreSQL connectivity: a shared pool, schema bootstrap, and COPY helpers.

Uses psycopg 3 directly rather than an ORM. The workload here is bulk ingest and
analytical aggregation -- both dominated by hand-written SQL and by COPY -- so an
ORM would add a mapping layer that every hot path then has to bypass.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from git_synapse.config import get_config

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        cfg = get_config().db
        _pool = ConnectionPool(
            conninfo=cfg.dsn,
            min_size=1,
            max_size=cfg.pool_size + cfg.pool_max_overflow,
            open=True,
            timeout=30.0,
            # Rotate connections so a long-lived one cannot hold a prepared
            # statement whose plan predates a schema change. Bounds the window
            # in which STALE_PLAN_SQLSTATES can occur at all; the retry below
            # handles the window itself.
            max_lifetime=1800.0,
            kwargs={"autocommit": False},
        )
        log.debug("opened connection pool to %s:%s/%s", cfg.host, cfg.port, cfg.database)
    return _pool


def close_pool() -> None:
    """Close the pool. Called on process shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Check out a connection; commit on clean exit, roll back on exception."""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def cursor(row_factory: Any = dict_row) -> Iterator[psycopg.Cursor]:
    """Check out a cursor returning dict rows by default."""
    with connection() as conn, conn.cursor(row_factory=row_factory) as cur:
        yield cur


#: SQLSTATEs raised when a cached prepared-statement plan no longer matches the
#: schema. psycopg auto-prepares a statement after a few executions, so an
#: ALTER TABLE while the pool holds idle connections makes those connections
#: fail with "cached plan must not change result type" until they are recycled.
#:
#: This is not hypothetical: adding `file.xrepo_change_count` for the cross-repo
#: feature broke `GET /api/files/{id}` on a running API. Retrying on a fresh
#: connection is the fix, because the error is a property of the connection, not
#: of the query.
STALE_PLAN_SQLSTATES = frozenset({"0A000", "26000"})


def _is_stale_plan(exc: BaseException) -> bool:
    """True if an exception is a stale prepared-statement plan."""
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in STALE_PLAN_SQLSTATES:
        return True
    text = str(exc).lower()
    return "cached plan must not change result type" in text


def _read(sql_text: str, params, mode: str, default=None):
    """Run a read, retrying once on a fresh connection if the plan went stale.

    Args:
        sql_text: the SQL to run.
        params: bound parameters.
        mode: ``all``, ``one`` or ``scalar``.
        default: value returned by ``scalar`` mode when there is no row.
    """
    for attempt in (1, 2):
        conn_ctx = get_pool().connection()
        conn = conn_ctx.__enter__()
        try:
            factory = dict_row if mode != "scalar" else None
            with conn.cursor(row_factory=factory) if factory else conn.cursor() as cur:
                cur.execute(sql_text, params)
                if mode == "all":
                    return cur.fetchall()
                row = cur.fetchone()
                if mode == "one":
                    return row
                return default if row is None else row[0]
        except Exception as exc:
            if attempt == 1 and _is_stale_plan(exc):
                log.warning("stale cached plan; discarding connection and retrying")
                # Closing it makes the pool drop rather than reuse it, so the
                # retry lands on a connection with no stale prepared statements.
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 - already failing
                    pass
                conn_ctx.__exit__(None, None, None)
                continue
            conn_ctx.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            conn_ctx.__exit__(None, None, None)
    raise RuntimeError("unreachable: read retry exhausted")


def query(sql_text: str, params: Sequence[Any] | dict[str, Any] | None = None) -> list[dict]:
    """Run a SELECT and return every row as a dict."""
    return _read(sql_text, params, "all")


def query_one(
    sql_text: str, params: Sequence[Any] | dict[str, Any] | None = None
) -> dict | None:
    """Run a SELECT and return the first row, or None."""
    return _read(sql_text, params, "one")


def scalar(
    sql_text: str, params: Sequence[Any] | dict[str, Any] | None = None, default: Any = None
) -> Any:
    """Run a SELECT and return the first column of the first row."""
    return _read(sql_text, params, "scalar", default)


def execute(sql_text: str, params: Sequence[Any] | dict[str, Any] | None = None) -> int:
    """Run a statement and return the affected row count."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.rowcount


def wait_for_database(timeout_s: float = 120.0, interval_s: float = 1.0) -> None:
    """Block until Postgres accepts connections.

    Compose starts the API and the database together, and a healthcheck alone
    does not guarantee the database is ready to serve, so every entrypoint waits
    here first.

    Raises:
        RuntimeError: if the database is still unreachable after ``timeout_s``.
    """
    cfg = get_config().db
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with psycopg.connect(cfg.dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            log.info("database reachable after %d attempt(s)", attempt)
            return
        except Exception as exc:  # noqa: BLE001 - any driver error means "not ready"
            last_error = exc
            time.sleep(interval_s)
    raise RuntimeError(
        f"database at {cfg.host}:{cfg.port} unreachable after {timeout_s:.0f}s: {last_error}"
    )


#: Bumped whenever ``schema.sql`` changes in a way that needs re-applying.
SCHEMA_VERSION = 11

#: How long a DDL statement waits for a lock before giving up. Short on purpose:
#: DDL queues ahead of ordinary queries in Postgres, so a schema apply that
#: blocks behind a long ingest would stall every subsequent reader too.
SCHEMA_LOCK_TIMEOUT_MS = 5000
SCHEMA_RETRIES = 5


def schema_is_current() -> bool:
    """True if the schema has already been applied at the current version.

    Read-only and cheap, so it can gate the DDL on every service boot. Returns
    False when the ``meta`` table does not exist yet, which is the first-run case.
    """
    try:
        with psycopg.connect(get_config().db.dsn, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        return False
    except Exception:  # noqa: BLE001 - treat any probe failure as "needs applying"
        return False
    return bool(row) and int(row[0]) >= SCHEMA_VERSION


def apply_schema(force: bool = False) -> None:
    """Create every table and index if it does not already exist.

    ``schema.sql`` is idempotent, so re-running it is harmless -- but not free:
    the DDL takes locks that can deadlock against a running ingest, and because
    Postgres queues DDL ahead of ordinary queries, a blocked schema apply stalls
    readers behind it. Two mitigations:

    * A cheap read of ``meta.schema_version`` short-circuits the whole thing on
      every boot after the first, so the common case takes no DDL locks at all.
    * When the DDL does run, ``lock_timeout`` makes it fail fast rather than
      block, and deadlock or timeout is retried with backoff.

    Args:
        force: apply the DDL even if the recorded version is already current.
    """
    if not force and schema_is_current():
        log.debug("schema already at version %d; skipping DDL", SCHEMA_VERSION)
        return

    ddl = resources.files("git_synapse.db").joinpath("schema.sql").read_text(encoding="utf-8")
    last: Exception | None = None
    for attempt in range(1, SCHEMA_RETRIES + 1):
        try:
            with psycopg.connect(get_config().db.dsn, connect_timeout=10) as conn:
                conn.execute(f"SET lock_timeout = '{SCHEMA_LOCK_TIMEOUT_MS}ms'")
                conn.execute(ddl)
                conn.commit()
            log.info("schema applied (version %d)", SCHEMA_VERSION)
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable) as exc:
            last = exc
            wait = min(2**attempt, 15)
            log.warning(
                "schema apply blocked by concurrent work (attempt %d/%d); retry in %ds",
                attempt, SCHEMA_RETRIES, wait,
            )
            time.sleep(wait)
        except psycopg.errors.QueryCanceled as exc:
            last = exc
            time.sleep(min(2**attempt, 15))

    raise RuntimeError(f"could not apply schema after {SCHEMA_RETRIES} attempts: {last}")


def get_watermark(key: str) -> str | None:
    """Read a derived-stage watermark from the ``meta`` table.

    Used by the globally-scoped derived stages -- lagged coupling, impact
    prediction -- which have no per-repository row to hang a timestamp on. Each
    records a fingerprint of its inputs, and skips entirely when that
    fingerprint has not moved.
    """
    row = query_one("SELECT value FROM meta WHERE key = %s", (f"watermark:{key}",))
    if row is None:
        return None
    value = row.get("value")
    return str(value) if value is not None else None


def set_watermark(key: str, value: str) -> None:
    """Record a derived-stage watermark."""
    import json as _json

    execute(
        """
        INSERT INTO meta (key, value, updated_at)
        VALUES (%s, %s::jsonb, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (f"watermark:{key}", _json.dumps(value)),
    )


def copy_rows(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    conn: psycopg.Connection | None = None,
) -> int:
    """Bulk-load rows with COPY, the fastest ingest path psycopg offers.

    Args:
        table: destination table name.
        columns: column names, matching the order of values in each row.
        rows: an iterable of row tuples. Consumed lazily, so a generator
            streaming millions of rows never has to be materialised.
        conn: reuse an existing connection/transaction; a new one is checked out
            when omitted.

    Returns:
        Number of rows written.
    """

    def _run(c: psycopg.Connection) -> int:
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(col) for col in columns),
        )
        written = 0
        with c.cursor() as cur, cur.copy(stmt) as copy:
            for row in rows:
                copy.write_row(row)
                written += 1
        return written

    if conn is not None:
        return _run(conn)
    with connection() as own:
        return _run(own)


def copy_into_temp(
    conn: psycopg.Connection,
    temp_table: str,
    column_defs: Sequence[tuple[str, str]],
    rows: Iterable[Sequence[Any]],
) -> int:
    """Create an unlogged temp table and COPY rows into it.

    This is the staging half of the standard "COPY then MERGE" pattern: bulk
    load into a scratch table, then a single set-based INSERT ... ON CONFLICT
    merges it into the real table. Far faster than row-by-row upserts, and it
    keeps the merge atomic.

    Args:
        conn: an open connection; the temp table lives for its session.
        temp_table: name for the scratch table.
        column_defs: ``(name, sql_type)`` pairs.
        rows: row tuples to load.

    Returns:
        Number of rows staged.
    """
    cols_ddl = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(typ)) for name, typ in column_defs
    )
    conn.execute(
        sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
            sql.Identifier(temp_table), cols_ddl
        )
    )
    return copy_rows(temp_table, [name for name, _ in column_defs], rows, conn=conn)
