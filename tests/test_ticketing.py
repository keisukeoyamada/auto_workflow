"""Offline tests for the ticketing helper tools (temp folders only; no Power Automate involved)."""
import json

import pytest

from nakama.guard import check_tool_call
from nakama.config import AgentSpec
from nakama.helpers import ticketing as tk


@pytest.fixture()
def paths(tmp_path):
    p = tk.Paths(tmp_path)
    for d in (p.draft, p.archive, p.state / "planner_state_am", p.logs):
        d.mkdir(parents=True)
    state = [
        {"id": "AAA-open", "title": "Report fix", "percentComplete": 50, "due": "2026-11-30"},
        {"id": "BBB-done", "title": "Master check", "percentComplete": 100, "due": ""},
        {"id": "CCC-done", "title": "Master check", "percentComplete": 100, "due": ""},
    ]
    # Exports start with a BOM; the tools must cope with it.
    (p.state / "planner_state_am" / "planner_state_2026-11-26.json").write_text(json.dumps(state), encoding="utf-8-sig")
    return p


def _draft(p, **over):
    data = {"generated_at": "2026-11-26T12:00:00", "cycle": "am", "status": "draft", "review_required": False,
            "source_file": "x.html", "tasks": [{"title": "t"}], "close_candidates": []} | over
    (p.draft / "tickets_2026-11-26_am.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_summary_lists_only_open_and_grep_finds_done(paths):
    out = tk.planner_state_summary(paths, "planner_state/planner_state_am/planner_state_2026-11-26.json")
    assert out.startswith("total=3 open=1 done=2") and "AAA-open" in out and "BBB-done" not in out
    hits = tk.planner_state_grep(paths, "planner_state/planner_state_am/planner_state_2026-11-26.json", "master", "done")
    assert hits.startswith("matches=2")


def test_paths_cannot_escape_base(paths):
    with pytest.raises(tk.TicketingError):
        tk.planner_state_summary(paths, "../outside.json")


def test_promote_strips_draft_keys_and_is_create_only(paths):
    _draft(paths, fallback_note="x")
    msg = tk.promote_ticket(paths, "2026-11-26", "am")
    dest = paths.tickets / "tickets_2026-11-26.json"
    raw = dest.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")                      # BOM-less
    data = json.loads(raw)
    assert "status" not in data and "fallback_note" not in data and data["cycle"] == "am"
    assert "tasks=1" in msg and not list(paths.draft.glob(".promote_*"))  # temp file cleaned up
    with pytest.raises(tk.TicketingError, match="already exists"):
        tk.promote_ticket(paths, "2026-11-26", "am")                 # never overwrite (PA would not fire)


@pytest.mark.parametrize("over", [{"review_required": True}, {"cycle": "pm"}, {"tasks": None}])
def test_promote_refuses_unsafe_drafts(paths, over):
    _draft(paths, **over)
    with pytest.raises(tk.TicketingError):
        tk.promote_ticket(paths, "2026-11-26", "am")
    assert not (paths.tickets / "tickets_2026-11-26.json").exists()


def test_archive_moves_only_promoted_files(paths):
    (paths.tickets / "tickets_2026-11-26.json").write_text("{}", encoding="utf-8")
    tk.archive_ticket(paths, "tickets_2026-11-26.json", "tickets_2026-11-26_am_PROCESSED.json")
    assert (paths.archive / "tickets_2026-11-26_am_PROCESSED.json").exists()
    with pytest.raises(tk.TicketingError):
        tk.archive_ticket(paths, "../../x.json", "y.json")


def test_guard_allows_draft_and_logs_but_not_tickets_root(paths):
    spec = AgentSpec(name="kihyo", model="m", writes=["workspace/reports"])
    extra = dict(extra_read=paths.read_roots, extra_write=paths.write_roots)
    ok = lambda f: check_tool_call(spec, "Write", {"file_path": str(f)}, **extra) is None
    assert ok(paths.draft / "tickets_2026-11-26_am.json") and ok(paths.logs / "report.md")
    assert not ok(paths.tickets / "tickets_2026-11-26.json")         # only promote_ticket may create this
    assert check_tool_call(spec, "Read", {"file_path": str(paths.base / "roster.json")}, **extra) is None


def test_ensure_readable_reports_missing(paths, monkeypatch):
    monkeypatch.setattr(tk, "_onedrive_running", lambda: True)
    out = tk.ensure_readable(paths, ["planner_state/planner_state_am/planner_state_2026-11-26.json", "nope.html"], timeout_sec=5)
    assert "ok\tplanner_state" in out and "missing\tnope.html" in out


# ---------- manual review / approval ----------
from nakama.helpers import ticket_review as tr


def _roster(p):
    (p.base / "roster").mkdir()
    (p.base / "roster" / "roster.json").write_text(json.dumps({"members": [{"id": "G1", "displayName": "Me"}]}), encoding="utf-8")


def test_validate_flags_duplicates_and_bad_ids(paths):
    _roster(paths)
    draft = {"tasks": [
        {"title": "Report fix 🟢11/26朝会で起票", "assignee_id": "G1", "due": "2026-12-01", "priority": "high"},  # open card
        {"title": "Master check", "assignee_id": "G1"},          # finished twice -> recurring
        {"title": "New thing", "assignee_id": "NOPE"},           # unknown assignee -> error
        {"title": "New thing", "assignee_id": "G1", "due": "12/1"},  # dup in draft + bad due -> error
    ], "close_candidates": [{"planner_task_id": "AAA-open", "note": "📝 x"}, {"planner_task_id": "made-up", "note": "x"}]}
    v = tr.validate(paths, draft)
    assert not v["errors"]
    t = v["tasks"]
    assert any("未完了の既存カード" in w for w in t[0]["warnings"]) and not t[0]["errors"]
    assert any("完了済みに同名カードが 2 枚" in w for w in t[1]["warnings"])
    assert any("roster" in e for e in t[2]["errors"])
    assert any("重複" in e for e in t[3]["errors"]) and any("due" in e for e in t[3]["errors"])
    assert not v["closes"][0]["errors"] and v["closes"][1]["errors"]


def test_promote_manual_only_selected_and_never_twice(paths):
    _roster(paths)
    draft = {"tasks": [{"title": "Brand new A", "assignee_id": "G1"}, {"title": "Brand new B", "assignee_id": "G1"}],
             "close_candidates": []}
    r = tr.promote_manual(paths, draft, [1], [], "20261126-200000")
    data = json.loads((paths.tickets / r["promoted"]).read_text(encoding="utf-8"))
    assert [t["title"] for t in data["tasks"]] == ["Brand new B"] and data["cycle"] == "manual"
    # the file is still unprocessed (no newer planner_state): a second promotion must be refused
    with pytest.raises(tk.TicketingError, match="処理されていない"):
        tr.promote_manual(paths, draft, [0], [], "20261126-200100")
    with pytest.raises(tk.TicketingError, match="エラー"):
        tr.promote_manual(paths, {"tasks": [{"title": "x", "assignee_id": "BAD"}], "close_candidates": []}, [0], [], "j")


def test_scheduled_promotion_blocked_by_validation_gate(paths):
    _roster(paths)
    # "Report fix" is an open card in planner_state -> an unattended promotion must refuse
    _draft(paths, tasks=[{"title": "Report fix 🟢11/26朝会で起票", "assignee_id": "G1"}])
    with pytest.raises(tk.TicketingError, match="validation gate"):
        tk.promote_ticket(paths, "2026-11-26", "am")
    assert not (paths.tickets / "tickets_2026-11-26.json").exists()
    _draft(paths, tasks=[{"title": "x", "assignee_id": "UNKNOWN"}])     # unknown assignee id
    with pytest.raises(tk.TicketingError, match="roster"):
        tk.promote_ticket(paths, "2026-11-26", "am")
