"""The database layer everything else sits on.

Its failure modes are the ones that look like something else: a stale prepared
plan surfacing as "could not load this view", a schema apply deadlocking against
a running ingest, a watermark that commits separately from the work it describes.
"""
from __future__ import annotations

import psycopg
import pytest

from git_synapse.db import engine


# ------------------------------------------------------------- basic reads

def test_query_query_one_and_scalar_agree(db):
    rows = engine.query("SELECT 1 AS a, 2 AS b")
    assert rows == [{"a": 1, "b": 2}]
    assert engine.query_one("SELECT 1 AS a")["a"] == 1
    assert engine.scalar("SELECT 42") == 42


def test_query_one_returns_none_rather_than_raising_on_no_rows(db):
    assert engine.query_one("SELECT 1 WHERE false") is None


def test_scalar_returns_its_default_when_there_is_no_row(db):
    assert engine.scalar("SELECT 1 WHERE false", default=-1) == -1


def test_parameters_are_bound_not_interpolated(db):
    """A value containing SQL must be data, never syntax."""
    evil = "'; DROP TABLE repo; --"
    assert engine.query_one("SELECT %s::text AS v", (evil,))["v"] == evil
    assert engine.scalar("SELECT count(*) FROM repo") >= 0  # table still there


def test_execute_reports_the_rows_it_touched(db):
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("CREATE TEMP TABLE probe_exec (n int) ON COMMIT DROP")
        conn.execute("INSERT INTO probe_exec VALUES (1),(2),(3)")
        assert conn.execute("UPDATE probe_exec SET n = n + 1").rowcount == 3


# ------------------------------------------------------------- watermarks

def test_a_watermark_round_trips_and_is_absent_until_set(db):
    key = "probe-watermark"
    from git_synapse.db.engine import connection

    try:
        assert engine.get_watermark(key) is None
        engine.set_watermark(key, "abc123")
        assert engine.get_watermark(key) == "abc123"
        engine.set_watermark(key, "def456")
        assert engine.get_watermark(key) == "def456"
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM meta WHERE key = %s", (f"watermark:{key}",))


def test_a_watermark_written_on_the_caller_connection_rolls_back_with_it(db):
    """It used to commit independently, so a rebuild that later failed left the
    watermark advanced and the next run skipped the work entirely."""
    import psycopg

    from git_synapse.db.engine import connection

    key = "probe-txn-watermark"
    try:
        with pytest.raises(psycopg.errors.DivisionByZero):
            with connection() as conn:
                engine.set_watermark(key, "should-not-survive", conn=conn)
                conn.execute("SELECT 1/0")
        assert engine.get_watermark(key) is None
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM meta WHERE key = %s", (f"watermark:{key}",))


# ------------------------------------------------------------ copy and DDL

def test_copy_rows_bulk_loads(db):
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("CREATE TEMP TABLE probe_copy (a int, b text) ON COMMIT DROP")
        n = engine.copy_rows("probe_copy", ["a", "b"],
                             [(i, f"r{i}") for i in range(50)], conn=conn)
        assert n == 50
        assert conn.execute("SELECT count(*) FROM probe_copy").fetchone()[0] == 50


def test_copy_rows_with_nothing_to_load(db):
    from git_synapse.db.engine import connection

    with connection() as conn:
        conn.execute("CREATE TEMP TABLE probe_empty (a int) ON COMMIT DROP")
        assert engine.copy_rows("probe_empty", ["a"], [], conn=conn) == 0


def test_apply_schema_is_idempotent(db):
    engine.apply_schema()
    engine.apply_schema()
    assert engine.schema_is_current()


def test_wait_for_database_returns_promptly_when_it_is_up(db):
    engine.wait_for_database(timeout_s=5, interval_s=0.2)


def test_reserve_ids_hands_out_a_disjoint_block(db):
    from git_synapse.db.engine import connection
    from git_synapse.ingest.store import reserve_ids

    with connection() as conn:
        first = reserve_ids(conn, "file_id_seq", 10)
        second = reserve_ids(conn, "file_id_seq", 10)
    assert min(second) > max(first), "two reservations must not overlap"
    assert len(set(first)) == 10


def test_the_schema_version_matches_between_the_code_and_the_sql():
    """The version lives in two places and drifted twice.

    `schema_is_current()` compares the constant against what schema.sql wrote.
    When they disagree it is permanently false, so every service boot re-runs the
    full DDL and takes the locks the fast path exists to avoid -- which is how a
    schema apply deadlocked against a running ingest before.
    """
    import re
    from importlib import resources

    sql = resources.files("git_synapse.db").joinpath("schema.sql").read_text(encoding="utf-8")
    m = re.search(r"'schema_version',\s*'(\d+)'::jsonb", sql)
    assert m, "schema.sql no longer records a version"
    assert int(m.group(1)) == engine.SCHEMA_VERSION, (
        f"schema.sql writes {m.group(1)} but SCHEMA_VERSION is {engine.SCHEMA_VERSION}"
    )


