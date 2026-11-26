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
    todo_note.save_raw(raw["text"] + "□added\n", raw["mtime"])
    with pytest.raises(todo_note.NoteError):
        todo_note.save_raw("x\n", raw["mtime"])  # the file changed since this editor loaded it


def test_devreq_keeps_open_items_open(tmp_path, monkeypatch):
    monkeypatch.setattr(devreq, "FILE", tmp_path / "r.json")
    a = devreq.add("something")
    assert a["status"] == "open"
    devreq.update(a["id"], status="partial", note="half")
    assert devreq.load()[0]["status"] == "partial"
    with pytest.raises(ValueError):
        devreq.update(a["id"], status="finished")
