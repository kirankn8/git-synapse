"""Run bookkeeping, on a scratch database.

These write `ingest_run` rows, and the live scheduler writes them too every
fifteen minutes -- sharing a database with it made the assertions race. A module
either uses the scratch database throughout or the real corpus throughout;
mixing them switches POSTGRES_DB process-wide and points the rest of the file at
the wrong place.
"""
from __future__ import annotations

import pytest

from git_synapse.db.engine import connection, query_one
from git_synapse.ingest import pipeline


@pytest.fixture
def held_lock():
    """Take the ingest lock, or skip.

    A real refresh runs every fifteen minutes against this server. Blocking on
    the lock it already holds would hang the suite, and proceeding without it
    would test something else -- so a test that needs the lock says so and
    stands aside when it cannot have it.
    """
    from git_synapse.db.engine import connection

    with connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (pipeline.INGEST_LOCK_KEY,)
        ).fetchone()[0]
        if not got:
            pytest.skip("a real ingest holds the lock")
        try:
            yield conn
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (pipeline.INGEST_LOCK_KEY,))



def test_a_skipped_run_creates_no_row_and_reports_itself(scratch_db, held_lock):
    before = query_one("SELECT count(*) AS n FROM ingest_run")["n"]
    result = pipeline.run_ingest(records=[], trigger="test")

    assert result.status == "skipped"
    assert query_one("SELECT count(*) AS n FROM ingest_run")["n"] == before


def test_active_run_ignores_a_finished_one(scratch_db):
    with connection() as conn:
        rid = conn.execute(
            "INSERT INTO ingest_run (kind, trigger, status, started_at, finished_at)"
            " VALUES ('sync','test','success',now(),now()) RETURNING id"
        ).fetchone()[0]
    try:
        current = pipeline.active_run()
        assert current is None or current["id"] != rid
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM ingest_run WHERE id=%s", (rid,))


def test_ingest_lock_is_released_when_the_run_raises(scratch_db):
    """An advisory lock outlives its transaction and ends only with the session,
    and a pooled connection's session does not end when it is returned -- so a
    lock left held would wedge every later run permanently."""

    def boom(*args, **kwargs):
        raise RuntimeError("run exploded")

    original = pipeline._run_ingest_locked
    pipeline._run_ingest_locked = boom
    try:
        with pytest.raises(RuntimeError, match="run exploded"):
            pipeline.run_ingest(records=[], trigger="test")
    finally:
        pipeline._run_ingest_locked = original

    with connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (pipeline.INGEST_LOCK_KEY,)
        ).fetchone()[0]
        if got:
            conn.execute("SELECT pg_advisory_unlock(%s)", (pipeline.INGEST_LOCK_KEY,))
    assert got, "the lock was still held after the run raised"


def test_a_second_run_is_skipped_while_one_holds_the_lock(scratch_db, held_lock):
    """Concurrent runs fetched the same mirrors and redid the same rebuilds."""
    result = pipeline.run_ingest(records=[], trigger="test")

    assert result.status == "skipped"
    assert result.run_id is None, "a skipped run must not create an ingest_run row"


