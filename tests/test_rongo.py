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


def test_favorites_are_kept_apart_from_reading(cfg):
    import json

    (rongo.DIR).mkdir(parents=True, exist_ok=True)
    (rongo.DIR / "progress.json").write_text(json.dumps({"read": {"01-01": "2026-11-27"}}), encoding="utf-8")  # old format
    (rongo.DIR / "notes").mkdir()
    (rongo.DIR / "notes" / "01-04.md").write_text(
        "## 書き下し文\nX\n\n## 現代語訳\n曾子が言った。\n\n## 語句\n- 曾子\n\n## 教訓\n振り返る習慣。\n\n## 今日の問い\n?\n", encoding="utf-8")
    s = rongo.set_fav("01-04", today=date(2026, 11, 28))
    assert s["current"]["fav_on"] == "2026-11-28" and s["read"] == 1  # reading history kept
    rongo.set_fav("01-02", today=date(2026, 11, 28))
    rongo.mark_read("01-02", today=date(2026, 11, 28))  # reading does not drop the star
    s = rongo.status()
    assert [f["id"] for f in s["favorites"]] == ["01-02", "01-04"]  # in the book's order
    fav4 = s["favorites"][1]
    assert (fav4["translation"], fav4["lesson"]) == ("曾子が言った。", "振り返る習慣。")
    assert s["favorites"][0]["translation"] == ""  # no explanation yet
    assert s["books"][0]["chapters"][3]["fav"] and s["read"] == 2
    rongo.set_fav("01-02", fav=False)
    assert [f["id"] for f in rongo.status()["favorites"]] == ["01-04"]
    with pytest.raises(ValueError):
        rongo.set_fav("99-99")


def test_each_login_reads_on_its_own(cfg):
    import json

    people = [{"role": "owner", "name": "A", "color": "blue"}, {"role": "guest", "name": "B", "color": "pink"}]
    rongo.mark_read("01-01", today=date(2026, 11, 27))  # the owner (default)
    rongo.mark_read("01-01", today=date(2026, 11, 28), user="guest")
    rongo.mark_read("01-02", today=date(2026, 11, 28), user="guest")
    rongo.set_fav("01-02", today=date(2026, 11, 28), user="guest")
    g = rongo.status(user="guest", people=people, today=date(2026, 11, 28))
    o = rongo.status(people=people, today=date(2026, 11, 28))
    assert (g["read"], g["next_id"], g["streak"], o["read"], o["next_id"]) == (2, "01-03", 1, 1, "01-02")
    assert [f["id"] for f in g["favorites"]] == ["01-02"] and o["favorites"] == []  # stars are per login too
    cells = o["books"][0]["chapters"]
    assert (cells[0]["read_by"], cells[1]["read_by"], cells[1]["read"]) == (["owner", "guest"], ["guest"], False)
    assert [(r["role"], r["read"], r["me"]) for r in o["readers"]] == [("owner", 1, True), ("guest", 2, False)]
    assert rongo.status("01-01", people=people)["current"]["read_by"] == [
        {"role": "owner", "on": "2026-11-27"}, {"role": "guest", "on": "2026-11-28"}]
    saved = json.loads((rongo.DIR / "progress.json").read_text(encoding="utf-8"))
    assert saved["read"] == {"01-01": "2026-11-27"} and set(saved["users"]["guest"]["read"]) == {"01-01", "01-02"}
    assert rongo.upcoming(2) == ["01-02", "01-03", "01-04"]  # the next chapters of both readers
