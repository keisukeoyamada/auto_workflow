"""Business days and holidays, shared by the whole fleet (scheduler, prompts, routines, dashboard).

Holidays live in config/holidays.json (git-ignored, edited from the dashboard), falling back to the
committed config/holidays.example.json. A business day is Mon-Fri and not a holiday.
"Nth business day" counts from the first business day of the month (1 = first).
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

HOLIDAYS_FILE = CONFIG_DIR / "holidays.json"
HOLIDAYS_EXAMPLE = CONFIG_DIR / "holidays.example.json"
# Official list of Japanese national holidays (Cabinet Office). Shift_JIS CSV: "YYYY/M/D,名称".
CAO_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

_cache: tuple[float, dict[str, dict[str, str]]] | None = None


def _source() -> Path | None:
    return HOLIDAYS_FILE if HOLIDAYS_FILE.exists() else (HOLIDAYS_EXAMPLE if HOLIDAYS_EXAMPLE.exists() else None)


def holidays() -> dict[str, dict[str, str]]:
    """{'YYYY-MM-DD': {'name': ..., 'note': ...}} (cached by file mtime)."""
    global _cache
    src = _source()
    if src is None:
        return {}
    mtime = src.stat().st_mtime
    if _cache and _cache[0] == mtime:
        return _cache[1]
    try:
        rows = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rows = []
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r.get("date", ""))):
            out[r["date"]] = {"name": str(r.get("name") or ""), "note": str(r.get("note") or "")}
    _cache = (mtime, out)
    return out


def save_holidays(rows: dict[str, dict[str, str]]) -> int:
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = [{"date": d, "name": v.get("name", ""), "note": v.get("note", "")} for d, v in sorted(rows.items())]
    tmp = HOLIDAYS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(HOLIDAYS_FILE)
    return len(data)


def upsert_holiday(day: str, name: str, note: str = "") -> int:
    datetime.strptime(day, "%Y-%m-%d")  # ValueError on a bad date
    if not name.strip():
        raise ValueError("名称を入力してください")
    rows = dict(holidays())
    rows[day] = {"name": name.strip()[:40], "note": note.strip()[:80]}
    return save_holidays(rows)


def delete_holiday(day: str) -> int:
    rows = dict(holidays())
    if day not in rows:
        raise ValueError(f"{day} は祝日一覧にありません")
    del rows[day]
    return save_holidays(rows)


def import_cao_csv(raw: bytes | None = None, timeout: int = 15) -> dict[str, int]:
    """Merge the Cabinet Office CSV (download it when raw is None). Company holidays already listed are kept."""
    if raw is None:
        from urllib.request import Request, urlopen

        req = Request(CAO_CSV_URL, headers={"User-Agent": "nakama-holiday-import"})
        with urlopen(req, timeout=timeout) as res:  # noqa: S310 - fixed https URL
            raw = res.read()
    text = raw.decode("cp932", errors="replace")
    rows = dict(holidays())
    added = 0
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 2:
            continue
        try:
            d = datetime.strptime(rec[0].strip(), "%Y/%m/%d").date().isoformat()
        except ValueError:
            continue  # header line
        if d not in rows:
            added += 1
        rows[d] = {"name": rec[1].strip(), "note": rows.get(d, {}).get("note", "")}
    total = save_holidays(rows)
    return {"added": added, "total": total}


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays()


def business_days_of_month(year: int, month: int) -> list[date]:
    last = calendar.monthrange(year, month)[1]
    return [d for d in (date(year, month, i) for i in range(1, last + 1)) if is_business_day(d)]


def business_day_index(d: date) -> int | None:
    """1-based position of d among its month's business days (None on weekends / holidays)."""
    days = business_days_of_month(d.year, d.month)
    return days.index(d) + 1 if d in days else None


def nth_business_day(year: int, month: int, n: int) -> date | None:
    """n >= 1 counts from the start; n <= -1 counts from the end (-1 = last business day)."""
    days = business_days_of_month(year, month)
    if not days or n == 0 or abs(n) > len(days):
        return None
    return days[n - 1] if n > 0 else days[n]


def upcoming_holidays(start: date, days: int = 30) -> list[tuple[str, str]]:
    end = (start + timedelta(days=days)).isoformat()
    return [(d, v["name"]) for d, v in sorted(holidays().items()) if start.isoformat() <= d <= end]


def prompt_section(today: date) -> str:
    """Runtime-context block telling agents about business days (fixes 'is today a holiday?' guesses)."""
    idx = business_day_index(today)
    status = f"{idx} 営業日目" if idx else ("祝日: " + holidays()[today.isoformat()]["name"] if today.isoformat() in holidays()
                                         else "休日（土日）")
    ups = upcoming_holidays(today, 30)
    lines = [f"- 今日 {today.isoformat()}: {status}（今月の営業日 {len(business_days_of_month(today.year, today.month))} 日）",
             "- 今後 30 日の祝日: " + (", ".join(f"{d} {n}" for d, n in ups) if ups else "なし（祝日一覧に登録がない可能性もある）")]
    if not holidays() or max(holidays()) < (today + timedelta(days=60)).isoformat():
        lines.append("- 注意: 祝日一覧が 60 日先までカバーしていない。祝日判定に自信がないときはそう書く")
    return "## 営業日・祝日（Nakama の祝日一覧より）\n" + "\n".join(lines)


def calendar_month(year: int, month: int) -> list[dict[str, Any]]:
    """Every day of the month with weekday, holiday name and business-day index, for grids."""
    last = calendar.monthrange(year, month)[1]
    hol = holidays()
    out = []
    for i in range(1, last + 1):
        d = date(year, month, i)
        out.append({"date": d.isoformat(), "day": i, "wd": "月火水木金土日"[d.weekday()],
                    "weekend": d.weekday() >= 5, "holiday": hol.get(d.isoformat(), {}).get("name", ""),
                    "bd": business_day_index(d)})
    return out
