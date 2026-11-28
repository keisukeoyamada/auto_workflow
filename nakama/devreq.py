"""A small tracker for requests to Nakama itself (what the owner asked for and where it stands).

Stored in workspace/dev/requests.json (git-ignored):
  [{id, text, status: open|partial|consult|done|hold|dropped, instruction, note, change, usage, created, updated,
    by, by_name, family}]
  instruction = the owner's direction for this item (answers to 要相談, priorities, "this is fine")
  change = what was changed and how (release note), usage = how to use it
  pr = the pull request(s) that resolved it, e.g. "#17" or "#17, #18" or URLs (shown as links)
  by / by_name = which login added it (owner | guest) and the name typed at login
  family = shown to the guest login too. Requests the guest adds are always family; the owner's own requests
  stay private unless the owner ticks them (so nothing personal or work-related reaches the family by default).
  The guest can use family requests fully (status, reply to the memo) but never sees or touches the others.
  triage = the requirements Claude wrote (devtriage.py): {summary, size, questions, spec, hash, at}
  impl = what the devbox did (devimpl.py): {state: queued|pr|merged|closed|failed, number, url, error, at}
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import WORKSPACE_DIR

FILE = WORKSPACE_DIR / "dev" / "requests.json"
STATUSES = ("open", "ready", "working", "review", "partial", "consult", "done", "hold", "dropped")
# ready = 着手OK (the owner approved the spec; only the owner sets it), working = the devbox is implementing,
# review = a draft PR waits for the owner's review (devimpl.py), consult = needs a decision,
# dropped = withdrawn by whoever asked


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


def for_family(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """What the guest login may see: family requests only."""
    return [r for r in (load() if rows is None else rows) if r.get("family")]


def add(text: str, status: str = "open", note: str = "", by: str = "owner", by_name: str = "",
        family: bool = False) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("要望が空です")
    if status not in STATUSES:
        raise ValueError(f"status は {STATUSES} のどれか")
    rows = load()
    now = datetime.now().isoformat(timespec="minutes")
    row = {"id": f"q{len(rows) + 1:03d}", "text": text[:1000], "status": status, "note": note[:1000],
           "created": now, "updated": now,
           "by": by if by in ("owner", "guest") else "owner", "by_name": str(by_name)[:40],
           "family": bool(family) or by == "guest"}
    while any(r["id"] == row["id"] for r in rows):
        row["id"] = f"q{int(row['id'][1:]) + 1:03d}"
    rows.append(row)
    _save(rows)
    return row


def update(rid: str, status: str | None = None, note: str | None = None,
           change: str | None = None, usage: str | None = None, instruction: str | None = None,
           family: bool | None = None, guest: bool = False, pr: str | None = None) -> dict[str, Any]:
    """guest=True: the family login. It may change only family requests, and not which ones are family."""
    rows = load()
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None or (guest and not row.get("family")):
        raise ValueError(f"unknown request: {rid}")  # the same answer as for a missing id: nothing leaks
    if guest and family is not None:
        raise PermissionError("家族に見せるかどうかは、オーナーだけが変えられます")
    if guest and status in ("ready", "working", "review"):
        raise PermissionError("着手OK・実装中・レビュー待ちにできるのはオーナーだけです")
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status は {STATUSES} のどれか")
        row["status"] = status
    for key, value in (("note", note), ("change", change), ("usage", usage), ("instruction", instruction),
                       ("pr", pr)):
        if value is not None:
            row[key] = str(value)[:4000]
    if family is not None:
        row["family"] = bool(family)
    row["updated"] = datetime.now().isoformat(timespec="minutes")
    _save(rows)
    return row


def set_triage(rid: str, triage: dict[str, Any]) -> dict[str, Any]:
    """Store Claude's requirements on a request. Questions -> 要相談; answered (no questions) -> back to オープン."""
    rows = load()
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise ValueError(f"unknown request: {rid}")
    row["triage"] = triage
    if triage.get("questions") and row.get("status") in ("open", "partial"):
        row["status"] = "consult"
    elif not triage.get("questions") and row.get("status") == "consult":
        row["status"] = "open"
    row["updated"] = datetime.now().isoformat(timespec="minutes")
    _save(rows)
    return row


def set_impl(rid: str, impl: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    """Record the devbox's progress on a request (and optionally its status)."""
    rows = load()
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise ValueError(f"unknown request: {rid}")
    row["impl"] = {k: v for k, v in impl.items() if v not in (None, "")}
    if status:
        row["status"] = status
    row["updated"] = datetime.now().isoformat(timespec="minutes")
    _save(rows)
    return row
