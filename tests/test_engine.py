"""The database layer everything else sits on.

Its failure modes are the ones that look like something else: a stale prepared
plan surfacing as "could not load this view", a schema apply deadlocking against
a running ingest, a watermark that commits separately from the work it describes.
"""
from __future__ import annotations

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
