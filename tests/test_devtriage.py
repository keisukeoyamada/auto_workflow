"""Offline tests for the 要望 triage (no Claude)."""
import json
from types import SimpleNamespace

import pytest

from nakama import devreq, devtriage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(devreq, "FILE", tmp_path / "requests.json")
    monkeypatch.setattr(devtriage, "STATE", tmp_path / "triage.json")
    fleet = SimpleNamespace(devreq={"triage": {"enabled": True, "max_per_run": 5}}, timezone="Asia/Tokyo", limits={})
    monkeypatch.setattr(devtriage, "load_fleet", lambda: fleet)
    monkeypatch.setattr("nakama.state.record_cost", lambda *a, **k: None)
    monkeypatch.setattr("nakama.state.spend_on", lambda *a, **k: {})
    return fleet


def _answer(questions=()):
    return json.dumps({"summary": "要望タブに〇〇を足す", "size": "s", "questions": list(questions),
                       "spec": "## 目的\nx\n## やること\n- y"}, ensure_ascii=False)


def test_triage_writes_spec_and_asks_back(env):
    a = devreq.add("献立タブがほしい", by="guest", by_name="yuka")
    b = devreq.add("仕事の要望")
    devreq.update(b["id"], status="done")  # finished ones are not read
    prompts = []

    async def ask(prompt):
        prompts.append(prompt)
        return "説明\n" + _answer(["毎日？週単位？"]), 0.01

    assert [r["id"] for r in devtriage.pending()] == [a["id"]]
    assert devtriage.run(ask=ask)["done"] == [a["id"]]
    assert "献立タブがほしい" in prompts[0] and "仕事の要望" not in prompts[0]  # one request per call, nothing else
    row = devreq.load()[0]
    assert row["status"] == "consult" and row["triage"]["size"] == "S" and row["triage"]["questions"]
    assert devtriage.pending() == [] and devtriage.run(ask=ask)["done"] == []  # unchanged: not read again

    devreq.update(a["id"], instruction="週単位で")  # the answer -> triaged again, back to オープン

    async def ask2(prompt):
        assert "週単位で" in prompt and "毎日？週単位？" in prompt
        return _answer(), 0.01

    devtriage.run(ask=ask2)
    row = devreq.load()[0]
    assert row["status"] == "open" and row["triage"]["questions"] == []
    assert devtriage.state()["at"] and devtriage.state()["pending"] == 0


def test_bad_answer_is_reported_and_nothing_saved(env):
    devreq.add("x")

    async def ask(prompt):
        return "すみません", 0.0

    assert "error" in devtriage.run(ask=ask)
    assert devtriage.state()["error"] and "triage" not in devreq.load()[0]


def test_disabled_and_guest_cannot_mark_ready(env):
    a = devreq.add("家族の要望", family=True)
    with pytest.raises(PermissionError):
        devreq.update(a["id"], status="ready", guest=True)
    assert devreq.update(a["id"], status="ready")["status"] == "ready"  # the owner can
    env.devreq = {}
    assert devtriage.run() == {"skipped": True}
    with pytest.raises(PermissionError):
        devtriage.start()


def test_app_map_lists_tabs_and_modules():
    m = devtriage.app_map()
    assert "credit「クレジット精算」" in m and "devreq.py" in m
