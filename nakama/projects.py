"""Reference folders per project (config/projects.json, git-ignored).

The chat and the assist prompts read these folders first for project questions and cite file paths
as evidence. Read-only: nothing in them is ever modified. Folders locked in the vault settings
(see vault.py) are excluded even if listed here.

  [{"name": "Project A", "folders": ["C:/work/projA/docs"], "note": "what lives where"}]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

FILE = CONFIG_DIR / "projects.json"


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for p in data if isinstance(data, list) else []:
        if isinstance(p, dict) and p.get("name"):
            out.append({"name": str(p["name"]), "folders": [str(f) for f in p.get("folders") or [] if f],
                        "note": str(p.get("note") or "")})
    return out


def save(rows: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def upsert(name: str, folders: list[str], note: str = "") -> list[dict[str, Any]]:
    name = name.strip()
    if not name:
        raise ValueError("プロジェクト名を入力してください")
    clean = []
    for f in folders:
        f = f.strip().strip('"')
        if not f:
            continue
        p = Path(f).expanduser()
        if not p.is_dir():
            raise ValueError(f"フォルダが見つかりません: {f}")
        if p.resolve() == Path(p.resolve().anchor):
            raise ValueError("ドライブ直下は登録できません（範囲が広すぎるため）")
        clean.append(p.resolve().as_posix())
    rows = [r for r in load() if r["name"] != name]
    rows.append({"name": name, "folders": clean, "note": note.strip()[:1000]})
    save(rows)
    return rows


def delete(name: str) -> list[dict[str, Any]]:
    rows = load()
    keep = [r for r in rows if r["name"] != name]
    if len(keep) == len(rows):
        raise ValueError(f"プロジェクトが見つかりません: {name}")
    save(keep)
    return keep


def read_roots() -> list[Path]:
    """Folders the chat may read (minus vault-locked ones, enforced by the chat guard)."""
    return [Path(f) for p in load() for f in p["folders"] if Path(f).is_dir()]


def prompt_block() -> str:
    rows = load()
    if not rows:
        return ""
    lines = ["## プロジェクトの参照フォルダ（読み取り専用）",
             "プロジェクトに関する質問・作成では、まず該当プロジェクトのフォルダを Grep / Glob / Read で調べ、",
             "根拠にしたファイルは**フルパス**で引用すること（例: `C:/.../手順書.xlsx`）。見つからなければそう書く。"]
    lines.append("大きいフォルダ（直下に .env がある等）はルートを検索すると拒否されるので、下のサブフォルダを path に指定して検索する。")
    for p in rows:
        lines.append(f"- {p['name']}: " + ", ".join(f"`{f}`" for f in p["folders"]) + (f"（{p['note']}）" if p["note"] else ""))
        for f in p["folders"]:
            subs = _subfolders(f)
            if subs:
                lines.append(f"  - `{f}` のサブフォルダ: " + ", ".join(subs))
    return "\n".join(lines)


def _subfolders(folder: str, limit: int = 40) -> list[str]:
    """Top-level folder names (q114), so the chat can aim a search at e.g. <repo>/<subfolder> directly."""
    try:
        names = sorted(d.name for d in Path(folder).iterdir() if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return []
    return names[:limit] + (["…"] if len(names) > limit else [])