# ------------------------------------------------------- failure handling

def test_a_stale_prepared_plan_is_retried_on_a_fresh_connection(db, monkeypatch):
    """After a live ALTER TABLE, a pooled connection can hold a cached plan for
    the old shape. That surfaced to a user as "Could not load this view"; the
    fix is to discard the connection and retry once, not to propagate."""
    import psycopg

    calls = {"n": 0}
    real_execute = psycopg.Cursor.execute

    def flaky(self, query, params=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.FeatureNotSupported(
                "cached plan must not change result type"
            )
        return real_execute(self, query, params, **kw)

    monkeypatch.setattr(psycopg.Cursor, "execute", flaky)
    assert engine.query_one("SELECT 1 AS a")["a"] == 1
    assert calls["n"] >= 2, "the stale plan must have been retried"


def test_an_error_that_is_not_a_stale_plan_propagates(db, monkeypatch):
    """Retrying an ordinary error would hide it and double the work."""
    import psycopg

    def always_fail(self, query, params=None, **kw):
        raise psycopg.errors.UndefinedColumn("column nope does not exist")

    monkeypatch.setattr(psycopg.Cursor, "execute", always_fail)
    with pytest.raises(psycopg.errors.UndefinedColumn):
        engine.query("SELECT nope FROM repo")


def test_apply_schema_gives_up_with_a_clear_error_rather_than_looping(db, monkeypatch):
    """DDL takes locks that can queue behind a running ingest; it must fail
    fast and say so instead of blocking readers indefinitely."""
    import psycopg

    monkeypatch.setattr(engine, "schema_is_current", lambda: False)
    monkeypatch.setattr(engine.time, "sleep", lambda _s: None, raising=False)

    def blocked(*a, **kw):
        raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(psycopg, "connect", blocked)
    with pytest.raises(RuntimeError, match="could not apply schema"):
        engine.apply_schema(force=True)


def test_wait_for_database_gives_up_rather_than_hanging(monkeypatch):
    """A container that waits forever on a database that will never come is
    indistinguishable from one that is working."""
    import psycopg

    monkeypatch.setattr(engine.time, "sleep", lambda _s: None, raising=False)

    def refuse(*a, **kw):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(Exception):
        engine.wait_for_database(timeout_s=0.3, interval_s=0.1)


def test_close_pool_is_safe_to_call_twice(db):
    engine.close_pool()
    engine.close_pool()
    # The pool must rebuild itself on the next use.
    assert engine.scalar("SELECT 1") == 1


# ---------------------------------------------- the read path's failure modes

def test_a_stale_cached_plan_is_retried_on_a_fresh_connection(monkeypatch):
    """A schema change invalidates prepared statements on pooled connections.
    Without this retry every long-lived reader -- the API, the MCP server -- would
    start erroring after a migration until it was restarted."""
    from git_synapse.db import engine

    calls = {"n": 0}
    real_pool = engine.get_pool()

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.errors.FeatureNotSupported(
                    "cached plan must not change result type"
                )

        def fetchone(self):
            return {"x": 1}

        def fetchall(self):
            return [{"x": 1}]

    closed = []

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def close(self):
            closed.append(True)

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    class _Pool:
        def connection(self):
            return _Ctx()

    monkeypatch.setattr(engine, "get_pool", lambda: _Pool())
    assert engine.query_one("SELECT 1 AS x") == {"x": 1}
    assert calls["n"] == 2, "the query was not retried"
    assert closed, "the poisoned connection was returned to the pool"

    monkeypatch.setattr(engine, "get_pool", lambda: real_pool)


def test_a_stale_plan_on_the_retry_is_raised_rather_than_looping(monkeypatch):
    """Two attempts, not an unbounded loop: if the second fresh connection still
    sees a stale plan the cause is not the cache and retrying will not fix it."""
    from git_synapse.db import engine

    calls = {"n": 0}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            calls["n"] += 1
            raise psycopg.errors.FeatureNotSupported(
                "cached plan must not change result type"
            )

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def close(self):
            pass

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    class _Pool:
        def connection(self):
            return _Ctx()

    monkeypatch.setattr(engine, "get_pool", lambda: _Pool())
    with pytest.raises(psycopg.errors.FeatureNotSupported):
        engine.query_one("SELECT 1")
    assert calls["n"] == 2


def test_an_ordinary_query_error_is_not_retried(monkeypatch):
    """Retrying a genuine SQL error doubles the cost of every mistake."""
    from git_synapse.db import engine

    calls = {"n": 0}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            calls["n"] += 1
            raise psycopg.errors.UndefinedColumn("column nope does not exist")

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "get_pool", lambda: type("P", (), {
        "connection": lambda self: _Ctx()})())
    with pytest.raises(psycopg.errors.UndefinedColumn):
        engine.query("SELECT nope")
    assert calls["n"] == 1


