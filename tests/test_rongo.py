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


def _fake_fetch(url):
    slug = url.rsplit("/", 1)[1]
    n = 3 if slug == "xue-er" else 1
    return {"title": slug, "fulltext": [f"{slug} 子曰 第{i}章" for i in range(1, n + 1)] + [""]}


def _import(cfg):
    return rongo.import_source(fetch=_fake_fetch, pause=0)


def test_import_numbers_books_and_chapters(cfg):
    assert _import(cfg) == {"chapters": 3 + 19, "books": 20}
    chs = rongo.chapters()
    assert [c["id"] for c in chs[:4]] == ["01-01", "01-02", "01-03", "02-01"]
    assert chs[0]["book_name"] == "学而" and chs[-1]["book_name"] == "堯曰" and chs[-1]["id"] == "20-01"


def test_import_is_all_or_nothing_and_strict(cfg):
    def broken(url):
        return {"unexpected": True} if url.endswith("ba-yi") else _fake_fetch(url)

    with pytest.raises(ValueError, match="想定と違います"):
        rongo.import_source(fetch=broken, pause=0)
    assert rongo.chapters() == []

    with pytest.raises(ValueError, match="エラーを返しました"):
        rongo.import_source(fetch=lambda url: {"error": {"code": "ERR_REQUEST_LIMIT", "description": "limit"}}, pause=0)


def test_progress_streak_and_next_chapter(cfg):
    _import(cfg)
    s = rongo.status(today=date(2026, 11, 27))
    assert (s["read"], s["total"], s["next_id"], s["current"]["id"]) == (0, 22, "01-01", "01-01")
    rongo.mark_read("01-01", today=date(2026, 11, 25))
    rongo.mark_read("01-02", today=date(2026, 11, 26))
    s = rongo.status(today=date(2026, 11, 27))
    assert s["streak"] == 2 and s["read_today"] == 0 and s["next_id"] == "01-03"  # not read today yet: run continues
    rongo.mark_read("01-03", today=date(2026, 11, 27))
    s = rongo.status(today=date(2026, 11, 27))
    assert s["streak"] == 3 and s["read"] == 3 and s["books"][0]["chapters"][2]["read"]
    rongo.mark_read("01-03", read=False)
    assert rongo.status(today=date(2026, 11, 27))["streak"] == 2
    assert rongo.status("02-01")["current"]["id"] == "02-01"
    with pytest.raises(ValueError):
        rongo.mark_read("99-99")


def test_generation_writes_notes_ahead(cfg):
    _import(cfg)
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
    assert rongo.status() == {"enabled": False, "schedule": rongo.DEFAULT_SCHEDULE, "generation": {}}
    with pytest.raises(PermissionError):
        rongo.import_source(fetch=_fake_fetch, pause=0)
