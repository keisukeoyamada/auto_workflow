"""A small tracker for requests to Nakama itself (what the owner asked for and where it stands).

Stored in workspace/dev/requests.json (git-ignored):
  [{id, text, status: open|partial|consult|done|hold, instruction, note, change, usage, created, updated}]
  instruction = the owner's direction for this item (answers to 要相談, priorities, "this is fine")
  change = what was changed and how (release note), usage = how to use it
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import WORKSPACE_DIR

FILE = WORKSPACE_DIR / "dev" / "requests.json"
STATUSES = ("open", "partial", "consult", "done", "hold")  # consult = needs a decision / not feasible as asked


def load() -> list[dict[str, Any]]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id")] if isinstance(data, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def add(text: str, status: str = "open", note: str = "") -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("要望が空です")
    if status not in STATUSES:
        raise ValueError(f"status は {STATUSES} のどれか")
    rows = load()
    now = datetime.now().isoformat(timespec="minutes")
    row = {"id": f"q{len(rows) + 1:03d}", "text": text[:1000], "status": status, "note": note[:1000],
           "created": now, "updated": now}
    while any(r["id"] == row["id"] for r in rows):
        row["id"] = f"q{int(row['id'][1:]) + 1:03d}"
    rows.append(row)
    _save(rows)
    return row


def update(rid: str, status: str | None = None, note: str | None = None,
           change: str | None = None, usage: str | None = None, instruction: str | None = None) -> dict[str, Any]:
    rows = load()
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise ValueError(f"unknown request: {rid}")
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status は {STATUSES} のどれか")
        row["status"] = status
    for key, value in (("note", note), ("change", change), ("usage", usage), ("instruction", instruction)):
        if value is not None:
            row[key] = str(value)[:4000]
    row["updated"] = datetime.now().isoformat(timespec="minutes")
    _save(rows)
    return row
