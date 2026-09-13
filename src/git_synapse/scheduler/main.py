"""Daily refresh scheduler.

Runs the same :func:`git_synapse.ingest.pipeline.run_ingest` the CLI and API use, on
a cron schedule. Kept as its own container so a long refresh cannot compete with
API request handling for the connection pool, and so it can be scaled to zero on
a deployment that only wants on-demand ingestion.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from git_synapse.config import get_config, live_cron
from git_synapse.db.engine import apply_schema, wait_for_database
from git_synapse.ingest import pipeline

log = logging.getLogger("git_synapse.scheduler")

#: Guards against a slow refresh overlapping the next cron tick.
_run_lock = threading.Lock()


def refresh(trigger: str = "schedule", discover: bool = False) -> None:
    """Fetch new commits, then rebuild whatever moved.

    Skips entirely if a previous refresh is still running. This matters far more
    now that the fast tier fires every 15 minutes: a slow tick must never overlap
    the next one, and two concurrent runs would fight over the same mirrors.

    Args:
        trigger: recorded on the run row.
        discover: also re-list the organisation through the GitHub API. Left off
            for the frequent tier, since new repositories do not appear every
            quarter hour and this is the only part that spends API quota.
    """
    if not _run_lock.acquire(blocking=False):
        log.warning("%s tick skipped: a refresh is still running", trigger)
        return
    try:
        log.info("starting %s refresh (discover=%s)", trigger, discover)
        records = None if discover else pipeline.load_repo_records()
        result = pipeline.run_ingest(records=records, trigger=trigger)
        log.info(
            "refresh finished: %s, %d ok, %d failed, %d commits added in %.1fs",
            result.status, len(result.ok), len(result.failed),
            result.commits_added, result.duration_s,
        )
    except Exception:
        log.exception("refresh failed")
    finally:
        _run_lock.release()


def _follow_stored_schedule(scheduler, timezone: str) -> None:
    """Re-schedule a job whose stored cron no longer matches the running one.

    Changing the schedule from the UI should take effect in about a minute, not
    on the next restart -- a deployment that has to be restarted to be slowed
    down will simply not be slowed down. One small query a minute is cheaper
    than the surprise.
    """
    for job_id, which in (("fast-refresh", "refresh"), ("discovery-refresh", "discover")):
        job = scheduler.get_job(job_id)
        if job is None:      # pragma: no cover - only if a job failed to register
            continue
        wanted = live_cron(which)
        try:
            trigger = CronTrigger.from_crontab(wanted, timezone=timezone)
        except ValueError:
            # Stored by hand, or by a future version with a different grammar.
            # Keep running on the last good schedule rather than stopping.
            log.warning("stored %s cron %r does not parse; keeping %s",
                        which, wanted, job.trigger)
            continue
        if str(trigger) == str(job.trigger):
            continue
        log.info("%s cron changed to %r; rescheduling", which, wanted)
        # reschedule_job replaces the trigger and recomputes the next fire time;
        # modify_job alone would leave the old one standing.
        scheduler.reschedule_job(job_id, trigger=trigger)
        scheduler.modify_job(job_id, misfire_grace_time=_grace_for(trigger, timezone))


def _grace_for(trigger, timezone: str, floor: int = 600) -> int:
    """How late a tick may be and still be worth running.

    Half the configured interval: late enough to survive a slow start, early
    enough that a tick is dropped rather than colliding with the next. This was
    a fixed 600s, justified by "the next one is imminent anyway" -- true when
    the cron fired quarterly-hourly, false once it is hourly, where dropping a
    tick costs a full hour. Derived from the trigger so it follows whatever
    REFRESH_CRON is set to, rather than assuming an interval.
    """
    now = datetime.now(ZoneInfo(timezone))
    first = trigger.get_next_fire_time(None, now)
    if first is None:
        return floor
    second = trigger.get_next_fire_time(first, first)
    if second is None:
        return floor
    return max(floor, int((second - first).total_seconds() // 2))


def main() -> int:
    cfg = get_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-24s %(message)s",
    )

    wait_for_database()
    apply_schema()
    pipeline.reconcile_stale_runs()

    if not cfg.schedule.enabled:
        log.info("scheduler disabled (SCHEDULER_ENABLED=false); idling")
        signal.pause()
        return 0

    scheduler = BlockingScheduler(timezone=cfg.schedule.timezone)

    fast_cron = live_cron("refresh")
    fast = CronTrigger.from_crontab(fast_cron, timezone=cfg.schedule.timezone)
    scheduler.add_job(
        refresh,
        trigger=fast,
        kwargs={"trigger": "schedule", "discover": False},
        id="fast-refresh",
        name="incremental repository refresh",
        max_instances=1,
        coalesce=True,          # collapse missed ticks into one
        misfire_grace_time=_grace_for(fast, cfg.schedule.timezone),
    )

    slow_cron = live_cron("discover")
    slow = CronTrigger.from_crontab(slow_cron, timezone=cfg.schedule.timezone)
    scheduler.add_job(
        refresh,
        trigger=slow,
        kwargs={"trigger": "discover", "discover": True},
        id="discovery-refresh",
        name="organisation discovery + refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # A minute is fast enough that a change made in the UI feels applied, and
    # slow enough that the query is free.
    scheduler.add_job(
        _follow_stored_schedule,
        trigger="interval",
        seconds=60,
        args=[scheduler, cfg.schedule.timezone],
        id="schedule-watch",
        name="follow a schedule changed from the UI",
        max_instances=1,
        coalesce=True,
    )

    # get_next_fire_time needs a concrete "now"; passing None for both
    # arguments makes APScheduler dereference it and crash.
    now = datetime.now(ZoneInfo(cfg.schedule.timezone))
    log.info(
        "fast refresh cron %r in %s; next run %s",
        fast_cron, cfg.schedule.timezone, fast.get_next_fire_time(None, now),
    )
    log.info(
        "discovery cron %r in %s; next run %s",
        slow_cron, cfg.schedule.timezone,
        slow.get_next_fire_time(None, now),
    )

    if cfg.schedule.run_on_start:
        log.info("REFRESH_ON_START set; running an immediate refresh")
        threading.Thread(target=refresh, args=("startup",), daemon=True).start()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
