"""The call log: what was asked, what came back, and what it costs to record.

Two surfaces consume this product and neither left a trace, so "is anything
using this?" had no answer. These cover the properties that make the log
trustworthy: it never blocks or breaks a caller, it bounds what it stores, and
it says when it dropped something rather than under-reporting silently.
"""
from __future__ import annotations

import json
import queue

import pytest

from git_synapse.analysis import calls


@pytest.fixture(autouse=True)
def _drain():
    """Each test starts with an empty queue and leaves one behind."""
    while True:
        try:
            calls._queue.get_nowait()
        except queue.Empty:
            break
    yield


def test_a_call_is_queued_with_what_was_asked_and_what_came_back():
    calls.record("mcp", "coupled_files", arguments={"repo": "guava"},
                 result={"partners": [1, 2, 3]}, duration_ms=12)
    row = calls._queue.get_nowait()
    assert row["surface"] == "mcp" and row["name"] == "coupled_files"
    assert json.loads(row["arguments"]) == {"repo": "guava"}
    assert json.loads(row["result_preview"]) == {"partners": [1, 2, 3]}
    assert row["result_rows"] == 3 and row["duration_ms"] == 12


def test_a_large_reply_is_truncated_but_its_true_size_is_kept():
    """A thousand-row table would make the log larger than the data it
    describes. How much came back is usually the question anyway."""
    # Sized from the constant, so raising CALL_LOG_BODY_BYTES does not quietly
    # turn this into a test of nothing.
    big = {"rows": ["x" * 100] * (calls.PREVIEW_BYTES // 100 + 20)}
    calls.record("http", "/api/pairs", result=big)
    row = calls._queue.get_nowait()
    stored = json.loads(row["result_preview"])
    assert stored["truncated"] is True
    assert stored["bytes"] > calls.PREVIEW_BYTES
    assert len(stored["head"]) == calls.PREVIEW_BYTES
    assert row["result_rows"] == len(big["rows"]), "row count survives truncation"


def test_explicit_size_and_rows_win_over_what_can_be_inferred():
    """An HTTP reply is bytes on the wire; re-encoding the parsed body would
    report a number the client never saw."""
    calls.record("http", "/api/repos", result={"repos": [1]},
                 result_bytes=9999, result_rows=42)
    row = calls._queue.get_nowait()
    assert row["result_bytes"] == 9999 and row["result_rows"] == 42


def test_something_unserialisable_is_described_rather_than_dropped():
    calls.record("mcp", "odd", arguments={"fn": object()})
    row = calls._queue.get_nowait()
    assert row.get("arguments")


def test_a_full_queue_drops_and_counts_rather_than_blocking(monkeypatch):
    """Telemetry that slows the thing it measures is a bad trade; telemetry
    that can wedge it is worse. The count makes the loss visible."""
    monkeypatch.setattr(calls, "_ensure_worker", lambda: None)
    before = calls.dropped()
    tiny: queue.Queue = queue.Queue(maxsize=1)
    monkeypatch.setattr(calls, "_queue", tiny)
    calls.record("mcp", "one")
    calls.record("mcp", "two")     # no room; must not raise
    calls.record("mcp", "three")
    assert calls.dropped() == before + 2


def test_recording_never_raises_even_when_everything_is_wrong(monkeypatch):
    def boom(_result):
        raise RuntimeError("nope")

    monkeypatch.setattr(calls, "_summarise", boom)
    calls.record("mcp", "x")       # must not raise


def test_rows_reach_the_database_and_read_back(db, settled_calls):
    calls.record("mcp", "test_tool", arguments={"a": 1},
                 result={"items": [1, 2]}, duration_ms=7, client="pytest")
    rows = settled_calls(lambda: calls.recent(surface="mcp", name="test_tool", limit=5))
    assert rows[0]["name"] == "test_tool"
    full = calls.detail(rows[0]["id"])
    assert full["arguments"] == {"a": 1}
    assert full["result_preview"] == {"items": [1, 2]}

    summary = calls.summary(hours=1)
    assert summary["calls"] >= 1 and summary["mcp_calls"] >= 1
    assert any(r["name"] == "test_tool" for r in calls.by_name(surface="mcp", hours=1))


def test_flushing_an_empty_queue_writes_nothing(db):
    assert calls._flush_once() == 0


def test_the_log_is_pruned_by_age(db):
    """It grows with traffic, while everything else here grows with history."""
    from datetime import UTC, datetime, timedelta

    from git_synapse.db.orm import models, session_scope
    with session_scope() as session:
        session.add(models().CallLog(at=datetime.now(UTC) - timedelta(days=calls.KEEP_DAYS + 5),
                                     surface="http", name="ancient", status="ok", duration_ms=1))
    with session_scope() as session:
        assert session.query(models().CallLog).filter_by(name="ancient").count() == 1
    calls.prune()
    with session_scope() as session:
        assert session.query(models().CallLog).filter_by(name="ancient").count() == 0


def test_the_log_is_pruned_by_row_count(db, monkeypatch):
    from datetime import UTC, datetime

    from git_synapse.db.orm import models, session_scope

    with session_scope() as session:
        rows = [models().CallLog(at=datetime.now(UTC), surface="http", name=f"bounded-{i}",
                                 status="ok", duration_ms=1) for i in range(3)]
        session.add_all(rows)
        session.flush()
        newest = sorted(row.id for row in rows)[-2:]

    monkeypatch.setattr(calls, "KEEP_ROWS", 2)
    assert calls.prune() >= 1
    with session_scope() as session:
        kept = {row.id for row in session.query(models().CallLog.id)}
    assert kept == set(newest)


@pytest.mark.parametrize("timed,value,only,expected", [
    (0, None, None, None),
    (1, 42.0, 42, 42),
    (3, 41.26, 10, 41.3),
])
def test_a_percentile_needs_two_timings_before_it_interpolates(timed, value, only, expected):
    assert calls._percentile(timed, value, only) == expected


def test_filters_narrow_the_list(db, settled_calls):
    calls.record("http", "/api/a", status="ok")
    calls.record("http", "/api/b", status="error", error="boom")
    only_errors = settled_calls(lambda: calls.recent(status="error", limit=50))
    assert only_errors and all(r["status"] == "error" for r in only_errors)
    assert calls.detail(-1) is None


def test_pruning_is_best_effort_and_reports_what_it_removed(db, caplog, monkeypatch):
    """It runs at the end of an ingest. A failure there must not fail the run,
    and a silent failure would let the table grow unbounded unnoticed."""
    import logging

    from git_synapse.ingest import pipeline

    monkeypatch.setattr(calls, "prune", lambda: 3)
    with caplog.at_level(logging.INFO, logger="git_synapse.ingest.pipeline"):
        pipeline._prune_call_log()
    assert "pruned 3 row(s)" in caplog.text

    def boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr(calls, "prune", boom)
    with caplog.at_level(logging.WARNING, logger="git_synapse.ingest.pipeline"):
        pipeline._prune_call_log()      # must not raise
    assert "could not prune the call log" in caplog.text


def test_a_list_reply_and_an_unserialisable_one_are_both_summarised():
    """Not every reply is a dict of rows: MCP tools return lists, and anything
    can arrive that json refuses. Neither may lose the record."""
    calls.record("mcp", "list_tool", result=[1, 2, 3, 4])
    assert calls._queue.get_nowait()["result_rows"] == 4

    class Odd:
        def __repr__(self):
            return "<odd>"

    calls.record("mcp", "odd_tool", result={"x": Odd()})
    row = calls._queue.get_nowait()
    assert "odd" in row["result_preview"]


def test_an_empty_reply_records_no_body():
    calls.record("http", "/api/x", result=None)
    row = calls._queue.get_nowait()
    assert row["result_preview"] is None and row["result_rows"] is None


def test_the_flusher_thread_starts_once_and_is_reused():
    calls._ensure_worker()
    first = calls._worker
    assert first is not None and first.daemon
    calls._ensure_worker()
    assert calls._worker is first, "a second thread per call would leak one per call"


def test_a_value_json_refuses_is_still_recorded_as_something():
    """A circular structure defeats json.dumps even with default=str. Losing
    the whole row over an unprintable argument would hide the call."""
    loop: dict = {}
    loop["self"] = loop

    calls.record("mcp", "cyclic", arguments=loop, result=loop)
    row = calls._queue.get_nowait()
    assert row["arguments"] and "unserialisable" in row["arguments"]
    assert row["result_preview"]


def test_two_threads_racing_to_start_the_flusher_start_only_one(monkeypatch):
    """The check outside the lock is a fast path; the one inside is what makes
    it correct. Without it, two callers arriving together get two threads."""
    import contextlib
    import threading

    started: list[threading.Thread] = []
    monkeypatch.setattr(calls, "_worker", None)

    real_thread = threading.Thread

    class CountingThread(real_thread):
        def start(self):
            started.append(self)
            super().start()

    @contextlib.contextmanager
    def racing_lock():
        # Stand in for the other thread having won the race while we waited.
        alive = real_thread(target=lambda: __import__("time").sleep(0.4), daemon=True)
        alive.start()
        calls._worker = alive
        yield

    monkeypatch.setattr(calls, "_lock", type("L", (), {
        "__enter__": lambda self: racing_lock().__enter__(),
        "__exit__": lambda self, *a: None,
    })())
    monkeypatch.setattr(calls.threading, "Thread", CountingThread)
    calls._ensure_worker()
    assert started == [], "the loser of the race must not start a second flusher"


def test_every_registered_tool_is_listed_even_when_never_called(db, monkeypatch, settled_calls):
    """Two tools had been called and fourteen exist, so the page showed two and
    read as "this server has two tools". A tool nobody uses is the row worth
    seeing, and it cannot come from a table built out of calls."""
    monkeypatch.setattr(calls, "known_mcp_tools",
                        lambda: ["called_one", "never_one", "never_two"])
    calls.record("mcp", "called_one", result={"x": [1]})
    settled_calls(lambda: calls.recent(surface="mcp", name="called_one", limit=1))

    rows = calls.by_name(surface="mcp", hours=1)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) >= {"called_one", "never_one", "never_two"}
    assert by_name["called_one"]["calls"] >= 1
    assert by_name["never_one"]["calls"] == 0
    assert by_name["never_one"]["last_call"] is None


def test_the_idle_inventory_is_left_out_where_it_would_mislead(db, monkeypatch):
    """Under "errors only", a tool that has never run has never failed either;
    listing it with zero would read as a passing tool in a failure report."""
    monkeypatch.setattr(calls, "known_mcp_tools", lambda: ["never_one"])
    assert not [r for r in calls.by_name(surface="mcp", hours=1, status="error")
                if r["name"] == "never_one"]
    assert not [r for r in calls.by_name(surface="http", hours=1)
                if r["name"] == "never_one"]


def test_the_summary_narrows_with_the_surface_the_reader_chose(db, settled_calls):
    """It ignored the filter, so the list narrowed and every figure above it
    stayed put -- which reads as the filters not working."""
    calls.record("mcp", "a_tool", result={"x": [1]})
    calls.record("http", "/api/thing", result={"x": [1]})
    settled_calls(lambda: calls.recent(surface="http", name="/api/thing", limit=1))

    everything = calls.summary(hours=1)
    just_mcp = calls.summary(hours=1, surface="mcp")
    assert just_mcp["surface"] == "mcp"
    assert just_mcp["http_calls"] == 0
    assert just_mcp["calls"] < everything["calls"]


def test_the_window_reaches_the_call_list(db):
    """The window chips were setting a parameter the list never read."""
    from datetime import UTC, datetime, timedelta

    from git_synapse.db.orm import models, session_scope
    with session_scope() as session:
        session.add(models().CallLog(at=datetime.now(UTC) - timedelta(hours=5),
                                     surface="http", name="/api/old", status="ok", duration_ms=1))
    assert not [r for r in calls.recent(hours=1, limit=500) if r["name"] == "/api/old"]
    assert [r for r in calls.recent(hours=24, limit=500) if r["name"] == "/api/old"]


def test_an_unpublished_inventory_is_empty_rather_than_an_error(db):
    """The API reads what the MCP container published. Before it has started,
    or if it never does, the activity page must still render."""
    from git_synapse.db.orm import models, session_scope
    with session_scope() as session:
        session.query(models().Meta).filter_by(key="watermark:mcp_tools").delete(synchronize_session=False)
    assert calls.known_mcp_tools() == []


def test_the_timeline_returns_every_hour_including_the_empty_ones(db):
    """A chart drawn only from hours that had traffic closes the gaps silently
    and turns an outage into a smooth line."""
    buckets = calls.timeline(hours=6)
    assert len(buckets) == 6
    hours = [b["hour"] for b in buckets]
    assert hours == sorted(hours), "buckets must be in time order"
    for b in buckets:
        assert b["calls"] == b["mcp"] + b["http"]
        assert b["errors"] <= b["calls"]


def test_the_timeline_window_is_clamped(db):
    """A hand-typed hours=100000 would ask Postgres to generate a series with
    four million rows in it."""
    assert len(calls.timeline(hours=10_000)) == 168
    assert len(calls.timeline(hours=0)) == 1


def test_expired_sessions_are_pruned_with_the_call_log(db, caplog, monkeypatch):
    """Kept forever they are dead weight, and a record of who was signed in
    from where long after it could matter."""
    import logging

    from git_synapse import auth
    from git_synapse.ingest import pipeline

    monkeypatch.setattr(calls, "prune", lambda: 0)
    monkeypatch.setattr(auth, "prune_sessions", lambda: 4)
    monkeypatch.setattr(auth, "prune_login_attempts", lambda: 7)
    with caplog.at_level(logging.INFO, logger="git_synapse.ingest.pipeline"):
        pipeline._prune_call_log()
    assert "pruned 4 expired" in caplog.text
    assert "pruned 7 past the window" in caplog.text

    def boom():
        raise RuntimeError("gone")

    monkeypatch.setattr(auth, "prune_sessions", boom)
    with caplog.at_level(logging.WARNING, logger="git_synapse.ingest.pipeline"):
        pipeline._prune_call_log()      # must not raise
    assert "could not prune expired sessions" in caplog.text
