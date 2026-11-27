"""Recurring-meeting decks: find past decks, keep templates, extract text for style learning and review.

Configure in fleet.yaml (git-ignored):
  decks:
    meetings:
      - {name: "Weekly sync", folder: "C:/path/to/decks/weekly", learn_last: 6}

- extract(): text of every slide (title, body, tables, notes) -> workspace/decks/extract/<meeting>/<file>.md,
  so the Tensaku agent can read decks with its normal Read tool (it never gets the .pptx itself).
- save_template(): copy the newest deck of a meeting into workspace/decks/templates/<meeting>/.
Nothing in the meeting folders is ever modified.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import WORKSPACE_DIR, load_fleet

DECKS_DIR = WORKSPACE_DIR / "decks"
EXTRACT_DIR = DECKS_DIR / "extract"
TEMPLATE_DIR = DECKS_DIR / "templates"
REVIEW_DIR = DECKS_DIR / "reviews"
SKIP = re.compile(r"(~\$|bak|backup)", re.I)


class DeckError(Exception):
    pass


def meetings() -> list[dict[str, Any]]:
    cfg = getattr(load_fleet(), "decks", None) or {}
    out = []
    for m in cfg.get("meetings") or []:
        if isinstance(m, dict) and m.get("name") and m.get("folder"):
            out.append({"name": str(m["name"]), "folder": str(m["folder"]), "learn_last": int(m.get("learn_last") or 6)})
    return out


def slug(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_") or hashlib.sha1(name.encode()).hexdigest()[:8]


def meeting(name: str) -> dict[str, Any]:
    m = next((x for x in meetings() if x["name"] == name), None)
    if m is None:
        raise DeckError(f"定例が登録されていません: {name}（fleet.yaml の decks.meetings）")
    return m


def list_decks(name: str) -> list[Path]:
    folder = Path(meeting(name)["folder"])
    if not folder.is_dir():
        raise DeckError(f"フォルダが見つかりません: {folder}")
    files = [p for p in folder.rglob("*.pptx") if not SKIP.search(p.name)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def deck_text(path: Path) -> str:
    """Markdown-ish text of a deck: one section per slide (layout, title, text, tables, notes)."""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise DeckError("python-pptx が必要です（pip install python-pptx）") from exc
    import io

    # Open from bytes: some synced file names (decomposed kana, cloud placeholders) trip python-pptx's path check.
    try:
        prs = Presentation(io.BytesIO(Path(path).read_bytes()))
    except Exception as exc:  # noqa: BLE001 - corrupt / password-protected / still syncing
        raise DeckError(f"開けません: {Path(path).name}（{exc.__class__.__name__}）") from exc
    out = [f"# {path.name}", f"- スライド数: {len(prs.slides)}"]
    for i, slide in enumerate(prs.slides, start=1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        out.append(f"\n## スライド {i}: {title or '（タイトルなし）'}  〔レイアウト: {slide.slide_layout.name}〕")
        for shape in slide.shapes:
            if shape == slide.shapes.title:
                continue
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in para.runs).strip()
                    if text:
                        out.append("  " * para.level + f"- {text}")
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    out.append("| " + " | ".join(c.text.replace("\n", " ").strip() for c in row.cells) + " |")
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append("> ノート: " + slide.notes_slide.notes_text_frame.text.strip().replace("\n", " "))
    return "\n".join(out) + "\n"


def extract(name: str, count: int | None = None) -> list[str]:
    """Extract the newest `count` decks of a meeting; returns repo-relative paths of the extracts."""
    m = meeting(name)
    dest = EXTRACT_DIR / slug(name)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for p in list_decks(name):
        if len(out) >= (count or m["learn_last"]):
            break
        target = dest / (p.stem + ".md")
        try:
            if not target.exists() or target.stat().st_mtime < p.stat().st_mtime:
                target.write_text(f"<!-- 出典: {p} -->\n" + deck_text(p), encoding="utf-8")
        except DeckError:
            continue  # a deck that cannot be opened (e.g. still being edited): take the next one
        out.append(target.relative_to(WORKSPACE_DIR.parent).as_posix())
    return out


def extract_file(path: str) -> str:
    """Extract one deck the owner wants reviewed (any .pptx path) into workspace/decks/extract/_review/."""
    p = Path(path.strip().strip('"')).expanduser()
    if not p.is_file() or p.suffix.lower() != ".pptx":
        raise DeckError("pptx ファイルのパスを指定してください")
    dest = EXTRACT_DIR / "_review"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / (p.stem + ".md")
    target.write_text(f"<!-- 出典: {p} -->\n" + deck_text(p), encoding="utf-8")
    return target.relative_to(WORKSPACE_DIR.parent).as_posix()


def save_template(name: str) -> str:
    decks = list_decks(name)
    if not decks:
        raise DeckError("資料が見つかりません")
    src = decks[0]
    dest = TEMPLATE_DIR / slug(name)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / src.name
    shutil.copy2(src, target)
    (dest / "source.json").write_text(json.dumps({"source": str(src), "copied_at": datetime.now().isoformat(timespec="minutes")},
                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    return target.relative_to(WORKSPACE_DIR.parent).as_posix()


def overview() -> list[dict[str, Any]]:
    out = []
    for m in meetings():
        row = {"name": m["name"], "folder": m["folder"], "slug": slug(m["name"])}
        try:
            decks = list_decks(m["name"])
            row |= {"count": len(decks), "latest": decks[0].name if decks else "",
                    "latest_at": datetime.fromtimestamp(decks[0].stat().st_mtime).strftime("%Y-%m-%d") if decks else ""}
        except DeckError as exc:
            row["error"] = str(exc)
        tdir = TEMPLATE_DIR / slug(m["name"])
        row["templates"] = sorted(p.name for p in tdir.glob("*.pptx")) if tdir.exists() else []
        style = WORKSPACE_DIR.parent / "knowledge" / "decks" / f"{slug(m['name'])}-style.md"
        row["style"] = style.relative_to(WORKSPACE_DIR.parent).as_posix() if style.exists() else ""
        out.append(row)
    return out


def reviews() -> list[dict[str, Any]]:
    return [{"path": p.relative_to(WORKSPACE_DIR.parent).as_posix(), "name": p.name, "mtime": p.stat().st_mtime}
            for p in sorted(REVIEW_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]]
