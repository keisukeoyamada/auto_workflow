"""Helper tools for a "meeting notes -> Planner tickets" pipeline that lives in a synced folder.

The pipeline itself (extraction, dedup, roster, close candidates) is the agent's job and is
described in the user's own SKILL.md. These tools cover the mechanical steps that would otherwise
need a shell (PowerShell), so the agent never gets unrestricted command execution:

- ensure_readable       start OneDrive if needed and wait until cloud files can be read
- planner_state_summary counts + open tasks only (keeps ~300 finished cards out of the context)
- planner_state_grep    regex over titles, e.g. finished cards for the recurring-topic check
- archive_ticket        move a promoted ticket file into tickets/archive/
- promote_ticket        draft -> tickets/tickets_<date>.json (validated, BOM-less, create-only)

Folder layout (relative to `ticketing.base`; names can be changed with `ticketing.layout` in fleet.yaml):
  tickets/{draft,archive}/, planner_state/planner_state_{am,pm}/, minutes/, roster/roster.json, logs/
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

SERVER_NAME = "ticketing"
DRAFT_ONLY_KEYS = ("status", "review_required", "fallback_note", "planner_state")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ONEDRIVE_EXES = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft OneDrive" / "OneDrive.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "OneDrive" / "OneDrive.exe",
)


class TicketingError(Exception):
    """A request the tools refuse (bad path, existing destination, invalid draft...)."""


LAYOUT = {"tickets": "tickets", "state": "planner_state", "minutes": "minutes",
          "roster": "roster/roster.json", "logs": "logs"}


class Paths:
    """Resolved folders of the pipeline; every tool path must stay inside `base`."""

    def __init__(self, base: str | Path, layout: dict[str, str] | None = None):
        names = {**LAYOUT, **{k: str(v) for k, v in (layout or {}).items() if k in LAYOUT and v}}
        self.base = Path(base).expanduser().resolve()
        self.tickets = self.base / names["tickets"]
        self.draft = self.tickets / "draft"
        self.archive = self.tickets / "archive"
        self.state = self.base / names["state"]
        self.minutes = self.base / names["minutes"]
        self.roster = self.base / names["roster"]
        self.logs = self.base / names["logs"]

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Paths":
        return cls(cfg["base"], cfg.get("layout"))

    def inside(self, raw: str) -> Path:
        path = Path(raw)
        path = (path if path.is_absolute() else self.base / path).resolve()
        if path != self.base and self.base not in path.parents:
            raise TicketingError(f"{raw} is outside the ticket folder")
        return path

    # Folders the agent may touch with the normal Read/Write tools (see runner/guard).
    @property
    def read_roots(self) -> tuple[Path, ...]:
        return (self.base,)

    @property
    def write_roots(self) -> tuple[Path, ...]:
        # tickets/ itself is NOT here: files there trigger Power Automate, so only promote_ticket writes it.
        return (self.draft, self.logs)


# ---------- pure functions (unit-testable without the SDK) ----------

def _onedrive_running() -> bool:
    if os.name != "nt":
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell; don't try to start a second copy
    return "OneDrive.exe" in out.stdout


def _start_onedrive() -> str:
    for exe in ONEDRIVE_EXES:
        if exe.exists():
            subprocess.Popen([str(exe), "/background"], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return f"started {exe.name}"
    return "OneDrive.exe not found"


def ensure_readable(paths: Paths, files: list[str], timeout_sec: int = 90) -> str:
    """Per file: ok / missing (does not exist) / unreadable (exists but could not be hydrated in time)."""
    lines = []
    if not _onedrive_running():
        lines.append("OneDrive was not running: " + _start_onedrive())
    targets = [paths.inside(f) for f in files]
    deadline = time.monotonic() + max(5, min(timeout_sec, 300))
    pending = {t for t in targets if t.exists()}
    while pending and time.monotonic() < deadline:
        for t in list(pending):
            try:
                with t.open("rb") as fh:
                    fh.read(64)  # reading forces hydration of a cloud placeholder
                pending.discard(t)
            except OSError:
                pass
        if pending:
            time.sleep(3)
    for t, raw in zip(targets, files):
        state = "missing" if not t.exists() else ("unreadable" if t in pending else "ok")
        lines.append(f"{state}\t{raw}")
    return "\n".join(lines)


def _load_state(paths: Paths, file: str) -> list[dict[str, Any]]:
    path = paths.inside(file)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # utf-8-sig: exports start with a BOM
    except FileNotFoundError as exc:
        raise TicketingError(f"not found: {file}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TicketingError(f"cannot read {file}: {exc}") from exc
    if not isinstance(data, list):
        raise TicketingError(f"{file}: expected a JSON array of tasks")
    return [t for t in data if isinstance(t, dict)]


def _fmt(task: dict[str, Any]) -> str:
    return f"{task.get('id', '')} | pc={task.get('percentComplete', '')} | due={task.get('due') or '-'} | {task.get('title', '')}"


def planner_state_summary(paths: Paths, file: str) -> str:
    tasks = _load_state(paths, file)
    open_tasks = [t for t in tasks if (t.get("percentComplete") or 0) < 100]
    head = f"total={len(tasks)} open={len(open_tasks)} done={len(tasks) - len(open_tasks)}"
    return "\n".join([head, "# open tasks (id is the full planner_task_id; copy it verbatim)"] + [_fmt(t) for t in open_tasks])


def planner_state_grep(paths: Paths, file: str, pattern: str, scope: str = "done", limit: int = 200) -> str:
    if scope not in ("done", "open", "all"):
        raise TicketingError("scope must be done | open | all")
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        raise TicketingError(f"bad regex: {exc}") from exc
    hits = []
    for t in _load_state(paths, file):
        done = (t.get("percentComplete") or 0) >= 100
        if (scope == "done" and not done) or (scope == "open" and done):
            continue
        if rx.search(str(t.get("title", ""))):
            hits.append(_fmt(t))
    return f"matches={len(hits)} (scope={scope})\n" + "\n".join(hits[:limit])


def archive_ticket(paths: Paths, name: str, dest_name: str) -> str:
    if not re.fullmatch(r"tickets_\d{4}-\d{2}-\d{2}\.json", name):
        raise TicketingError("name must be tickets_<YYYY-MM-DD>.json (a promoted file in tickets/)")
    if not re.fullmatch(r"[\w\-]+\.json", dest_name):
        raise TicketingError("dest_name must be a plain file name ending in .json")
    src, dest = paths.tickets / name, paths.archive / dest_name
    if not src.exists():
        raise TicketingError(f"tickets/{name} does not exist")
    if dest.exists():
        raise TicketingError(f"archive/{dest_name} already exists")
    paths.archive.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return f"moved tickets/{name} -> tickets/archive/{dest_name}"


def promote_ticket(paths: Paths, date: str, cycle: str) -> str:
    """Create tickets/tickets_<date>.json from draft/tickets_<date>_<cycle>.json exactly once."""
    if not DATE_RE.fullmatch(date) or cycle not in ("am", "pm"):
        raise TicketingError("date must be YYYY-MM-DD and cycle am | pm")
    draft = paths.draft / f"tickets_{date}_{cycle}.json"
    dest = paths.tickets / f"tickets_{date}.json"
    try:
        data = json.loads(draft.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise TicketingError(f"draft not found: draft/{draft.name}") from exc
    except json.JSONDecodeError as exc:
        raise TicketingError(f"draft is not valid JSON: {exc}") from exc
    if data.get("status") != "draft" or data.get("review_required") is not False:
        raise TicketingError("refusing: draft must have status 'draft' and review_required false")
    if data.get("cycle") != cycle:
        raise TicketingError(f"refusing: draft cycle is {data.get('cycle')!r}, expected {cycle!r}")
    for key in ("tasks", "close_candidates"):
        if not isinstance(data.get(key), list):
            raise TicketingError(f"refusing: {key} must be an array (use [] when empty)")
    if dest.exists():
        raise TicketingError(f"ABORT: tickets/{dest.name} already exists (archive it first or it is already promoted)")

    gate = validation_gate(paths, data)
    if gate:  # same code checks as the manual flow; the agent must fix the draft (move items to Drop) and retry
        raise TicketingError("REFUSED by validation gate (draft not promoted). Fix the draft, record removed items as "
                             "dropped, then call promote_ticket again — or stop and report draft_only:\n- " + "\n- ".join(gate))
    final = {k: v for k, v in data.items() if k not in DRAFT_ONLY_KEYS}
    create_promoted(paths, date, final, tag=cycle)
    return (f"promoted draft/{draft.name} -> tickets/{dest.name} "
            f"(tasks={len(final['tasks'])}, close_candidates={len(final['close_candidates'])})")


# Warnings that are (almost) certainly a duplicate: with nobody reviewing, they block an automatic promotion.
# "finished card with the same name" stays a warning only: the SKILL's Step 3G handles recurring cards.
_BLOCKING_WARNINGS = ("未完了の既存カードと同名", "直近 ")


def validation_gate(paths: Paths, draft: dict[str, Any]) -> list[str]:
    """Problems that must stop an unattended promotion (empty list = OK). Shared with the manual flow."""
    from .ticket_review import validate  # local import: ticket_review imports this module

    report = validate(paths, draft)
    problems = list(report["errors"])
    for kind, items in (("task", report["tasks"]), ("close", report["closes"])):
        for x in items:
            problems += [f"{kind} '{x['title']}': {e}" for e in x["errors"]]
            problems += [f"{kind} '{x['title']}': {w}" for w in x["warnings"] if w.startswith(_BLOCKING_WARNINGS)]
    return problems


def create_promoted(paths: Paths, date: str, final: dict[str, Any], tag: str) -> Path:
    """Create tickets/tickets_<date>.json once (BOM-less, validated). Never overwrites."""
    dest = paths.tickets / f"tickets_{date}.json"
    if dest.exists():
        raise TicketingError(f"ABORT: tickets/{dest.name} already exists (archive it first)")
    body = json.dumps(final, ensure_ascii=False, indent=2)
    json.loads(body)  # round-trip check before Power Automate ever sees it
    paths.draft.mkdir(parents=True, exist_ok=True)
    tmp = paths.draft / f".promote_{date}_{tag}.tmp"  # subfolder: does not trigger the flow
    tmp.write_bytes(body.encode("utf-8"))  # UTF-8 without BOM
    try:
        if dest.exists():  # re-check right before creating (someone may have promoted meanwhile)
            raise TicketingError(f"ABORT: tickets/{dest.name} appeared during promotion")
        shutil.copyfile(tmp, dest)  # a fresh *create* event is what fires the flow
    finally:
        tmp.unlink(missing_ok=True)
    return dest


# ---------- SDK wiring ----------

def _text(result: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": result}], **({"is_error": True} if error else {})}


def build_server(paths: Paths, allow_writes: bool = True):
    """In-process MCP server exposing the functions above as mcp__ticketing__* tools.

    allow_writes=False (review mode) leaves out archive_ticket / promote_ticket entirely.
    """

    def wrap(fn):
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return _text(fn(args))
            except TicketingError as exc:
                return _text(str(exc), error=True)
            except Exception as exc:  # noqa: BLE001 - report to the agent instead of killing the run
                return _text(f"unexpected error: {exc}", error=True)
        return handler

    tools = [
        tool("ensure_readable",
             "Start OneDrive if needed and wait until the given files (paths relative to the ticket base) are readable. "
             "Returns ok / missing / unreadable per file. Use before reading cloud files (Step 0).",
             {"files": list[str], "timeout_sec": int},
             )(wrap(lambda a: ensure_readable(paths, list(a.get("files") or []), int(a.get("timeout_sec") or 90)))),
        tool("planner_state_summary",
             "Counts and the OPEN tasks (percentComplete<100) of a planner_state JSON file. Use instead of reading the raw file.",
             {"file": str},
             )(wrap(lambda a: planner_state_summary(paths, str(a.get("file", ""))))),
        tool("planner_state_grep",
             "Regex search over task titles in a planner_state file. scope: done (default, for the recurring-card check) | open | all.",
             {"file": str, "pattern": str, "scope": str},
             )(wrap(lambda a: planner_state_grep(paths, str(a.get("file", "")), str(a.get("pattern", "")),
                                                 str(a.get("scope") or "done")))),
        tool("archive_ticket",
             "Move a promoted tickets/tickets_<date>.json into tickets/archive/<dest_name> (Step 0.5).",
             {"name": str, "dest_name": str},
             )(wrap(lambda a: archive_ticket(paths, str(a.get("name", "")), str(a.get("dest_name", ""))))),
        tool("promote_ticket",
             "Promote draft/tickets_<date>_<cycle>.json to tickets/tickets_<date>.json (Step 6.5): drops draft-only keys, "
             "validates, writes BOM-less UTF-8 and refuses if the destination exists. This triggers Power Automate.",
             {"date": str, "cycle": str},
             )(wrap(lambda a: promote_ticket(paths, str(a.get("date", "")), str(a.get("cycle", ""))))),
    ]
    if not allow_writes:
        tools = [t for t in tools if t.name not in ("archive_ticket", "promote_ticket")]
    return create_sdk_mcp_server(SERVER_NAME, tools=tools)
