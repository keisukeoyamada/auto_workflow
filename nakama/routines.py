"""Routine (定常) jobs: definitions, monthly occurrences on business days, progress and per-task notes.

Storage (git-ignored):
  workspace/routine/routines.json         definitions [{id, name, schedule_text, rule, manual, assignee, mine, active, note}]
  workspace/routine/progress/<YYYY-MM>.json  {routine_id: {status, updated}}
  workspace/notes/<key>.jsonl             notes per task; key = routine-<id>-<YYYY-MM> | planner-<hash>

rule (machine form of schedule_text):
  {"type": "monthly", "windows": [[start_bd, end_bd], ...]}   bd >= 1 from the start, -1 = last business day
  {"type": "weekly", "weekday": 0..4, "every": 1 | 2}         every Monday / every other Monday
  {"type": "adhoc"}                                            随時・半年次など（カレンダーには出さない）
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from .bizdays import business_days_of_month, is_business_day, nth_business_day
from .config import WORKSPACE_DIR

ROUTINE_DIR = WORKSPACE_DIR / "routine"
ROUTINES_FILE = ROUTINE_DIR / "routines.json"
PROGRESS_DIR = ROUTINE_DIR / "progress"
NOTES_DIR = WORKSPACE_DIR / "notes"
STATUSES = ("todo", "doing", "review", "pending", "done", "skip")
WEEKDAYS = "月火水木金"


# ---------- parsing 実施日 ----------

def parse_schedule(text: str) -> dict[str, Any]:
    """Best-effort conversion of the free-text schedule ('1-3営業日', '毎週月曜日', '6営業日、月末' ...)."""
    t = unicodedata.normalize("NFKC", str(text or "")).replace(" ", "")
    if not t or t in ("随時", "-"):
        return {"type": "adhoc"}
    m = re.search(r"毎週([月火水木金])", t)
    if m:
        return {"type": "weekly", "weekday": WEEKDAYS.index(m.group(1)), "every": 1}
    if t in ("週次",):
        return {"type": "weekly", "weekday": 0, "every": 1}
    if t.startswith("隔週"):
        return {"type": "weekly", "weekday": 0, "every": 2}
    if "最終週" in t:
        return {"type": "monthly", "windows": [[-5, -1]]}
    if re.search(r"半年|年次|毎年", t):
        return {"type": "adhoc"}
    windows: list[list[int]] = []
    for part in re.split(r"[、,，/]", t):
        if not part:
            continue
        if part.startswith("月末"):
            windows.append([-1, -1])
            continue
        m = re.fullmatch(r"(\d+)[-~〜～](\d+)営?業?日?", part)
        if m:
            windows.append([int(m.group(1)), int(m.group(2))])
            continue
        m = re.fullmatch(r"(\d+)営?業?日?まで", part)
        if m:
            windows.append([1, int(m.group(1))])
            continue
        m = re.fullmatch(r"(\d+)営?業?日?以降", part)
        if m:
            windows.append([int(m.group(1)), -1])
            continue
        m = re.fullmatch(r"(\d+)(営業日|営|)", part)
        if m:
            windows.append([int(m.group(1)), int(m.group(1))])
    return {"type": "monthly", "windows": windows} if windows else {"type": "adhoc"}


def describe(rule: dict[str, Any]) -> str:
    if rule.get("type") == "weekly":
        return ("隔週" if rule.get("every") == 2 else "毎週") + WEEKDAYS[int(rule.get("weekday", 0))] + "曜"
    if rule.get("type") == "monthly":
        def bd(n: int) -> str:
            return "月末" if n == -1 else (f"末{-n}" if n < 0 else str(n))
        return "・".join(f"{bd(a)}営" if a == b else f"{bd(a)}-{bd(b)}営" for a, b in rule.get("windows") or [])
    return "随時"


# ---------- storage ----------

def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_routines() -> list[dict[str, Any]]:
    data = _read(ROUTINES_FILE, [])
    out = []
    for r in data if isinstance(data, list) else []:
        if isinstance(r, dict) and r.get("id") and r.get("name"):
            r.setdefault("rule", parse_schedule(r.get("schedule_text", "")))
            r.setdefault("active", True)
            r.setdefault("mine", False)
            out.append(r)
    return out


def save_routines(rows: list[dict[str, Any]]) -> None:
    _write(ROUTINES_FILE, rows)


def progress(month: str) -> dict[str, dict[str, str]]:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month は YYYY-MM")
    data = _read(PROGRESS_DIR / f"{month}.json", {})
    return data if isinstance(data, dict) else {}


def set_progress(routine_id: str, month: str, status: str) -> dict[str, str]:
    if status not in STATUSES:
        raise ValueError(f"status は {STATUSES} のどれか")
    if routine_id not in {r["id"] for r in load_routines()}:
        raise ValueError("unknown routine")
    data = progress(month)
    data[routine_id] = {"status": status, "updated": datetime.now().isoformat(timespec="minutes")}
    _write(PROGRESS_DIR / f"{month}.json", data)
    return data[routine_id]


# ---------- occurrences ----------

def occurrences(rule: dict[str, Any], year: int, month: int) -> list[tuple[date, date]]:
    """Windows (first, last business day) of this routine within the month."""
    if rule.get("type") == "monthly":
        out = []
        for a, b in rule.get("windows") or []:
            first, last = nth_business_day(year, month, int(a)), nth_business_day(year, month, int(b))
            if first and last and first <= last:
                out.append((first, last))
        return out
    if rule.get("type") == "weekly":
        wd, every = int(rule.get("weekday", 0)), int(rule.get("every", 1))
        out, d = [], date(year, month, 1)
        while d.month == month:
            if d.weekday() == wd and (every == 1 or d.isocalendar()[1] % 2 == 0):
                day = d
                while not is_business_day(day) and day.month == month:  # holiday Monday -> next business day
                    day += timedelta(days=1)
                if day.month == month:
                    out.append((day, day))
            d += timedelta(days=1)
        return out
    return []


def month_view(month: str, mine_only: bool = True) -> dict[str, Any]:
    from .bizdays import calendar_month

    year, mon = (int(x) for x in month.split("-"))
    prog = progress(month)
    rows = []
    for r in load_routines():
        if not r.get("active") or (mine_only and not r.get("mine")):
            continue
        occ = occurrences(r["rule"], year, mon)
        rows.append({"id": r["id"], "name": r["name"], "schedule": r.get("schedule_text") or describe(r["rule"]),
                     "rule_text": describe(r["rule"]), "assignee": r.get("assignee", ""), "manual": r.get("manual", ""),
                     "note": r.get("note", ""), "windows": [[a.isoformat(), b.isoformat()] for a, b in occ],
                     "status": prog.get(r["id"], {}).get("status", "todo"), "notes": note_count(routine_key(r["id"], month))})
    first_bd = lambda row: row["windows"][0][0] if row["windows"] else "9999"
    rows.sort(key=lambda x: (first_bd(x), x["name"]))
    return {"month": month, "days": calendar_month(year, mon), "rows": rows,
            "business_days": len(business_days_of_month(year, mon))}


# ---------- notes per executed task ----------

def routine_key(routine_id: str, month: str) -> str:
    return f"routine-{routine_id}-{month}"


def planner_key(task_id_or_title: str) -> str:
    return "planner-" + hashlib.sha1(task_id_or_title.encode("utf-8")).hexdigest()[:12]


def _note_path(key: str):
    if not re.fullmatch(r"(routine-[\w\-]+-\d{4}-\d{2}|(planner|note)-[0-9a-f]{12})", key or ""):
        raise ValueError("bad note key")
    return NOTES_DIR / f"{key}.jsonl"


def notes(key: str) -> list[dict[str, str]]:
    try:
        lines = _note_path(key).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def note_count(key: str) -> int:
    try:
        return sum(1 for _ in _note_path(key).open(encoding="utf-8"))
    except OSError:
        return 0


def add_note(key: str, text: str, title: str = "") -> dict[str, str]:
    text = text.strip()
    if not text:
        raise ValueError("メモが空です")
    path = _note_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="minutes"), "text": text[:20000], "title": title[:200]}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def items_for_tasks(today: date) -> list[dict[str, Any]]:
    """This month's routine windows (mine, active) as rows for the unified task list."""
    month = today.strftime("%Y-%m")
    prog = progress(month)
    out = []
    for r in load_routines():
        if not (r.get("active") and r.get("mine")):
            continue
        occ = occurrences(r["rule"], today.year, today.month)
        if not occ:
            continue
        # one row per routine: the window that is current or next; after the last one, keep showing the last
        a, b = next(((a, b) for a, b in occ if b >= today), occ[-1])
        st = prog.get(r["id"], {}).get("status", "todo")
        out.append({"source": "routine", "id": r["id"], "title": r["name"], "due": b.isoformat(), "start": a.isoformat(),
                    "window": describe(r["rule"]), "status": st, "key": routine_key(r["id"], month),
                    "notes": note_count(routine_key(r["id"], month))})
    return out


