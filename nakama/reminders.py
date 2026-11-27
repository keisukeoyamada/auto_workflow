"""Date reminders shown in the escalation feed (workspace/reminders.json, git-ignored).

  [{id, date: "YYYY-MM-DD", title, link, note, notify_from: "YYYY-MM-DD", done: false, created}]
A reminder appears from notify_from (default: the day before) until it is marked done.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any

from .config import WORKSPACE_DIR

FILE = WORKSPACE_DIR / "reminders.json"
_lock = threading.Lock()


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id")] if isinstance(data, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def add(day: str, title: str, link: str = "", note: str = "", lead_days: int = 1) -> dict[str, Any]:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if not title.strip():
        raise ValueError("内容を入力してください")
    if link and not re.match(r"https?://", link):
        raise ValueError("リンクは http(s):// で始めてください")
    with _lock:
        rows = load()
        row = {"id": f"r{len(rows) + 1:03d}", "date": d.isoformat(), "title": title.strip()[:200], "link": link.strip()[:500],
               "note": note.strip()[:500], "notify_from": (d - timedelta(days=max(0, lead_days))).isoformat(),
               "done": False, "created": datetime.now().isoformat(timespec="minutes")}
        while any(r["id"] == row["id"] for r in rows):
            row["id"] = f"r{int(row['id'][1:]) + 1:03d}"
        rows.append(row)
        _save(rows)
    return row


def set_done(rid: str, done: bool = True) -> dict[str, Any]:
    with _lock:
        rows = load()
        row = next((r for r in rows if r["id"] == rid), None)
        if row is None:
            raise ValueError("リマインドが見つかりません")
        row["done"] = done
        row["done_at"] = datetime.now().isoformat(timespec="minutes") if done else ""
        _save(rows)
    return row


def due(today: date) -> list[dict[str, Any]]:
    """Not done and already in its notice window (overdue ones stay until done)."""
    return [r for r in load() if not r.get("done") and r.get("notify_from", r["date"]) <= today.isoformat()]


WEEKDAYS = "月火水木金土日"


def draft_message(r: dict[str, Any], today: date) -> str:
    """A reminder you can paste into Teams/mail when you are the one chasing others (e.g. a scheduling poll)."""
    d = date.fromisoformat(r["date"])
    md = f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"
    subject = re.sub(r"(の)?(締め?切り?|〆切|期限)$", "", r["title"]).strip() or r["title"]
    poll = bool(re.search(r"chouseisan|调整|調整|densuke|forms\.(office|gle)|doodle", r.get("link", "") + r["title"]))
    ask = "ご回答（出欠・候補日のご入力）" if poll else "ご対応"
    if d > today:
        when = f"{md}が締切です。"
    elif d == today:
        when = f"本日 {md} が締切です。"
    else:
        when = f"締切（{md}）を過ぎていますが、まだの方は本日中にお願いできると助かります。"
    lines = [f"【リマインド】{subject}", "", "お疲れさまです。", f"{subject}の件、{when}",
             f"まだの方はお手数ですが、{ask}をお願いします。"]
    if r.get("link"):
        lines += ["", r["link"]]
    lines += ["", "すでにご対応いただいた方は、行き違いでしたらご容赦ください。"]
    return "\n".join(lines)
