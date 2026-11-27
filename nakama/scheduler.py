"""Long-running scheduler: fires each agent on its cron schedule (the 'deployment')."""
from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ConfigError, load_fleet
from .runner import run_agent
from .spawn import spawn_agent

log = logging.getLogger(__name__)


def _job(name: str) -> None:
    # Each job gets its own event loop; failures are logged, never crash the scheduler.
    try:
        if sys.platform == "win32":
            # Run in a hidden-console child so the Claude CLI doesn't pop up a terminal window.
            code = spawn_agent(name).wait()
            if code:
                log.error("%s: run exited with code %d (see the lines above)", name, code)
            return
        asyncio.run(run_agent(name))
    except Exception:  # noqa: BLE001 - keep the daemon alive no matter what one run does
        log.exception("%s: unexpected failure", name)


def _rongo_job() -> None:
    from . import rongo

    result = rongo.run_generation()
    log.info("rongo: %s", result)


def _add_agent_jobs(scheduler: BlockingScheduler) -> None:
    fleet = load_fleet()
    grace = int(fleet.limits.get("misfire_grace_seconds") or 3600)
    rongo_cfg = fleet.rongo or {}
    if rongo_cfg.get("enabled"):  # not an agent: one short Claude call per chapter, a few chapters ahead
        from .rongo import DEFAULT_SCHEDULE

        scheduler.add_job(_rongo_job, CronTrigger.from_crontab(str(rongo_cfg.get("schedule") or DEFAULT_SCHEDULE),
                                                               timezone=fleet.timezone),
                          id="rongo#daily", max_instances=1, coalesce=True, misfire_grace_time=grace)
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


def _fleet_mtime() -> float:
    try:
        return load_fleet().source.stat().st_mtime
    except (ConfigError, OSError):
        return 0.0


def _reload_if_changed(scheduler: BlockingScheduler, seen: dict) -> None:
    """Pick up fleet.yaml edits (e.g. from the settings tab) without restarting the service."""
    mtime = _fleet_mtime()
    if mtime == seen["mtime"]:
        return
    seen["mtime"] = mtime
    try:
        load_fleet()  # validate first; keep the old jobs if the new file is broken
    except ConfigError as exc:
        log.error("fleet.yaml changed but is invalid; keeping the current schedule: %s", exc)
        return
    for job in scheduler.get_jobs():
        if job.id not in (RELOAD_JOB_ID, REMIND_JOB_ID):
            job.remove()
    _add_agent_jobs(scheduler)
    log.info("fleet.yaml changed: schedule reloaded (%d jobs)", len(scheduler.get_jobs()) - 1)


RELOAD_JOB_ID = "_reload_fleet"
REMIND_JOB_ID = "_reminders"


def _reminders() -> None:
    """Built-in reminders (no LLM): today's overtime request, sent once to Slack/Teams if configured."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .config import LOGS_DIR
    from .notify import notify_all
    from . import overtime

    fleet = load_fleet()
    now = datetime.now(ZoneInfo(fleet.timezone))
    sent_file = LOGS_DIR / "reminders.json"
    try:
        sent = json.loads(sent_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sent = {}
    key = f"overtime-{now.date().isoformat()}"
    if overtime.reminder_due(now) and key not in sent:
        result = notify_all(fleet.notify, "今日の残業申請を上げてください（Nakama の 勤怠 > 残業 で申請文をコピーできます）", "🕕 残業申請")
        sent[key] = now.isoformat(timespec="minutes")
        sent_file.write_text(json.dumps(sent, indent=1), encoding="utf-8")
        log.info("overtime reminder sent: %s", result)


def build_scheduler() -> BlockingScheduler:
    fleet = load_fleet()
    scheduler = BlockingScheduler(timezone=fleet.timezone)
    _add_agent_jobs(scheduler)
    seen = {"mtime": _fleet_mtime()}
    scheduler.add_job(_reload_if_changed, "interval", seconds=60, args=[scheduler, seen], id=RELOAD_JOB_ID)
    scheduler.add_job(_reminders, "interval", minutes=5, id=REMIND_JOB_ID)
    return scheduler


def serve() -> None:
    scheduler = build_scheduler()
    for job in scheduler.get_jobs():
        if job.id in (RELOAD_JOB_ID, REMIND_JOB_ID):
            continue
        log.info("scheduled %-14s %s", job.id, job.trigger)
    log.info("Nakama scheduler started. Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
