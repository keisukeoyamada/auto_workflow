"""Tests for: overtime log/reminder, cron text, memo de-dup, profile edit, due dates (note/routine)."""
from datetime import datetime

import pytest

from nakama import cronja, overtime, routines, todo_note, ui


def test_cron_is_read_in_japanese():
    assert cronja.describe("30 7 * * mon-fri") == "平日 7:30"
    assert cronja.describe("0 12,19 * * *") == "毎日 12:00 / 19:00"
    assert "15 分ごと" in cronja.describe("*/15 8-22 * * *")


def test_overtime_log_and_reminder(tmp_path, monkeypatch):
    monkeypatch.setattr(overtime, "FILE", tmp_path / "ot.json")
    monkeypatch.setattr(overtime, "config", lambda: {"remind_at": "18:00"})
    monkeypatch.setattr("nakama.bizdays.holidays", lambda: {})
    thu = datetime(2026, 11, 26, 18, 5)
    assert overtime.reminder_due(thu) and not overtime.reminder_due(datetime(2026, 11, 26, 17, 0))
    row = overtime.add("2026-11-26", "18:00", "20:30", "月次資料")
    assert row["hours"] == 2.5 and "11/26（木） 18:00〜20:30（2.5h）" in row["text"]
    assert not overtime.reminder_due(thu)                               # logged -> no reminder
    assert overtime.month_summary("2026-11")["total"] == 2.5
    with pytest.raises(ValueError):
        overtime.add("2026-11-26", "6pm", "20:00", "x")


def test_memo_double_submit_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    a = ui.save_memo("same text")
    b = ui.save_memo("same text")
    assert a == b and len(list((tmp_path / "memos").glob("*.md"))) == 1


def test_profile_edit_keeps_comments(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    src = (ui.ROOT / "config" / "fleet.example.yaml").read_text(encoding="utf-8")
    (tmp_path / "config" / "fleet.yaml").write_text(src, encoding="utf-8")
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    ui.save_profile('Taro "T" Yamada', "Taro / Example")
    text = (tmp_path / "config" / "fleet.yaml").read_text(encoding="utf-8")
    assert 'name: "Taro \\"T\\" Yamada"' in text and "# must match a member id" in text


def test_due_dates(tmp_path, monkeypatch):
    f = tmp_path / "note.md"
    f.write_text("## ASAP\n□task one\n", encoding="utf-8")
    monkeypatch.setattr(todo_note, "note_path", lambda: f)
    monkeypatch.setattr(todo_note, "BACKUP_DIR", tmp_path / "bk")
    item = todo_note.load()["items"][0]
    todo_note.set_due(item["line"], item["raw"], "2026-12-01")
    assert f.read_text(encoding="utf-8").splitlines()[1] == "□task one 📅 2026-12-01"
    assert todo_note.load()["items"][0]["due"] == "2026-12-01"
    monkeypatch.setattr(routines, "PROGRESS_DIR", tmp_path / "prog")
    monkeypatch.setattr(routines, "load_routines", lambda: [{"id": "r1", "name": "x", "rule": {"type": "adhoc"}}])
    routines.set_due_override("r1", "2026-11", "2026-11-20")
    assert routines.progress("2026-11")["r1"]["due"] == "2026-11-20"
    with pytest.raises(ValueError):
        routines.set_due_override("r1", "2026-11", "2026-12-01")       # must be inside that month
