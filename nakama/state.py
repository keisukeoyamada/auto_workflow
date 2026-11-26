"""Run bookkeeping: last successful run, daily spend, per-agent locks, changed files."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterator

from .config import LOGS_DIR, ROOT

STATE_FILE = LOGS_DIR / "state.json"
COSTS_FILE = LOGS_DIR / "costs.jsonl"
LOCK_DIR = LOGS_DIR / "locks"
STALE_LOCK_SECONDS = 3 * 3600


class AgentBusy(Exception):
    """Another run of the same agent is in progress."""


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def last_success(agent: str) -> datetime | None:
    value = _load_state().get(agent, {}).get("last_success")
    return datetime.fromisoformat(value) if value else None


def mark_success(agent: str, when: datetime) -> None:
    state = _load_state()
    state.setdefault(agent, {})["last_success"] = when.isoformat()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_cost(agent: str, when: datetime, cost_usd: float, turns: int, is_error: bool) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": when.isoformat(), "agent": agent, "cost_usd": cost_usd, "turns": turns, "is_error": is_error}
    with COSTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def spend_on(day: str, tz: tzinfo) -> dict[str, float]:
    """Total cost per agent for a YYYY-MM-DD in the fleet timezone."""
    totals: dict[str, float] = {}
    if not COSTS_FILE.exists():
        return totals
    for line in COSTS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if datetime.fromisoformat(row["ts"]).astimezone(tz).date().isoformat() == day:
            totals[row["agent"]] = totals.get(row["agent"], 0.0) + float(row.get("cost_usd") or 0)
    return totals


@contextmanager
def agent_lock(agent: str) -> Iterator[None]:
    """Prevent overlapping runs of the same agent (e.g. manual run during a scheduled one)."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{agent}.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
        lock.unlink(missing_ok=True)  # left behind by a crash
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AgentBusy(agent) from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def changed_files(paths: list[str], since: datetime | None) -> list[str]:
    """Files under `paths` modified after `since` (all files if since is None)."""
    cutoff = since.timestamp() if since else 0.0
    found = []
    for rel in paths:
        base = ROOT / rel
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.name != ".gitkeep" and p.stat().st_mtime > cutoff:
                found.append(p.relative_to(ROOT).as_posix())
    return sorted(found)
