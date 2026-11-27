"""Offline tests for the Analects tab (no network, no Claude)."""
from datetime import date
from types import SimpleNamespace

import pytest

from nakama import rongo


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(rongo, "DIR", tmp_path / "rongo")
    fleet = SimpleNamespace(rongo={"enabled": True, "ahead": 2}, timezone="Asia/Tokyo", limits={})
    monkeypatch.setattr(rongo, "load_fleet", lambda: fleet)
    monkeypatch.setattr("nakama.state.record_cost", lambda *a, **k: None)
    monkeypatch.setattr("nakama.state.spend_on", lambda *a, **k: {})
    return fleet


def test_bundled_text_has_all_books_and_chapters():
    chs = rongo.chapters()
    assert len(chs) == 512
    assert [c["id"] for c in chs[:2]] == ["01-01", "01-02"] and chs[0]["text"].startswith("子曰：“学而时习之")
    assert chs[0]["book_name"] == "学而" and chs[-1]["book_name"] == "堯曰" and chs[-1]["id"] == "20-03"
    assert sum(1 for c in chs if c["book"] == 1) == 16


def test_progress_streak_and_next_chapter(cfg):
    s = rongo.status(today=date(2026, 11, 27))
    assert (s["read"], s["total"], s["next_id"], s["current"]["id"]) == (0, 512, "01-01", "01-01")
    rongo.mark_read("01-01", today=date(2026, 11, 25))
    rongo.mark_read("01-02", today=date(2026, 11, 26))
    s = rongo.status(today=date(2026, 11, 27))
    assert s["streak"] == 2 and s["read_today"] == 0 and s["next_id"] == "01-03"  # not read today yet: run continues
    rongo.mark_read("01-03", today=date(2026, 11, 27))
    s = rongo.status(today=date(2026, 11, 27))
    assert s["streak"] == 3 and s["read"] == 3 and s["books"][0]["chapters"][2]["read"]
    rongo.mark_read("01-03", read=False)
    assert rongo.status(today=date(2026, 11, 27))["streak"] == 2
    assert rongo.status("02-01")["current"]["id"] == "02-01" and rongo.status("02-01")["prev_id"] == "01-16"
    with pytest.raises(ValueError):
        rongo.mark_read("99-99")


def test_generation_writes_notes_ahead(cfg):
    rongo.mark_read("01-01", today=date(2026, 11, 27))
    asked = []

    async def fake_ask(prompt):
        asked.append(prompt)
        return "## 書き下し文\n…\n## 現代語訳\n…\n## 語句\n…\n## 教訓\n…\n## 今日の問い\n…", 0.01

    assert rongo.upcoming() == ["01-02", "01-03"]
    result = rongo.run_generation(ask=fake_ask)
    assert result["made"] == ["01-02", "01-03"] and "学而篇 第2章" in asked[0]
    assert rongo.status("01-02")["current"]["note"].startswith("## 書き下し文")
    assert rongo.run_generation(ask=fake_ask)["made"] == []  # existing notes are not rewritten
    assert rongo.status()["notes_ready"] == 2

    async def bad_ask(prompt):
        return "すみません", 0.0

    rongo.mark_read("01-02")
    rongo.mark_read("01-03")
    assert "error" in rongo.run_generation(ask=bad_ask)
    assert rongo.status()["generation"]["error"]
    assert not (rongo.DIR / "notes" / "02-01.md").exists()


def test_disabled(cfg):
    cfg.rongo = {"enabled": False}
    s = rongo.status()
    assert (s["enabled"], s["schedule"], s["generation"]) == (False, rongo.DEFAULT_SCHEDULE, {}) and "current" not in s
    with pytest.raises(PermissionError):
        rongo.mark_read("01-01")
