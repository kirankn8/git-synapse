"""Run bookkeeping tests using only the ORM session API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from git_synapse.db.orm import models, session_factory, session_scope
from git_synapse.ingest import pipeline


@pytest.fixture
def held_lock(scratch_db):
    Meta = models().Meta
    session = session_factory()()
    row = session.get(Meta, "lock:ingest")
    if row is None:
        row = Meta(key="lock:ingest", value={"owner": "test"})
        session.add(row)
        session.flush()
    session.refresh(row, with_for_update=True)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_skipped_run_creates_no_row_and_reports_itself(scratch_db, held_lock):
    IngestRun = models().IngestRun
    with session_scope() as session:
        before = session.query(IngestRun).count()
    result = pipeline.run_ingest(records=[], trigger="test")
    assert result.status == "skipped"
    with session_scope() as session:
        assert session.query(IngestRun).count() == before


def test_active_run_ignores_a_finished_one(scratch_db):
    IngestRun = models().IngestRun
    with session_scope() as session:
        row = IngestRun(kind="sync", trigger="test", status="success",
                        started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
        session.add(row)
        session.flush()
        run_id = row.id
    try:
        current = pipeline.active_run()
        assert current is None or current["id"] != run_id
    finally:
        with session_scope() as session:
            row = session.get(IngestRun, run_id)
            if row is not None:
                session.delete(row)


def test_ingest_lock_is_released_when_the_run_raises(scratch_db):
    def boom(*args, **kwargs):
        raise RuntimeError("run exploded")

    original = pipeline._run_ingest_locked
    pipeline._run_ingest_locked = boom
    try:
        with pytest.raises(RuntimeError, match="run exploded"):
            pipeline.run_ingest(records=[], trigger="test")
    finally:
        pipeline._run_ingest_locked = original

    with session_scope() as session:
        assert pipeline._try_ingest_lock(session)


def test_second_run_is_skipped_while_one_holds_the_lock(scratch_db, held_lock):
    result = pipeline.run_ingest(records=[], trigger="test")
    assert result.status == "skipped"
    assert result.run_id is None


def test_abandoned_runs_are_reconciled(scratch_db):
    IngestRun = models().IngestRun
    old = datetime.now(UTC) - timedelta(hours=24)
    fresh = datetime.now(UTC)
    with session_scope() as session:
        stale = IngestRun(kind="sync", trigger="test", status="running", started_at=old)
        current = IngestRun(kind="sync", trigger="test", status="running", started_at=fresh)
        session.add_all([stale, current])
        session.flush()
        stale_id, fresh_id = stale.id, current.id
    try:
        assert pipeline.reconcile_stale_runs(max_age_hours=6) == 1
        with session_scope() as session:
            assert session.get(IngestRun, stale_id).status == "failed"
            assert "abandoned" in session.get(IngestRun, stale_id).error
            assert session.get(IngestRun, fresh_id).status == "running"
        assert pipeline.active_run()["id"] == fresh_id
    finally:
        with session_scope() as session:
            for run_id in (stale_id, fresh_id):
                row = session.get(IngestRun, run_id)
                if row is not None:
                    session.delete(row)


def test_failed_run_summary_identifies_the_common_error(db):
    run = pipeline.RunResult(kind="sync")
    run.repos = [
        pipeline.RepoResult(full_name=f"acme/r{i}", status="failed", error="disk full")
        for i in range(5)
    ] + [pipeline.RepoResult(full_name="acme/ok", status="success")]
    summary = pipeline._failure_summary(run)
    assert "5 of 6 repositories failed" in summary
    assert "every one with" in summary
