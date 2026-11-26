"""Offline tests for business days, holidays and routine scheduling."""
from datetime import date

import pytest

from nakama import bizdays, routines


@pytest.fixture()
def holidays(monkeypatch):
    days = {"2026-11-03": {"name": "文化の日", "note": ""}, "2026-11-23": {"name": "勤労感謝の日", "note": ""}}
    monkeypatch.setattr(bizdays, "holidays", lambda: days)
    return days


def test_business_day_counting(holidays):
    assert bizdays.business_day_index(date(2026, 11, 2)) == 1      # Mon
    assert bizdays.business_day_index(date(2026, 11, 3)) is None   # holiday
    assert bizdays.business_day_index(date(2026, 11, 4)) == 2
    assert len(bizdays.business_days_of_month(2026, 11)) == 19     # 19 business days in Nov 2026
    assert bizdays.nth_business_day(2026, 11, -1) == date(2026, 11, 30)


@pytest.mark.parametrize("text,rule", [
    ("1-3営業日", {"type": "monthly", "windows": [[1, 3]]}),
    ("1~18営業日", {"type": "monthly", "windows": [[1, 18]]}),
    ("3営業日まで", {"type": "monthly", "windows": [[1, 3]]}),
    ("13営業日以降", {"type": "monthly", "windows": [[13, -1]]}),
    ("５営業日", {"type": "monthly", "windows": [[5, 5]]}),
    ("6営業日、月末", {"type": "monthly", "windows": [[6, 6], [-1, -1]]}),
    ("4営,15営", {"type": "monthly", "windows": [[4, 4], [15, 15]]}),
    ("毎週月曜日", {"type": "weekly", "weekday": 0, "every": 1}),
    ("隔週", {"type": "weekly", "weekday": 0, "every": 2}),
    ("随時", {"type": "adhoc"}),
    ("半年次（６か月ごと）", {"type": "adhoc"}),
])
def test_parse_schedule(text, rule):
    assert routines.parse_schedule(text) == rule


def test_occurrences_skip_holidays(holidays):
    win = routines.occurrences({"type": "monthly", "windows": [[1, 3]]}, 2026, 11)
    assert win == [(date(2026, 11, 2), date(2026, 11, 5))]          # 11/3 is a holiday
    mondays = routines.occurrences({"type": "weekly", "weekday": 0, "every": 1}, 2026, 11)
    assert date(2026, 11, 24) in [a for a, _ in mondays]            # 11/23 holiday -> next business day


def test_import_cao_csv_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(bizdays, "HOLIDAYS_FILE", tmp_path / "holidays.json")
    monkeypatch.setattr(bizdays, "HOLIDAYS_EXAMPLE", tmp_path / "none.json")
    monkeypatch.setattr(bizdays, "_cache", None)
    bizdays.upsert_holiday("2026-12-30", "年末年始休暇", "会社休日")
    raw = "国民の祝日・休日月日,国民の祝日・休日名称\r\n2027/1/1,元日\r\n".encode("cp932")
    assert bizdays.import_cao_csv(raw) == {"added": 1, "total": 2}
    assert bizdays.holidays()["2026-12-30"]["note"] == "会社休日"     # company holidays are kept


def test_notes_keys_are_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(routines, "NOTES_DIR", tmp_path)
    key = routines.routine_key("r001", "2026-11")
    routines.add_note(key, "やった")
    assert routines.note_count(key) == 1 and routines.notes(key)[0]["text"] == "やった"
    with pytest.raises(ValueError):
        routines.add_note("../../evil", "x")