def test_the_cursor_helper_hands_out_dict_rows(db):
    from git_synapse.db.engine import cursor

    with cursor() as cur:
        cur.execute("SELECT 1 AS x")
        assert cur.fetchone() == {"x": 1}


def test_the_cursor_helper_honours_a_different_row_factory(db):
    from psycopg.rows import tuple_row

    from git_synapse.db.engine import cursor

    with cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT 1 AS x")
        assert cur.fetchone() == (1,)


# --------------------------------------------------------- the schema version

def test_a_missing_meta_table_reads_as_needing_the_schema(monkeypatch):
    """First boot against an empty database: the probe must answer "apply it",
    not raise."""
    from git_synapse.db import engine

    def raise_undefined(*a, **k):
        raise psycopg.errors.UndefinedTable("relation meta does not exist")

    monkeypatch.setattr(psycopg, "connect", raise_undefined)
    assert engine.schema_is_current() is False


def test_an_unreachable_database_reads_as_needing_the_schema(monkeypatch):
    """Any probe failure means "unknown", and unknown must not be reported as
    current -- booting on an unmigrated schema is the worse outcome."""
    from git_synapse.db import engine

    def raise_other(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(psycopg, "connect", raise_other)
    assert engine.schema_is_current() is False


def test_applying_the_schema_retries_while_a_lock_is_held(monkeypatch):
    """DDL queues ahead of ordinary queries, so a blocked apply stalls every
    reader behind it. It fails fast and backs off instead."""
    from git_synapse.db import engine

    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise psycopg.errors.LockNotAvailable("canceling statement")
        raise psycopg.errors.QueryCanceled("canceling statement due to lock timeout")

    monkeypatch.setattr(psycopg, "connect", flaky)
    monkeypatch.setattr(engine, "schema_is_current", lambda: False)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="could not apply schema"):
        engine.apply_schema()
    assert attempts["n"] == engine.SCHEMA_RETRIES


def test_applying_the_schema_succeeds_once_the_lock_clears(monkeypatch):
    from git_synapse.db import engine

    attempts = {"n": 0}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            pass

        def commit(self):
            pass

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return _Conn()

    monkeypatch.setattr(psycopg, "connect", flaky)
    monkeypatch.setattr(engine, "schema_is_current", lambda: False)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    engine.apply_schema()
    assert attempts["n"] == 2


def test_a_connection_that_will_not_close_still_lets_the_retry_proceed(monkeypatch):
    """The close is best-effort: the point is to stop the pool reusing a
    connection with a stale plan, and failing to close it must not turn a
    recoverable error into a fatal one."""
    from git_synapse.db import engine

    calls = {"n": 0}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.errors.FeatureNotSupported(
                    "cached plan must not change result type"
                )

        def fetchone(self):
            return {"x": 1}

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def close(self):
            raise OSError("socket already gone")

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(engine, "get_pool", lambda: type("P", (), {
        "connection": lambda self: _Ctx()})())
    assert engine.query_one("SELECT 1 AS x") == {"x": 1}
    assert calls["n"] == 2


def test_copy_rows_opens_its_own_connection_when_given_none(db):
    """Callers inside a transaction pass theirs; the CLI and one-off scripts do
    not, and that branch had never run."""
    from git_synapse.db.engine import connection, copy_rows, copy_into_temp

    with connection() as conn:
        conn.execute("CREATE TEMP TABLE t_copy_own (a INT, b TEXT)")
        assert copy_rows("t_copy_own", ["a", "b"], [], conn=conn) == 0
        assert copy_rows("t_copy_own", ["a", "b"], [(1, "x"), (2, "y")], conn=conn) == 2
        assert conn.execute("SELECT count(*) FROM t_copy_own").fetchone()[0] == 2

    # With no connection passed, copy_rows opens one of its own and commits.
    with connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t_copy_shared (a INT, b TEXT)")
        conn.execute("TRUNCATE t_copy_shared")
    try:
        assert copy_rows("t_copy_shared", ["a", "b"], [(9, "z")]) == 1
        with connection() as conn:
            assert conn.execute(
                "SELECT b FROM t_copy_shared"
            ).fetchone()[0] == "z"
    finally:
        with connection() as conn:
            conn.execute("DROP TABLE IF EXISTS t_copy_shared")


def test_copy_into_temp_creates_the_table_and_stages_the_rows(db):
    from git_synapse.db.engine import connection, copy_into_temp

    with connection() as conn:
        staged = copy_into_temp(
            conn, "t_staged", [("repo_id", "BIGINT"), ("name", "TEXT")],
            [(1, "a"), (2, "b"), (3, "c")],
        )
        assert staged == 3
        assert conn.execute("SELECT sum(repo_id) FROM t_staged").fetchone()[0] == 6
