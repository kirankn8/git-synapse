"""Daily refresh scheduler."""

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
    """Fetch new commits, then rebuild whatever moved."""
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
    """Re-schedule a job whose stored cron no longer matches the running one."""
    for job_id, which in (("fast-refresh", "refresh"), ("discovery-refresh", "discover")):
        job = scheduler.get_job(job_id)
        if job is None:      # pragma: no cover - only if a job failed to register
            continue
        wanted = live_cron(which)
        try:
            trigger = CronTrigger.from_crontab(wanted, timezone=timezone)
        except ValueError:
            log.warning("stored %s cron %r does not parse; keeping %s",
                        which, wanted, job.trigger)
            continue
        if str(trigger) == str(job.trigger):
            continue
        log.info("%s cron changed to %r; rescheduling", which, wanted)
        scheduler.reschedule_job(job_id, trigger=trigger)
        scheduler.modify_job(job_id, misfire_grace_time=_grace_for(trigger, timezone))


def _grace_for(trigger, timezone: str, floor: int = 600) -> int:
    """How late a tick may be and still be worth running."""
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
