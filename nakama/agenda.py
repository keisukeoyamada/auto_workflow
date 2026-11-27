"""Carry-over topics of recurring meetings (workspace/agenda/items.json, git-ignored).

An item is something raised in a meeting that is not resolved yet and has to come back:
  {id, meeting, topic, status: open|carry|closed, owner, next_action, due, raised, last, history: [{date, note}]}
  open  = raised, still being worked on      carry = to be brought up at the next meeting      closed = resolved

Tensaku proposes items from decks (workspace/agenda/suggestions/*.json); the owner confirms them in the UI.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .config import WORKSPACE_DIR
import threading

_lock = threading.Lock()  # read-check-write of items.json / suggestions must not interleave

FILE = WORKSPACE_DIR / "agenda" / "items.json"
SUGGEST_DIR = WORKSPACE_DIR / "agenda" / "suggestions"
STATUSES = ("open", "carry", "closed")


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


def add(meeting: str, topic: str, owner: str = "", next_action: str = "", due: str = "", status: str = "carry",
        note: str = "") -> dict[str, Any]:
    with _lock:
        return _add(meeting, topic, owner, next_action, due, status, note)


def _add(meeting: str, topic: str, owner: str, next_action: str, due: str, status: str, note: str) -> dict[str, Any]:
    if not meeting.strip() or not topic.strip():
        raise ValueError("定例と議題を入力してください")
    if status not in STATUSES:
        raise ValueError(f"status は {STATUSES}")
    rows = load()
    today = date.today().isoformat()
    row = {"id": f"a{len(rows) + 1:03d}", "meeting": meeting.strip(), "topic": topic.strip()[:300], "status": status,
           "owner": owner.strip()[:60], "next_action": next_action.strip()[:500], "due": due, "raised": today, "last": today,
           "history": [{"date": today, "note": note.strip()[:500] or "登録"}]}
    while any(r["id"] == row["id"] for r in rows):
        row["id"] = f"a{int(row['id'][1:]) + 1:03d}"
    rows.append(row)
    _save(rows)
    return row


def update(item_id: str, status: str | None = None, note: str = "", next_action: str | None = None,
           owner: str | None = None, due: str | None = None) -> dict[str, Any]:
    rows = load()
    row = next((r for r in rows if r["id"] == item_id), None)
    if row is None:
        raise ValueError("議題が見つかりません")
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status は {STATUSES}")
        row["status"] = status
    for key, value in (("next_action", next_action), ("owner", owner), ("due", due)):
        if value is not None:
            row[key] = value.strip()
    row["last"] = date.today().isoformat()
    row["history"].append({"date": row["last"], "note": (note.strip() or f"状態: {row['status']}")[:500]})
    _save(rows)
    return row


def for_next(meeting: str) -> list[dict[str, Any]]:
    """What to bring up next time: carried items first, then open ones (oldest first)."""
    rows = [r for r in load() if r["meeting"] == meeting and r["status"] != "closed"]
    return sorted(rows, key=lambda r: (r["status"] != "carry", r["raised"]))


def suggestions() -> list[dict[str, Any]]:
    out = []
    for p in sorted(SUGGEST_DIR.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for i, s in enumerate(data.get("items") or [] if isinstance(data, dict) else []):
            if isinstance(s, dict) and s.get("topic") and not s.get("handled"):
                out.append({"file": p.name, "index": i, "meeting": str(data.get("meeting") or s.get("meeting") or ""),
                            "topic": str(s["topic"]), "reason": str(s.get("reason") or ""), "source": str(data.get("source") or "")})
    return out


def handle_suggestion(file: str, index: int, accept: bool) -> dict[str, Any] | None:
    if index < 0:
        raise ValueError("提案の番号が不正です")
    with _lock:
        return _handle_suggestion(file, index, accept)


def _handle_suggestion(file: str, index: int, accept: bool) -> dict[str, Any] | None:
    p = SUGGEST_DIR / file
    if p.parent != SUGGEST_DIR or not p.exists():
        raise ValueError("提案が見つかりません")
    data = json.loads(p.read_text(encoding="utf-8"))
    item = data["items"][index]
    if item.get("handled"):
        raise ValueError("処理済みです")
    row = _add(str(data.get("meeting") or item.get("meeting") or ""), item["topic"], item.get("owner", ""),
              item.get("next_action", ""), "", "carry", f"Tensaku の提案（{data.get('source', '')}）") if accept else None
    item["handled"] = "accepted" if accept else "rejected"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return row
