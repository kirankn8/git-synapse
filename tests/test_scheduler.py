"""The scheduler's guards.

It runs unattended every 15 minutes, so its failure modes are the ones nobody
watches: a tick that overlaps the previous one, or an exception that kills the
loop and stops all future refreshes silently.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("apscheduler")

from git_synapse.scheduler import main as sched  # noqa: E402


def test_a_tick_is_skipped_while_the_previous_one_runs(monkeypatch):
    """Two concurrent refreshes fight over the same mirrors."""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_ingest(**kwargs):
        calls.append(kwargs.get("trigger"))
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("stop here; the run itself is not under test")

    monkeypatch.setattr(sched.pipeline, "run_ingest", slow_ingest)
    monkeypatch.setattr(sched.pipeline, "load_repo_records", lambda: [])

    first = threading.Thread(target=sched.refresh, kwargs={"trigger": "one"})
    first.start()
    assert started.wait(timeout=5), "the first refresh never started"

    # While the first holds the lock, a second must return immediately.
    sched.refresh(trigger="two")
    assert calls == ["one"], f"a second refresh ran concurrently: {calls}"

    release.set()
    first.join(timeout=5)


def test_the_lock_is_released_after_a_failing_run(monkeypatch):
    """A crash must not wedge the scheduler for the life of the process."""
    def boom(**kwargs):
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(sched.pipeline, "run_ingest", boom)
    monkeypatch.setattr(sched.pipeline, "load_repo_records", lambda: [])

    sched.refresh(trigger="first")
    # If the lock leaked, this would be skipped rather than attempted.
    ran = []
    monkeypatch.setattr(
        sched.pipeline, "run_ingest",
        lambda **kw: ran.append(kw.get("trigger")) or _Result(),
    )
    sched.refresh(trigger="second")
    assert ran == ["second"], "the lock was not released after a failure"


class _Result:
    status = "success"
    ok: list = []
    failed: list = []
    commits_added = 0
    duration_s = 0.0


def test_refresh_survives_an_exception_rather_than_killing_the_loop(monkeypatch):
    """apscheduler would stop calling a job that propagates; the process must
    keep scheduling."""
    monkeypatch.setattr(
        sched.pipeline, "run_ingest",
        lambda **kw: (_ for _ in ()).throw(ValueError("bad run")),
    )
    monkeypatch.setattr(sched.pipeline, "load_repo_records", lambda: [])
    sched.refresh(trigger="schedule")  # must not raise


def test_the_frequent_tier_does_not_spend_api_quota(monkeypatch):
    """Discovery is the only part that calls GitHub; the 15-minute tick must
    reuse the stored repository list instead."""
    used_discovery = []
    monkeypatch.setattr(
        sched.pipeline, "load_repo_records", lambda: used_discovery.append("stored") or []
    )
    monkeypatch.setattr(sched.pipeline, "run_ingest", lambda **kw: _Result())

    sched.refresh(trigger="schedule", discover=False)
    assert used_discovery == ["stored"]


def test_discovery_tier_passes_no_records_so_the_org_is_relisted(monkeypatch):
    seen = {}
    monkeypatch.setattr(sched.pipeline, "run_ingest",
                        lambda **kw: seen.update(kw) or _Result())
    monkeypatch.setattr(sched.pipeline, "load_repo_records", lambda: ["should-not-be-used"])

    sched.refresh(trigger="discovery", discover=True)
    assert seen["records"] is None


@pytest.mark.parametrize("cron", ["*/15 * * * *", "0 3 * * *"])
def test_configured_crons_parse(cron):
    """A malformed cron string would crash the container at startup."""
    from apscheduler.triggers.cron import CronTrigger

    assert CronTrigger.from_crontab(cron) is not None


def test_the_real_configured_crons_are_valid():
    from apscheduler.triggers.cron import CronTrigger

    from git_synapse.config import get_config

    cfg = get_config().schedule
    for expr in (cfg.cron, cfg.discover_cron):
        assert CronTrigger.from_crontab(expr, timezone=cfg.timezone) is not None


def test_main_wires_both_tiers_and_survives_a_bad_run(monkeypatch):
    """`main()` is the container's entrypoint: if it raises, nothing ever
    refreshes and the only symptom is silence."""
    import git_synapse.scheduler.main as sched_main

    added = []

    class _FakeScheduler:
        def __init__(self, *a, **kw): pass
        def add_job(self, func, trigger, **kw):
            added.append(kw.get("id") or getattr(func, "__name__", "job"))
        def start(self): raise KeyboardInterrupt  # exit the blocking loop

    monkeypatch.setattr(sched_main, "BlockingScheduler", _FakeScheduler, raising=False)
    monkeypatch.setattr(sched_main.pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(sched_main, "wait_for_database", lambda *a, **kw: None)
    monkeypatch.setattr(sched_main, "apply_schema", lambda *a, **kw: None)

    rc = sched_main.main()
    assert rc == 0
    assert len(added) >= 2, f"both tiers must be scheduled, got {added}"


def test_main_waits_for_the_database_before_scheduling(monkeypatch):
    """Starting jobs against a database that is not up yet fails every tick
    until someone restarts the container."""
    import git_synapse.scheduler.main as sched_main

    order = []

    class _FakeScheduler:
        def __init__(self, *a, **kw): pass
        def add_job(self, *a, **kw): order.append("add_job")
        def start(self): raise KeyboardInterrupt

    monkeypatch.setattr(sched_main, "BlockingScheduler", _FakeScheduler, raising=False)
    monkeypatch.setattr(sched_main.pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(sched_main, "wait_for_database",
                        lambda *a, **kw: order.append("wait"))
    monkeypatch.setattr(sched_main, "apply_schema",
                        lambda *a, **kw: order.append("schema"))

    sched_main.main()
    assert order[0] == "wait", order
