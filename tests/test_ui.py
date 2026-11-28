"""Offline tests for the dashboard server (no API calls)."""
import http.client
import json
import threading

import pytest

from nakama import ui
from nakama.config import WORKSPACE_DIR


def test_safe_path_blocks_secrets_and_traversal():
    for bad in (".env", "config/fleet.yaml", "workspace/../.env", "agents/henji/AGENT.md"):
        with pytest.raises(PermissionError):
            ui._safe_path(bad)
    assert ui._safe_path("knowledge/rulebook.md").name == "rulebook.md"


def test_set_status_only_on_allowed_folders(tmp_path, monkeypatch):
    folder = WORKSPACE_DIR / "action_items"
    folder.mkdir(parents=True, exist_ok=True)
    item = folder / "zz-test-item.md"
    item.write_text("---\nstatus: open\nsource: test\n---\nbody\n", encoding="utf-8")
    try:
        ui.set_status("workspace/action_items/zz-test-item.md", "done")
        assert "status: done" in item.read_text(encoding="utf-8")
        with pytest.raises(PermissionError):
            ui.set_status("workspace/action_items/zz-test-item.md", "hacked")
    finally:
        item.unlink(missing_ok=True)
    with pytest.raises(PermissionError):
        ui.set_status("knowledge/rulebook.md", "done")


@pytest.fixture()
def server():
    srv = ui.make_server("127.0.0.1", 0)  # port 0 = pick a free port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _get(srv, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)  # first read of a synced folder can be slow
    conn.request("GET", path, headers={"Host": "localhost", **(headers or {})})
    res = conn.getresponse()
    return res.status, res.read()


def test_api_requires_token_and_local_host(server):
    token = server.RequestHandlerClass.token
    assert _get(server, "/api/overview")[0] == 403                                   # no token
    assert _get(server, "/api/overview", {"Host": "evil.example", "X-Nakama-Token": token})[0] == 403
    status, body = _get(server, "/api/overview", {"X-Nakama-Token": token})
    assert status == 200 and "agents" in json.loads(body)
    status, body = _get(server, "/")
    assert status == 200 and token.encode() in body                                   # page carries the token
    assert _get(server, "/api/file?path=.env", {"X-Nakama-Token": token})[0] == 403
    status, body = _get(server, "/api/guide", {"X-Nakama-Token": token})
    assert status == 200 and "使い方ガイド" in json.loads(body)["content"]


def test_attendance_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "ATTENDANCE_DIR", tmp_path)
    monkeypatch.setattr(ui, "MINE_MD", tmp_path / "mine.md")
    monkeypatch.setattr(ui, "ROOT", tmp_path.parent)
    ui.add_my_attendance("2026-11-27", "有休", "", "line1\nline2 | pipe")
    text = (tmp_path / "mine.md").read_text(encoding="utf-8")
    assert "| 2026-11-27 | 有休 | 終日 | line1 line2 / pipe |" in text   # one row, no table breakage
    with pytest.raises(ValueError):
        ui.add_my_attendance("11/27", "有休")
    rel = ui.save_paste("teams", "  11/27 有休  ")
    saved = tmp_path.parent / rel
    assert saved.parent.name == "sources" and "source: teams" in saved.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        ui.save_paste("slack", "x")


def test_attendance_calendar_tolerates_bad_agent_output(tmp_path, monkeypatch):
    from datetime import date

    cal = tmp_path / "calendar.json"
    monkeypatch.setattr(ui, "CALENDAR_JSON", cal)
    assert ui.attendance_calendar()["error"]                    # missing file -> message, no crash
    cal.write_text("{not json", encoding="utf-8")
    assert "読めません" in ui.attendance_calendar()["error"]
    today = ui.attendance_calendar()["days"][0]["date"]
    cal.write_text(json.dumps({"members": [{"name": "A", "group": "g"}], "entries": [
        {"date": today, "member": "B", "kind": "有休", "hours": "終日", "level": "off"},
        {"date": "1999-01-01", "member": "A", "kind": "有休", "level": "off"},   # outside the window
        "garbage",
    ]}), encoding="utf-8")
    out = ui.attendance_calendar()
    assert len(out["days"]) == 14 and out["error"] is None
    assert [m["name"] for m in out["members"]] == ["A", "B"]   # unknown member still gets a row
    assert len(out["entries"]) == 1