def items_for_month(year: int, month: int, today: date) -> list[dict[str, Any]]:
    """Routine rows for the unified task list of any month (mine, active, one row per routine)."""
    key_month = f"{year:04d}-{month:02d}"
    prog = progress(key_month)
    pivot = today if (today.year, today.month) == (year, month) else date(year, month, 1)
    out = []
    for r in load_routines():
        if not (r.get("active") and r.get("mine")):
            continue
        occ = occurrences(r["rule"], year, month)
        if not occ:
            continue
        a, b = next(((a, b) for a, b in occ if b >= pivot), occ[-1])  # current / next window, else the last
        key = routine_key(r["id"], key_month)
        due = prog.get(r["id"], {}).get("due") or b.isoformat()  # owner may move it for this month
        out.append({"source": "routine", "id": r["id"], "title": r["name"], "due": due, "start": a.isoformat(),
                    "due_moved": bool(prog.get(r["id"], {}).get("due")),
                    "window": describe(r["rule"]), "status": prog.get(r["id"], {}).get("status", "todo"),
                    "month": key_month, "key": key, "notes": note_count(key)})
    return out


def set_due_override(routine_id: str, month: str, due: str) -> dict[str, str]:
    """Move this month's due date of one routine (e.g. done early / postponed). Empty = back to the rule."""
    if due:
        datetime.strptime(due, "%Y-%m-%d")
        if not due.startswith(month):
            raise ValueError("その月の日付を指定してください")
    data = progress(month)
    row = data.get(routine_id, {"status": "todo"})
    if due:
        row["due"] = due
    else:
        row.pop("due", None)
    row["updated"] = datetime.now().isoformat(timespec="minutes")
    data[routine_id] = row
    _write(PROGRESS_DIR / f"{month}.json", data)
    return row


def history(routine_id: str, months: int = 6, until: str | None = None) -> list[dict[str, Any]]:
    """The same routine in previous months: status, due override and number of notes (newest first)."""
    y, m = (int(x) for x in (until or datetime.now().strftime("%Y-%m")).split("-"))
    out = []
    for _ in range(months):
        key = f"{y:04d}-{m:02d}"
        p = progress(key).get(routine_id, {})
        out.append({"month": key, "status": p.get("status", ""), "due": p.get("due", ""),
                    "updated": p.get("updated", ""), "notes": note_count(routine_key(routine_id, key))})
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out
