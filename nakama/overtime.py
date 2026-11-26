"""Overtime log + daily reminder (workspace/attendance/overtime.json, git-ignored).

The reminder is code, not an agent: on business days from `remind_at` (default 18:00) the dashboard
shows a notification until today's entry exists (or it is OK'd), and the scheduler posts it once to
Slack/Teams if configured. The copy-paste text uses fleet.yaml overtime.template.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from .config import WORKSPACE_DIR, load_fleet

FILE = WORKSPACE_DIR / "attendance" / "overtime.json"
DEFAULT_TEMPLATE = "【残業申請】{date}（{wd}） {start}〜{end}（{hours}h）\n理由: {reason}"


def config() -> dict[str, Any]:
    cfg = getattr(load_fleet(), "overtime", None) or {}
    return cfg if isinstance(cfg, dict) else {}


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(rows, key=lambda r: (r["date"], r.get("start", ""))), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(FILE)


def _hours(start: str, end: str) -> float:
    def mins(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    diff = mins(end) - mins(start)
    if diff <= 0:
        diff += 24 * 60  # past midnight
    return round(diff / 60, 2)


def add(day: str, start: str, end: str, reason: str) -> dict[str, Any]:
    datetime.strptime(day, "%Y-%m-%d")
    for t in (start, end):
        if not re.fullmatch(r"\d{1,2}:\d{2}", t or ""):
            raise ValueError("時刻は HH:MM で入力してください")
    if not reason.strip():
        raise ValueError("理由を入力してください")
    row = {"date": day, "start": start, "end": end, "hours": _hours(start, end), "reason": reason.strip()[:500],
           "created": datetime.now().isoformat(timespec="minutes")}
    rows = [r for r in load() if not (r["date"] == day and r.get("start") == start)]
    rows.append(row)
    _save(rows)
    return row | {"text": render(row)}


def delete(day: str, start: str) -> None:
    rows = load()
    keep = [r for r in rows if not (r["date"] == day and r.get("start") == start)]
    if len(keep) == len(rows):
        raise ValueError("該当する記録がありません")
    _save(keep)


def render(row: dict[str, Any]) -> str:
    d = date.fromisoformat(row["date"])
    template = str(config().get("template") or DEFAULT_TEMPLATE)
    return template.format(date=f"{d.month}/{d.day}", wd="月火水木金土日"[d.weekday()], start=row["start"],
                           end=row["end"], hours=row["hours"], reason=row["reason"])


def month_summary(month: str) -> dict[str, Any]:
    rows = [r | {"text": render(r)} for r in load() if r["date"].startswith(month)]
    return {"month": month, "rows": rows, "total": round(sum(r["hours"] for r in rows), 2), "days": len({r["date"] for r in rows})}


def reminder_due(now: datetime) -> bool:
    """Business day, past remind_at, and nothing logged for today yet."""
    from .bizdays import is_business_day

    if config().get("enabled", True) is False or not is_business_day(now.date()):
        return False
    hh, mm = (int(x) for x in str(config().get("remind_at") or "18:00").split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    return not any(r["date"] == now.date().isoformat() for r in load())
