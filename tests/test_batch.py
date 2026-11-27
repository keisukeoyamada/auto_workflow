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


def test_reminders_show_until_done(tmp_path, monkeypatch):
    from datetime import date

    from nakama import reminders

    monkeypatch.setattr(reminders, "FILE", tmp_path / "r.json")
    r = reminders.add("2026-11-27", "締め切り", "https://example.com/x", lead_days=1)
    assert [x["id"] for x in reminders.due(date(2026, 11, 26))] == [r["id"]]   # from the day before
    assert reminders.due(date(2026, 11, 25)) == []
    assert reminders.due(date(2026, 11, 30))                                  # overdue stays until done
    reminders.set_done(r["id"])
    assert reminders.due(date(2026, 11, 30)) == []
    with pytest.raises(ValueError):
        reminders.add("2026-11-27", "x", "javascript:alert(1)")              # links must be http(s)


def test_star_store_and_related_wbs(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "STARS_FILE", tmp_path / "stars.json")
    ui.star_task({"source": "routine", "key": "routine-r001-2026-11", "star": True})
    assert ui._load_stars() == {"routine-r001-2026-11": True}
    with pytest.raises(ValueError):
        ui.star_task({"source": "planner", "key": "../../x", "star": True})
    index = [{"id": "w1", "name": "x_WBS.xlsx", "path_key": "cworkreswinserver02win2025replacexwbsxlsx"}]
    assert [w["id"] for w in ui._related_wbs("★Winserverのアップデート", index)] == ["w1"]
    assert ui._related_wbs("短い data の話", index) == []                     # short / common words do not match


def test_reminder_draft_message_for_polls():
    from datetime import date

    from nakama import reminders

    r = {"date": "2026-11-27", "title": "忘年会の調整さんの締め切り", "link": "https://chouseisan.com/s?h=x"}
    today = reminders.draft_message(r, date(2026, 11, 27))
    assert today.startswith("【リマインド】忘年会の調整さん") and "本日 11/27（金）" in today and "https://chouseisan.com/s?h=x" in today
    assert "出欠" in today
    assert "過ぎていますが" in reminders.draft_message(r, date(2026, 11, 30))
    assert "出欠" not in reminders.draft_message({"date": "2026-12-01", "title": "資料提出", "link": ""}, date(2026, 11, 27))


def test_reminder_assist_asks_for_message():
    from nakama import ui

    p = ui.assist_prompt("notice", {"kind": "reminder", "title": "調整さん", "draft": "【リマインド】…"})["prompt"]
    assert "催促" in p and "こちらでいかがでしょう" in p


def test_chat_actions_need_explicit_approval(tmp_path, monkeypatch):
    from nakama import chat

    monkeypatch.setattr(chat, "CHATS_DIR", tmp_path)
    m = chat.new_chat()
    assert not chat.actions_enabled(m["id"])
    chat.set_actions(m["id"], True)
    assert chat.actions_enabled(m["id"])
    chat.add_user_message(m["id"], "今日の朝会です\n- A さん: 資料作成 12/1")
    assert not chat.approval_ok(m["id"], "今日の朝会です")          # pasting minutes is not an approval
    chat.add_user_message(m["id"], "0 と 1 を昇格して")
    assert chat.approval_ok(m["id"], "0 と 1 を昇格して")
    assert not chat.approval_ok(m["id"], "2 を昇格して")            # words the user did not write
    guard = chat._guard_for_chat(actions=False)
    import asyncio
    denied = asyncio.run(guard({"tool_name": "mcp__kihyo__approve", "tool_input": {}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asyncio.run(chat._guard_for_chat(actions=True)({"tool_name": "mcp__kihyo__approve", "tool_input": {}}, None, None)) == {}


def test_script_message_fills_plan_and_picks_landing(monkeypatch):
    from nakama import scripts

    out = "x\n=====\n報告メール貼り付け用\n=====\n・SIT(計画値$XXXXX)*税抜き\n月末見込み:$2,553.35\n計画値内着地/超過見込み\n\n" \
          "・PRD(計画値$XXXXX)*税抜き\n月末見込み:$16,275.11\n計画値内着地/超過見込み"
    spec = {"id": "t", "command": ["x"], "extract_after": "報告メール貼り付け用", "template": "宛先さん\n{output}\n以上",
            "plan_values": {"SIT": 3000, "PRD": 16000}}
    msg = scripts.build_message(spec, out)
    assert msg.startswith("宛先さん\n・SIT(計画値$3,000)") and "計画値内着地見込み" in msg and "超過見込み" in msg
    assert "=====" not in msg and "x\n" not in msg


def test_script_registry_only_runs_listed(monkeypatch):
    from nakama import scripts

    monkeypatch.setattr(scripts, "registry", lambda: [{"id": "ok", "command": ["echo"]}])
    import pytest
    with pytest.raises(scripts.ScriptError):
        scripts.run("rm-rf")
