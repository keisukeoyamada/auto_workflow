"""Offline tests for the two-way ToDo note sync (temp file only)."""
import pytest

from nakama import devreq, todo_note

NOTE = "# top\n## ASAP\n□first\n\t■child done\n\t□child open\n★□starred\n△(NICE)□prefixed\n\n## Archive\n□old\n"


@pytest.fixture()
def note(tmp_path, monkeypatch):
    f = tmp_path / "note.md"
    f.write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(todo_note, "note_path", lambda: f)
    monkeypatch.setattr(todo_note, "config", lambda: {"add_section": "ASAP"})
    monkeypatch.setattr(todo_note, "BACKUP_DIR", tmp_path / "bk")
    return f


def test_parse_notation(note):
    items = todo_note.load(include_done=True)["items"]
    assert [i["text"] for i in items] == ["first", "child done", "child open", "starred", "prefixed"]  # Archive ignored
    assert items[1]["done"] and items[1]["parent"] == "first" and items[3]["star"]
    assert [i["text"] for i in todo_note.load()["items"]] == ["first", "child open", "starred", "prefixed"]


def test_set_done_flips_only_the_box(note):
    item = next(i for i in todo_note.load()["items"] if i["text"] == "prefixed")
    todo_note.set_done(item["line"], item["raw"], True)
    text = note.read_text(encoding="utf-8")
    assert "△(NICE)■prefixed" in text and text.replace("■prefixed", "□prefixed") == NOTE  # nothing else changed
    assert list((note.parent / "bk").glob("note.*.md"))                                   # backup kept


def test_set_done_detects_concurrent_edit(note):
    item = todo_note.load()["items"][0]
    note.write_text(NOTE.replace("□first", "□first (edited in Obsidian)"), encoding="utf-8")
    with pytest.raises(todo_note.NoteError):
        todo_note.set_done(item["line"], item["raw"], True)


def test_add_item_goes_to_section_end(note):
    todo_note.add_item("new task", star=True)
    lines = note.read_text(encoding="utf-8").splitlines()
    assert lines.index("★□new task") == lines.index("△(NICE)□prefixed") + 1


def test_save_raw_refuses_stale_editor(note):
    raw = todo_note.read_raw()
    saved = todo_note.save_raw(raw["text"] + "□added\n", raw["mtime"], raw["version"])
    with pytest.raises(todo_note.NoteError):
        # the file changed since this editor loaded it, even within the same filesystem timestamp tick
        todo_note.save_raw("x\n", raw["mtime"], raw["version"])
    todo_note.save_raw("y\n", saved["mtime"], saved["version"])  # the editor that saved last may save again
    assert note.read_text(encoding="utf-8") == "y\n"


def test_devreq_keeps_open_items_open(tmp_path, monkeypatch):
    monkeypatch.setattr(devreq, "FILE", tmp_path / "r.json")
    a = devreq.add("something")
    assert a["status"] == "open"
    devreq.update(a["id"], status="partial", note="half")
    assert devreq.load()[0]["status"] == "partial"
    with pytest.raises(ValueError):
        devreq.update(a["id"], status="finished")


def test_star_toggles_only_the_prefix(note):
    item = next(i for i in todo_note.load()["items"] if i["text"] == "first")
    r = todo_note.set_star(item["line"], item["raw"], True)
    assert r["raw"] == "★□first"
    starred = next(i for i in todo_note.load()["items"] if i["text"] == "first")
    assert starred["star"]
    todo_note.set_star(starred["line"], starred["raw"], False)
    assert note.read_text(encoding="utf-8") == NOTE                      # back to exactly the original


def test_add_child_goes_under_parent_subtree_with_tab(tmp_path, monkeypatch):
    f = tmp_path / "n.md"
    f.write_text("# ASAP\n□親\n\t□子1\n\t\t□孫\n□別\n", encoding="utf-8")
    monkeypatch.setattr(todo_note, "note_path", lambda: f)
    monkeypatch.setattr(todo_note, "BACKUP_DIR", tmp_path / "bk")
    items = todo_note.parse(f.read_text(encoding="utf-8"))
    assert [i["parent_line"] for i in items] == [-1, 1, 2, -1]
    todo_note.add_child(1, "□親", "子2")
    assert f.read_text(encoding="utf-8").splitlines() == ["# ASAP", "□親", "\t□子1", "\t\t□孫", "\t□子2", "□別"]


def test_unified_rows_nest_note_children():
    from nakama import ui

    rows = [{"source": "note", "line": 2, "parent_line": 1, "title": "親 › 子", "text": "子", "status": "open"},
            {"source": "planner", "line": 0, "title": "P", "status": "open"},
            {"source": "note", "line": 1, "parent_line": -1, "title": "親", "text": "親", "status": "open"},
            {"source": "note", "line": 9, "parent_line": 5, "title": "消えた親 › 孤児", "text": "孤児", "status": "open"}]
    out = ui._nest_note_rows(rows)
    assert [(r["title"], r.get("depth")) for r in out] == [("P", None), ("親", 0), ("子", 1), ("消えた親 › 孤児", 0)]
