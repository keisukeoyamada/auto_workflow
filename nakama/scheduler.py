"""Long-running scheduler: fires each agent on its cron schedule (the 'deployment')."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import load_fleet
from .runner import run_agent

log = logging.getLogger(__name__)


def _job(name: str) -> None:
    # Each job gets its own event loop; failures are logged, never crash the scheduler.
    try:
        asyncio.run(run_agent(name))
    except Exception:  # noqa: BLE001 - keep the daemon alive no matter what one run does
        log.exception("%s: unexpected failure", name)


def build_scheduler() -> BlockingScheduler:
    fleet = load_fleet()
    grace = int(fleet.limits.get("misfire_grace_seconds") or 3600)
    scheduler = BlockingScheduler(timezone=fleet.timezone)
    for name, spec in fleet.agents.items():
        if not spec.enabled:
            continue
        for i, expr in enumerate(spec.schedule):
            scheduler.add_job(
                _job,
                CronTrigger.from_crontab(expr, timezone=fleet.timezone),
                args=[name],
                id=f"{name}#{i}",
                max_instances=1,           # never overlap the same agent
                coalesce=True,             # after sleep/downtime, run once instead of N times
                misfire_grace_time=grace,  # still run if the PC woke up within the grace window
            )
    return scheduler


def serve() -> None:
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        log.info("scheduled %-14s %s", job.id, job.trigger)
    log.info("Nakama scheduler started. Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