def test_settings_whitelist_and_validation():
    from nakama.config import ROOT

    with pytest.raises(PermissionError):
        ui._settings_path(".env")
    with pytest.raises(PermissionError):
        ui._settings_path("config/mcp.json")
    good = (ROOT / "config" / "fleet.example.yaml").read_text(encoding="utf-8")
    ui._validate_setting("config/fleet.yaml", good)                       # example is valid
    with pytest.raises(ValueError):
        ui._validate_setting("config/fleet.yaml", "agents: [oops")       # broken YAML
    with pytest.raises(Exception):
        ui._validate_setting("config/fleet.yaml", good.replace('"30 7 * * mon-fri"', '"99 99 * * *"'))
    with pytest.raises(ValueError):
        ui._validate_setting("config/team.yaml", "members: nope")


def test_samples_are_recognised():
    assert (WORKSPACE_DIR / "crm" / "leads" / "L-0001.json").resolve() in ui._sample_paths()


def test_calendar_window_offset():
    out = ui.attendance_calendar(days=61, offset=-30)
    assert len(out["days"]) == 61 and out["days"][0]["date"] < out["days"][30]["date"]


def test_scheduler_has_reload_job():
    from nakama import scheduler

    ids = [j.id for j in scheduler.build_scheduler().get_jobs()]
    assert scheduler.RELOAD_JOB_ID in ids and any(i.startswith("kintai#") for i in ids)


def test_notifications_from_decisions_and_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "DECISIONS_DIR", tmp_path / "d")
    monkeypatch.setattr(ui, "ACKS_FILE", tmp_path / "acks.json")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "2026-11-26_am.json").write_text(json.dumps({
        "date": "2026-11-26", "cycle": "am", "outcome": "promoted",
        "created": [{"title": "A", "assignee": "me"}],
        "dropped": [{"title": "B", "kind": "unclear", "reason": "r"}, {"title": "C", "kind": "dup"}, {"title": "D"}, 42],
        "needs_attention": "not-a-list",
    }), encoding="utf-8")
    kihyo = lambda: [i["kind"] for i in ui.notifications()["items"] if i["group"].startswith("Kihyo")]  # noqa: E731
    assert kihyo() == []                                             # q120: ticketing results stay in the report
    monkeypatch.setattr(ui, "KIHYO_DETAIL_IN_FEED", True)
    assert kihyo() == ["created", "unclear", "dup"]                  # dups folded into one row, junk ignored
    first = ui.notifications()["items"][0]
    ui.ack_notifications([first["id"], "not-an-id"])
    after = ui.notifications()
    assert first["id"] not in [i["id"] for i in after["items"]]      # OK hides it from the feed
    assert any(i["id"] == first["id"] and i["acked"] for i in ui.notifications(include_acked=True)["items"])


