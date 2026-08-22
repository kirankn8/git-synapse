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

from git_synapse.config import get_config
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
    except Exception:  # noqa: BLE001 - the scheduler must survive a bad run
        log.exception("refresh failed")
    finally:
        _run_lock.release()


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

    fast = CronTrigger.from_crontab(cfg.schedule.cron, timezone=cfg.schedule.timezone)
    scheduler.add_job(
        refresh,
        trigger=fast,
        kwargs={"trigger": "schedule", "discover": False},
        id="fast-refresh",
        name="incremental repository refresh",
        max_instances=1,
        coalesce=True,          # collapse missed ticks into one
        # Short grace on the fast tier: a tick more than one interval late is
        # better dropped than run, because the next one is imminent anyway.
        misfire_grace_time=600,
    )

    slow = CronTrigger.from_crontab(
        cfg.schedule.discover_cron, timezone=cfg.schedule.timezone
    )
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

    # get_next_fire_time needs a concrete "now"; passing None for both
    # arguments makes APScheduler dereference it and crash.
    now = datetime.now(ZoneInfo(cfg.schedule.timezone))
    log.info(
        "fast refresh cron %r in %s; next run %s",
        cfg.schedule.cron, cfg.schedule.timezone, fast.get_next_fire_time(None, now),
    )
    log.info(
        "discovery cron %r in %s; next run %s",
        cfg.schedule.discover_cron, cfg.schedule.timezone,
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
