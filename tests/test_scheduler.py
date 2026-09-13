"""The scheduler's guards."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("apscheduler")

from git_synapse.scheduler import main as sched


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
    monkeypatch.setattr(sched.pipeline, "load_repo_records", list)

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
    monkeypatch.setattr(sched.pipeline, "load_repo_records", list)

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
    """apscheduler would stop calling a job that propagates; the process must keep scheduling."""
    monkeypatch.setattr(
        sched.pipeline, "run_ingest",
        lambda **kw: (_ for _ in ()).throw(ValueError("bad run")),
    )
    monkeypatch.setattr(sched.pipeline, "load_repo_records", list)
    sched.refresh(trigger="schedule")  # must not raise


def test_the_frequent_tier_does_not_spend_api_quota(monkeypatch):
    """Discovery is the only part that calls GitHub; the 15-minute tick must reuse the stored repository list instead."""
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
    """`main()` is the container's entrypoint: if it raises, nothing ever refreshes and the only symptom is silence."""
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
    """Starting jobs against a database that is not up yet fails every tick until someone restarts the container."""
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


def test_a_disabled_scheduler_idles_instead_of_exiting(monkeypatch):
    """The container must stay up with SCHEDULER_ENABLED=false."""
    import git_synapse.scheduler.main as sched_main
    from git_synapse.config import get_config, reset_config_cache

    paused = []
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    reset_config_cache()
    monkeypatch.setattr(sched_main.pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(sched_main, "wait_for_database", lambda *a, **kw: None)
    monkeypatch.setattr(sched_main, "apply_schema", lambda *a, **kw: None)
    monkeypatch.setattr(sched_main.signal, "pause", lambda: paused.append(True))
    monkeypatch.setattr(
        sched_main, "BlockingScheduler",
        lambda *a, **kw: pytest.fail("a disabled scheduler still built a scheduler"),
        raising=False,
    )
    try:
        assert sched_main.main() == 0
        assert paused == [True]
        assert get_config().schedule.enabled is False
    finally:
        reset_config_cache()


def test_refresh_on_start_runs_one_immediately_without_blocking_startup(monkeypatch):
    """A cold container would otherwise serve stale data until the next tick."""
    import threading

    import git_synapse.scheduler.main as sched_main
    from git_synapse.config import reset_config_cache

    started = []
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("REFRESH_ON_START", "true")
    reset_config_cache()

    class _FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        def add_job(self, *a, **kw):
            pass

        def start(self):
            raise KeyboardInterrupt

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=False):
            started.append((getattr(target, "__name__", target), args, daemon))

        def start(self):
            pass

    monkeypatch.setattr(sched_main, "BlockingScheduler", _FakeScheduler, raising=False)
    monkeypatch.setattr(sched_main.pipeline, "reconcile_stale_runs", lambda *a, **kw: 0)
    monkeypatch.setattr(sched_main, "wait_for_database", lambda *a, **kw: None)
    monkeypatch.setattr(sched_main, "apply_schema", lambda *a, **kw: None)
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    try:
        assert sched_main.main() == 0
        assert started and started[0][1] == ("startup",)
        assert started[0][2] is True, "a non-daemon thread would block shutdown"
    finally:
        reset_config_cache()


@pytest.mark.parametrize(("cron", "expected"), [
    ("0 * * * *",   1800),   # hourly -> half an hour
    ("*/30 * * * *", 900),
    ("*/15 * * * *", 600),   # half is 450, but the floor holds
    ("*/5 * * * *",  600),   # so does it here
    ("0 3 * * *",  43200),   # daily -> twelve hours; nothing caps the top end
])
def test_the_misfire_grace_follows_the_configured_interval(cron, expected):
    """A late tick is worth running if the next one is far off, and worth dropping if it is imminent."""
    from apscheduler.triggers.cron import CronTrigger

    import git_synapse.scheduler.main as sched

    trigger = CronTrigger.from_crontab(cron, timezone="UTC")
    assert sched._grace_for(trigger, "UTC") == expected


def test_a_trigger_that_never_fires_still_yields_a_usable_grace():
    """`get_next_fire_time` returns None for an exhausted trigger."""
    import git_synapse.scheduler.main as sched

    class Never:
        def get_next_fire_time(self, previous, now):
            return None

    assert sched._grace_for(Never(), "UTC") == 600

    class Once:
        def __init__(self):
            self.calls = 0

        def get_next_fire_time(self, previous, now):
            self.calls += 1
            return now if self.calls == 1 else None

    assert sched._grace_for(Once(), "UTC") == 600