def test_meeting_prompt_and_reference_pdf(tmp_path, monkeypatch):
    assert ui._prompt_section("x\n## プロンプト\n＋＋\n本文\n\n## 実行例\nfoo") == "本文"
    assert ui._prompt_section("no heading") == "no heading"
    monkeypatch.setattr(ui, "REFERENCES_DIR", tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    assert [r["name"] for r in ui.reference_pdfs()] == ["a.pdf"]
    assert ui._reference_path("a.pdf").name == "a.pdf"
    for bad in ("../a.pdf", "sub/a.pdf", "a.txt", ""):
        with pytest.raises(PermissionError):
            ui._reference_path(bad)


def test_schedule_today_lists_planned_times():
    sc = ui.schedule_today()
    kintai = next(a for a in sc["agents"] if a["name"] == "kintai")
    assert all(len(t) == 5 for t in kintai["planned"]) and kintai["trigger"] is True


def test_files_derived_from_samples_are_samples(tmp_path):
    tokens = ui._sample_tokens()
    derived = tmp_path / "D-M-0001.md"                         # an agent's draft for sample mail M-0001
    derived.write_text("---\nthread_id: x\n---\nbody", encoding="utf-8")
    via_body = tmp_path / "2026-11-26-nokori-x.md"
    via_body.write_text("---\nrelated: O-0001\n---\n", encoding="utf-8")
    real = tmp_path / "D-M-9999.md"
    real.write_text("---\nthread_id: T-9999\n---\n", encoding="utf-8")
    assert ui._is_sample(derived, set(), tokens) and ui._is_sample(via_body, set(), tokens)
    assert not ui._is_sample(real, set(), tokens)


def test_memo_suggestion_apply_and_dismiss(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "MEMOS_DIR", tmp_path / "memos")
    monkeypatch.setattr(ui, "SUGGEST_DIR", tmp_path / "sug")
    monkeypatch.setattr(ui, "APPLIED_FILE", tmp_path / "applied.json")
    monkeypatch.setattr(ui, "MINE_MD", tmp_path / "mine.md")
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "set_status", lambda rel, st: calls.append((rel, st)))
    calls = []
    (tmp_path / "memos").mkdir(); (tmp_path / "sug").mkdir()
    stem = "20261126-210000"
    (tmp_path / "memos" / f"{stem}.md").write_text("---\nstatus: triaged\n---\nmemo", encoding="utf-8")
    (tmp_path / "sug" / f"{stem}.json").write_text(json.dumps({"items": [
        {"route": "attendance", "summary": "s", "who": "self", "date": "2026-12-03", "kind": "午後休"},
        {"route": "evil", "summary": "unknown route is ignored"},
        {"route": "keep", "summary": "k"},
    ]}), encoding="utf-8")
    d = ui.memo_detail(stem)
    assert [i["route"] for i in d["items"]] == ["attendance", "keep"]
    ui.apply_memo_item(stem, 0, "apply")
    assert "2026-12-03 | 午後休" in (tmp_path / "mine.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        ui.apply_memo_item(stem, 0, "apply")                    # never twice
    ui.apply_memo_item(stem, 1, "dismiss")
    assert calls == [(f"workspace/memos/{stem}.md", "done")]  # all handled -> memo done


def test_chat_guard_is_read_only_and_hides_env():
    import asyncio

    from nakama import chat
    from nakama.config import ROOT

    guard = chat._guard_for_chat()
    decide = lambda tool, **inp: asyncio.run(guard({"tool_name": tool, "tool_input": inp}, None, None))
    denied = lambda r: r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    assert not denied(decide("Read", file_path=str(ROOT / "README.md")))
    assert denied(decide("Read", file_path=str(ROOT / ".env")))
    assert denied(decide("Grep", pattern="TOKEN", path=str(ROOT)))          # root search would include .env
    assert not denied(decide("Grep", pattern="def", path=str(ROOT / "nakama")))
    assert denied(decide("Read", file_path="C:/Windows/win.ini"))            # outside the repo
    assert denied(decide("Write", file_path=str(ROOT / "x.md"), content="x"))  # read-only


def test_build_info_for_the_footer(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    assert ui.build_info()["commit"] == "" and ui.build_info()["started_at"]  # manual/local build
    (tmp_path / "build-info.json").write_text(
        '{"commit": "7c1d2ab0000000000000000000000000deadbeef", "commit_date": "2026-11-28T14:05:00+09:00",'
        ' "built_at": "2026-11-28T05:07:00Z"}', encoding="utf-8")
    info = ui.build_info()
    assert info["commit"].startswith("7c1d2ab") and info["commit_date"].startswith("2026-11-28")
    assert info["repo"] == ""
    for repo, shown in (("https://github.com/owner/repo", "https://github.com/owner/repo"),
                        ('javascript:alert(1)', "")):  # only a plain GitHub repository URL is used for links
        (tmp_path / "build-info.json").write_text(f'{{"commit": "x", "repo": "{repo}"}}', encoding="utf-8")
        assert ui.build_info()["repo"] == shown


def test_system_jobs_list_only_enabled_ones(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from nakama import devimpl, devreq, devtriage, rongo

    fleet = SimpleNamespace(rongo={"enabled": False}, devreq={"triage": {"enabled": True}, "implement": {"enabled": True}},
                            timezone="Asia/Tokyo", limits={})
    for m in (rongo, devtriage, devimpl):
        monkeypatch.setattr(m, "load_fleet", lambda: fleet)
    monkeypatch.setattr(devreq, "FILE", tmp_path / "r.json")
    monkeypatch.setattr(devtriage, "STATE", tmp_path / "triage.json")
    monkeypatch.setattr(devimpl, "QUEUE", tmp_path / "devq")
    (tmp_path / "triage.json").write_text(json.dumps({"running": True}))
    jobs = {j["id"]: j for j in ui.system_jobs()}
    assert set(jobs) == {"devtriage", "devbox"}  # rongo is off
    assert jobs["devtriage"]["running"] and jobs["devbox"]["error"]  # no heartbeat from the devbox yet


def test_deploy_state_for_the_banner(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(ui, "LOGS_DIR", tmp_path)
    assert ui.deploy_state() == {}
    f = tmp_path / "autodeploy-state.json"
    f.write_text(json.dumps({"state": "deploying", "to": "abcdef1234", "started": 1000, "seconds": 240}))
    assert ui.deploy_state(now=1060) == {"deploying": True, "to": "abcdef1", "remaining": 180}
    assert ui.deploy_state(now=1500)["remaining"] == 0  # past the estimate: "まもなく"
    assert ui.deploy_state(now=1000 + 3600) == {}  # stale (the script died)
    f.write_text(json.dumps({"state": "done", "to": "x", "started": 1000, "seconds": 200}))
    assert ui.deploy_state(now=1100) == {}