def test_abandoned_runs_are_reconciled_and_stop_blocking(scratch_db):
    """A killed run must not block ingestion forever.

    `POST /api/ingest/refresh` refuses to start while a run is in flight, so a
    container killed mid-run would otherwise disable ingestion permanently.
    Liveness is the advisory lock, not the row's age.
    """
    with connection() as conn:
        stale_id = conn.execute(
            "INSERT INTO ingest_run (kind, trigger, status, started_at)"
            " VALUES ('sync','test','running', now() - interval '24 hours') RETURNING id"
        ).fetchone()[0]
        fresh_id = conn.execute(
            "INSERT INTO ingest_run (kind, trigger, status, started_at)"
            " VALUES ('sync','test','running', now()) RETURNING id"
        ).fetchone()[0]

    try:
        with connection() as live:
            if not live.execute(
                "SELECT pg_try_advisory_lock(%s)", (pipeline.INGEST_LOCK_KEY,)
            ).fetchone()[0]:
                pytest.skip("a real ingest holds the lock")
            try:
                assert pipeline.reconcile_stale_runs(max_age_hours=6) >= 1

                stale = query_one(
                    "SELECT status, error FROM ingest_run WHERE id=%s", (stale_id,)
                )
                assert stale["status"] == "failed"
                assert "abandoned" in (stale["error"] or "")

                fresh = query_one("SELECT status FROM ingest_run WHERE id=%s", (fresh_id,))
                assert fresh["status"] == "running", "a lock-held run is alive"
                assert pipeline.active_run()["id"] == fresh_id
            finally:
                live.execute("SELECT pg_advisory_unlock(%s)", (pipeline.INGEST_LOCK_KEY,))

        # With nothing holding the lock the same row is provably dead.
        assert pipeline.reconcile_stale_runs(max_age_hours=6) >= 1
        assert query_one(
            "SELECT status FROM ingest_run WHERE id=%s", (fresh_id,)
        )["status"] == "failed"
    finally:
        with connection() as conn:
            conn.execute("DELETE FROM ingest_run WHERE id = ANY(%s)",
                         ([stale_id, fresh_id],))


# --------------------------------------------------------- why a run failed

def test_a_failed_run_records_why(db):
    """A run row that says `failed` with an empty error is the worst of both:
    it tells a reader something is wrong and nothing about what."""
    from git_synapse.ingest import pipeline

    run = pipeline.RunResult(kind="sync")
    run.repos = [
        pipeline.RepoResult(full_name=f"acme/r{i}", status="failed",
                            error="psycopg.errors.InvalidColumnReference: there is "
                                  "no unique or exclusion constraint matching the "
                                  "ON CONFLICT specification")
        for i in range(163)
    ]
    summary = pipeline._failure_summary(run)
    assert "163 of 163 repositories failed" in summary
    assert "every one with" in summary, "identical failures are one problem"
    assert "InvalidColumnReference" in summary


def test_a_mixed_failure_names_the_commonest_and_counts_the_rest(db):
    from git_synapse.ingest import pipeline

    run = pipeline.RunResult(kind="sync")
    run.repos = (
        [pipeline.RepoResult(full_name=f"a/r{i}", status="failed", error="disk full")
         for i in range(5)]
        + [pipeline.RepoResult(full_name="a/x", status="failed", error="bad ref")]
        + [pipeline.RepoResult(full_name="a/ok", status="success")]
    )
    summary = pipeline._failure_summary(run)
    assert "6 of 7 repositories failed" in summary
    assert "the most common (5) was: disk full" in summary
    assert "1 other kind of error" in summary


def test_only_the_first_line_of_a_traceback_reaches_the_summary(db):
    """A run row is read in a table cell. Twenty lines of Python there is worse
    than nothing, because it pushes the rest of the page off screen."""
    from git_synapse.ingest import pipeline

    run = pipeline.RunResult(kind="sync")
    run.repos = [pipeline.RepoResult(
        full_name="a/r", status="failed",
        error="RuntimeError: it broke\n  File \"x.py\", line 1\n    boom()")]
    summary = pipeline._failure_summary(run)
    assert summary.endswith("RuntimeError: it broke")
    assert "\n" not in summary


def test_a_failure_with_no_message_still_counts(db):
    from git_synapse.ingest import pipeline

    run = pipeline.RunResult(kind="sync")
    run.repos = [pipeline.RepoResult(full_name="a/r", status="failed")]
    assert "unknown error" in pipeline._failure_summary(run)


def test_a_clean_run_has_nothing_to_explain(db):
    from git_synapse.ingest import pipeline

    run = pipeline.RunResult(kind="sync")
    run.repos = [pipeline.RepoResult(full_name="a/r", status="success")]
    assert pipeline._failure_summary(run) is None
