"""Two-way sync with a hand-written Markdown ToDo note (e.g. an Obsidian file).

Owner's notation (kept as-is when Nakama writes):
  □ text   = to do          ■ text = done          ★ before the box = highlighted
  other prefixes before the box (e.g. "△(NICE TO HAVE)") are preserved; tabs nest items.
  "## heading" starts a section; items under a section named Archive are ignored.

Nakama only ever (a) flips one box character on one line or (b) inserts one new "□ ..." line.
Every write re-reads the file, checks the target line still says what the UI saw, keeps a backup
in logs/note-backup/ and replaces the file atomically. Configure in fleet.yaml:

  todo_note:
    path: "C:/path/to/vault/todo.md"
    add_section: "ASAP"        # where new items go
"""
from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LOGS_DIR, load_fleet

BOXES = ("□", "■")
ITEM_RE = re.compile(r"^(?P<indent>\t*| *)(?P<prefix>[^□■\t]{0,24}?)(?P<box>[□■])(?P<text>.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
BACKUP_DIR = LOGS_DIR / "note-backup"
KEEP_BACKUPS = 30


class NoteError(Exception):
    pass


def config() -> dict[str, Any]:
    cfg = load_fleet().todo_note
    return cfg if isinstance(cfg, dict) else {}


def note_path() -> Path | None:
    raw = config().get("path")
    return Path(raw).expanduser() if raw else None


def _read(path: Path) -> tuple[str, str]:
    """(text, newline) — keep the file's own line endings when writing back."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    return text, ("\r\n" if b"\r\n" in raw else "\n")


def _clean(text: str) -> str:
    """Display text: drop Obsidian link brackets, collapse spaces."""
    t = re.sub(r"\[\[([^\]|]+)\|?([^\]]*)\]\]", lambda m: m.group(2) or m.group(1), text)
    return re.sub(r"\s+", " ", t).strip()


def item_key(section: str, text: str) -> str:
    """Stable id for per-task notes (survives line moves; changes if the text is edited)."""
    raw = unicodedata.normalize("NFKC", f"{section}\x1f{text.strip()}")
    return "note-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def parse(text: str) -> list[dict[str, Any]]:
    items, section, parents, plines = [], "", [], []
    for n, line in enumerate(text.splitlines()):
        h = HEADING_RE.match(line)
        if h and not line.startswith("#" * 7):
            section, parents, plines = h.group(2).strip(), [], []
            continue
        m = ITEM_RE.match(line)
        if not m or section.lower().startswith("archive"):
            continue
        depth = len(m.group("indent").replace("    ", "\t"))
        body = m.group("text")
        parents = parents[:depth] + [_clean(body)]
        plines = plines[:depth] + [n]
        items.append({
            "line": n, "raw": line, "section": section, "depth": depth,
            "done": m.group("box") == "■", "star": "★" in m.group("prefix"),
            "prefix": m.group("prefix").replace("★", "").strip(),
            "text": _clean(body), "parent": parents[-2] if depth and len(parents) > 1 else "",
            "parent_line": plines[-2] if depth and len(plines) > 1 else -1,
            "key": item_key(section, body),
            "due": (DUE_RE.search(body).group(1) if DUE_RE.search(body) else ""),
        })
    return items


def load(include_done: bool = False) -> dict[str, Any]:
    path = note_path()
    if path is None:
        return {"configured": False, "items": []}
    if not path.exists():
        return {"configured": True, "error": f"ファイルが見つかりません: {path}", "items": []}
    text, _ = _read(path)
    items = [i for i in parse(text) if include_done or not i["done"]]
    return {"configured": True, "path": str(path), "items": items, "mtime": path.stat().st_mtime}


def _backup(path: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP_DIR / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
    for old in sorted(BACKUP_DIR.glob(f"{path.stem}.*{path.suffix}"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def _write(path: Path, lines: list[str], newline: str, trailing: bool) -> None:
    _backup(path)
    tmp = path.with_name(path.name + ".nakama.tmp")
    tmp.write_bytes((newline.join(lines) + (newline if trailing else "")).encode("utf-8"))
    tmp.replace(path)  # atomic; Obsidian notices the external change and reloads


def _locate(lines: list[str], line_no: int, raw: str) -> int:
    """The line the UI saw: same index if unchanged, else a unique exact match elsewhere."""
    if 0 <= line_no < len(lines) and lines[line_no] == raw:
        return line_no
    hits = [i for i, l in enumerate(lines) if l == raw]
    if len(hits) == 1:
        return hits[0]
    raise NoteError("ノートがほかで編集されたため、対象の行を特定できませんでした。画面を再読み込みしてください")


def set_done(line_no: int, raw: str, done: bool) -> dict[str, Any]:
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    text, nl = _read(path)
    lines = text.splitlines()
    i = _locate(lines, line_no, raw)
    m = ITEM_RE.match(lines[i])
    if not m:
        raise NoteError("ToDo 行ではありません")
    want = "■" if done else "□"
    if m.group("box") == want:
        return {"line": i, "changed": False}
    pos = m.start("box")
    lines[i] = lines[i][:pos] + want + lines[i][pos + 1:]
    _write(path, lines, nl, text.endswith(("\n", "\r\n")))
    return {"line": i, "changed": True, "raw": lines[i]}


def add_item(text: str, section: str | None = None, star: bool = False) -> dict[str, Any]:
    text = " ".join(text.split())
    if not text:
        raise NoteError("内容が空です")
    if "\n" in text or len(text) > 500:
        raise NoteError("1 行・500 字までにしてください")
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    section = section or str(config().get("add_section") or "")
    body, nl = _read(path)
    lines = body.splitlines()
    new_line = ("★" if star else "") + "□" + text
    start = next((i for i, l in enumerate(lines) if (h := HEADING_RE.match(l)) and h.group(2).strip() == section), None) \
        if section else None
    if start is None:  # no such section: put it on top, under the first heading if any
        first = next((i for i, l in enumerate(lines) if HEADING_RE.match(l)), None)
        at = (first + 1) if first is not None else 0
    else:
        end = next((i for i in range(start + 1, len(lines)) if HEADING_RE.match(lines[i])), len(lines))
        at = end
        while at - 1 > start and not lines[at - 1].strip():  # before trailing blank lines of the section
            at -= 1
    lines.insert(at, new_line)
    _write(path, lines, nl, body.endswith(("\n", "\r\n")))
    return {"line": at, "raw": new_line, "section": section}


def add_child(parent_line: int, parent_raw: str, text: str, star: bool = False) -> dict[str, Any]:
    """Add □text one level below an existing item, after its last descendant (keeps the file's indent style)."""
    text = " ".join(text.split())
    if not text:
        raise NoteError("内容が空です")
    if len(text) > 500:
        raise NoteError("500 字までにしてください")
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    body, nl = _read(path)
    lines = body.splitlines()
    i = _locate(lines, parent_line, parent_raw)
    m = ITEM_RE.match(lines[i])
    if not m:
        raise NoteError("親が ToDo 行ではありません")
    base = m.group("indent")
    width = lambda ind: len(ind.replace("    ", "\t"))  # noqa: E731 - depth in levels
    at, child_indent = i + 1, ""
    while at < len(lines):  # skip the parent's existing subtree (deeper items and blank-free continuation)
        cm = ITEM_RE.match(lines[at])
        if not cm or width(cm.group("indent")) <= width(base) or HEADING_RE.match(lines[at]):
            break
        if width(cm.group("indent")) == width(base) + 1 and not child_indent:
            child_indent = cm.group("indent")
        at += 1
    if not child_indent:  # first child: tab if the file uses tabs (Obsidian default), else 4 spaces
        child_indent = base + ("\t" if ("\t" in base or any(l.startswith("\t") for l in lines) or not base) else "    ")
    new_line = child_indent + ("★" if star else "") + "□" + text
    lines.insert(at, new_line)
    _write(path, lines, nl, body.endswith(("\n", "\r\n")))
    return {"line": at, "raw": new_line}


def _version(path) -> str:
    """A fingerprint of the file's bytes. Modification times are too coarse for this on Linux (writes a few
    milliseconds apart can share one), so "did it change since the editor loaded it?" compares contents."""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def read_raw() -> dict[str, Any]:
    """Whole note for the free-form editor (same file Obsidian edits)."""
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    text, nl = _read(path)
    return {"path": str(path), "text": text.replace("\r\n", "\n"), "mtime": path.stat().st_mtime,
            "version": _version(path), "crlf": nl == "\r\n"}


def save_raw(text: str, expected_mtime: float, expected_version: str = "") -> dict[str, Any]:
    """Save the editor's text unless the file changed since it was loaded (e.g. edited in Obsidian).
    Prefer `expected_version` (contents); the mtime check stays for older pages that only send mtime."""
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    changed = (_version(path) != expected_version) if expected_version else abs(path.stat().st_mtime - float(expected_mtime)) > 0.001
    if changed:
        raise NoteError("ノートが別の場所（Obsidian など）で更新されています。再読み込みしてから編集してください")
    if len(text) > 2_000_000:
        raise NoteError("大きすぎます")
    _, nl = _read(path)
    lines = text.replace("\r\n", "\n").split("\n")
    trailing = text.endswith("\n")
    if trailing:
        lines = lines[:-1]
    _write(path, lines, nl, trailing)
    return {"mtime": path.stat().st_mtime, "version": _version(path), "lines": len(lines)}


def set_due(line_no: int, raw: str, due: str) -> dict[str, Any]:
    """Write/replace/remove the "📅 YYYY-MM-DD" marker on one line (Obsidian Tasks style)."""
    if due:
        datetime.strptime(due, "%Y-%m-%d")
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    text, nl = _read(path)
    lines = text.splitlines()
    i = _locate(lines, line_no, raw)
    line = DUE_RE.sub("", lines[i]).rstrip()
    lines[i] = f"{line} 📅 {due}" if due else line
    _write(path, lines, nl, text.endswith(("\n", "\r\n")))
    return {"line": i, "raw": lines[i]}


def set_star(line_no: int, raw: str, star: bool) -> dict[str, Any]:
    """Add/remove the owner's ★ right before the box on one line (same notation as in Obsidian)."""
    path = note_path()
    if path is None or not path.exists():
        raise NoteError("todo_note.path が未設定か、ファイルがありません")
    text, nl = _read(path)
    lines = text.splitlines()
    i = _locate(lines, line_no, raw)
    m = ITEM_RE.match(lines[i])
    if not m:
        raise NoteError("ToDo 行ではありません")
    prefix = m.group("prefix")
    has = "★" in prefix
    if has == star:
        return {"line": i, "raw": lines[i], "changed": False}
    new_prefix = prefix.replace("★", "", 1) if has else "★" + prefix
    lines[i] = m.group("indent") + new_prefix + m.group("box") + m.group("text")
    _write(path, lines, nl, text.endswith(("\n", "\r\n")))
    return {"line": i, "raw": lines[i], "changed": True}