def test_the_refresh_default_is_declared_once_and_matches_everywhere():
    """The default lives in three files -- the config constant, the compose environment and .env.example."""
    import re
    from pathlib import Path

    from git_synapse.config import DEFAULT_REFRESH_CRON

    root = Path(__file__).resolve().parents[1]
    compose_text = (root / "docker-compose.yml").read_text()
    declared = re.findall(r"REFRESH_CRON: \$\{REFRESH_CRON:-([^}]+)\}", compose_text)
    assert declared == [DEFAULT_REFRESH_CRON], declared

    anchor = compose_text[compose_text.index("x-app-env:"):compose_text.index("services:")]
    assert "REFRESH_CRON:" in anchor, "the API must see the same schedule as the scheduler"

    example = re.search(r"^REFRESH_CRON=(.+)$",
                        (root / ".env.example").read_text(), re.MULTILINE)
    assert example and example.group(1).strip() == DEFAULT_REFRESH_CRON, example



def test_a_stored_schedule_overrides_the_environment_and_clearing_restores_it(db):
    """A deployment that must be restarted to be slowed down will not be slowed down, so the schedule is stored and the environment is only the seed."""
    from git_synapse.analysis import settings
    from git_synapse.config import get_config, live_cron

    from_env = get_config().schedule.cron
    try:
        assert live_cron("refresh") == from_env
        settings.set("refresh_cron", "*/7 * * * *")
        assert live_cron("refresh") == "*/7 * * * *"
        assert settings.get("refresh_cron") == "*/7 * * * *"
    finally:
        settings.clear("refresh_cron")
    assert live_cron("refresh") == from_env
    assert settings.get("refresh_cron") is None


def test_only_named_settings_can_be_stored(db):
    """An open key/value endpoint invites storing configuration nothing reads."""
    from git_synapse.analysis import settings

    with pytest.raises(ValueError, match="not a writable setting"):
        settings.set("github_token", "ghp_whatever")


def test_a_settings_read_that_fails_falls_back_rather_than_stopping_the_loop(monkeypatch):
    """The scheduler reads this every minute."""
    from git_synapse.analysis import settings

    def boom(_name):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(settings, "get", boom)
    assert settings.effective("refresh_cron", "0 * * * *") == "0 * * * *"


def test_the_scheduler_follows_a_schedule_changed_while_it_runs(monkeypatch):
    """The whole point of storing it: a change made in the UI applies without a restart, and within a minute."""
    import git_synapse.scheduler.main as sched

    calls = {"rescheduled": [], "modified": []}

    class Job:
        trigger = "cron[hour='*']"

    class Sched:
        def get_job(self, job_id):
            return Job() if job_id == "fast-refresh" else None

        def reschedule_job(self, job_id, trigger=None):
            calls["rescheduled"].append((job_id, str(trigger)))

        def modify_job(self, job_id, misfire_grace_time=None):
            calls["modified"].append((job_id, misfire_grace_time))

    monkeypatch.setattr(sched, "live_cron", lambda which: "*/5 * * * *")
    sched._follow_stored_schedule(Sched(), "UTC")
    assert calls["rescheduled"] and calls["rescheduled"][0][0] == "fast-refresh"
    assert calls["modified"] == [("fast-refresh", 600)]


def test_an_unchanged_schedule_is_left_alone(monkeypatch):
    """Rescheduling every minute would reset the next fire time every minute, so a job on a long cron could never reach it."""
    from apscheduler.triggers.cron import CronTrigger

    import git_synapse.scheduler.main as sched

    same = CronTrigger.from_crontab("0 * * * *", timezone="UTC")
    touched = []

    class Sched:
        def get_job(self, job_id):
            return type("J", (), {"trigger": same})()

        def reschedule_job(self, *a, **k):
            touched.append(a)

        def modify_job(self, *a, **k):
            touched.append(a)

    monkeypatch.setattr(sched, "live_cron", lambda which: "0 * * * *")
    sched._follow_stored_schedule(Sched(), "UTC")
    assert touched == []


def test_a_stored_cron_that_does_not_parse_leaves_the_job_running(monkeypatch):
    """Written by hand, or by a future version."""
    import git_synapse.scheduler.main as sched

    touched = []

    class Sched:
        def get_job(self, job_id):
            return type("J", (), {"trigger": "cron[hour='*']"})()

        def reschedule_job(self, *a, **k):
            touched.append(a)

        def modify_job(self, *a, **k):
            touched.append(a)

    monkeypatch.setattr(sched, "live_cron", lambda which: "nonsense")
    sched._follow_stored_schedule(Sched(), "UTC")
    assert touched == []


def test_a_missing_job_is_skipped_rather_than_crashing_the_watcher(monkeypatch):
    import git_synapse.scheduler.main as sched

    class Sched:
        def get_job(self, job_id):
            return None

    monkeypatch.setattr(sched, "live_cron", lambda which: "0 * * * *")
    sched._follow_stored_schedule(Sched(), "UTC")   # must not raise
