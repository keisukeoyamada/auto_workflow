"""Local web dashboard: `python -m nakama ui` (or `serve --ui`). Standard library only.

Security model:
- Binds to 127.0.0.1 by default.
- Every /api call needs a per-process token embedded in the page (blocks cross-site requests).
- The Host header must be localhost or listed in NAKAMA_UI_ALLOW_HOSTS (blocks DNS rebinding).
- Optional HTTP Basic auth via NAKAMA_UI_PASSWORD (use it when exposing on a LAN, e.g. a NAS).
- File access is limited to workspace/ and knowledge/; secrets (.env, config/) are never served.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import unicodedata
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import AGENTS_DIR, KNOWLEDGE_DIR, LOGS_DIR, ROOT, SAMPLES_DIR, TEMPLATES_DIR, WORKSPACE_DIR, ConfigError, load_fleet
from . import chat, cronja
from .todo_note import NoteError
from .spawn import spawn_agent, spawn_nakama
from .state import LOCK_DIR, STALE_LOCK_SECONDS, last_success, spend_on

log = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).with_name("static") / "index.html"
GUIDE_MD = ROOT / "docs" / "GUIDE.md"
READABLE_ROOTS = (WORKSPACE_DIR, KNOWLEDGE_DIR)
# kind -> (folder, glob)
KINDS = {
    "drafts": (WORKSPACE_DIR / "mail" / "drafts", "*.md"),
    "inbox": (WORKSPACE_DIR / "mail" / "inbox", "*.md"),
    "action_items": (WORKSPACE_DIR / "action_items", "*.md"),
    "reports": (WORKSPACE_DIR / "reports", "*.md"),
    "requests": (WORKSPACE_DIR / "requests", "*.md"),
    "assignments": (WORKSPACE_DIR / "assignments", "*.md"),
    "leads": (WORKSPACE_DIR / "crm" / "leads", "*.json"),
    "opportunities": (WORKSPACE_DIR / "crm" / "opportunities", "*.json"),
    "knowledge": (KNOWLEDGE_DIR, "*.md"),
    "attendance": (WORKSPACE_DIR / "attendance", "**/*.md"),  # mine.md + sources/ + outbox/
    "memos": (WORKSPACE_DIR / "memos", "*.md"),
}
ATTENDANCE_DIR = WORKSPACE_DIR / "attendance"
MINE_MD = ATTENDANCE_DIR / "mine.md"
MINE_HEADER = (
    "# 自分の勤怠（マスター）\n\n"
    "Kintai がここを正として Teams / Loop 用の貼り付け文を作る。手で編集しても OK。\n\n"
    "| 日付 | 種別 | 時間 | メモ |\n|---|---|---|---|\n"
)
PASTE_SOURCES = {"teams", "loop", "memo"}  # memo = one absence line routed from a dashboard memo
# Which statuses the UI may set, per folder
EDITABLE_STATUS = {
    WORKSPACE_DIR / "action_items": {"open", "waiting", "done"},
    WORKSPACE_DIR / "requests": {"open", "done"},
    WORKSPACE_DIR / "memos": {"new", "triaged", "done"},
}
MAX_LIST = 200
MAX_FILE_BYTES = 512 * 1024


def _editable(path: Path) -> set[str]:
    """Statuses the UI may set on this (resolved) file; empty if none."""
    for folder, statuses in EDITABLE_STATUS.items():
        if path.parent == folder.resolve():
            return statuses
    return set()

_spawned: dict[str, subprocess.Popen] = {}  # agent -> process started from the UI
_spawn_lock = threading.Lock()


# ---------- helpers ----------

def _front_matter(text: str) -> dict[str, str]:
    """Tiny `key: value` front matter parser (enough for list views; not full YAML)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.split(" #", 1)[0].strip().strip("\"'")
    return meta


def _title(text: str, meta: dict) -> str:
    if meta.get("subject"):
        return meta["subject"]
    body = re.sub(r"^---\r?\n.*?\r?\n---\r?\n?", "", text, count=1, flags=re.S)
    for line in body.splitlines():
        line = line.strip().lstrip("#>").strip()
        if line and not line.startswith(("<!--", "```")):
            return line[:120]
    return ""


def _list_kind(kind: str, samples: bool = True) -> list[dict]:
    folder, pattern = KINDS[kind]
    if not folder.exists():
        return []
    files = [p for p in folder.glob(pattern) if p.name != "README.md"]  # folder docs, not data
    sample_set, tokens = _sample_paths(), _sample_tokens()
    if not samples:
        files = [p for p in files if not _is_sample(p, sample_set, tokens)]
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_LIST]
    items = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if p.suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            keep = ("status", "owner", "priority", "stage", "category", "next_step")
            meta = {k: str(data[k]) for k in keep if data.get(k) not in (None, "")}
            title = data.get("company") or data.get("account") or p.stem
        else:
            meta = _front_matter(text)
            title = _title(text, meta)
        items.append({
            "path": p.relative_to(ROOT).as_posix(),
            "name": p.name,
            "mtime": p.stat().st_mtime,
            "title": title,
            "meta": meta,
            "sample": _is_sample(p, sample_set, tokens),
        })
    return items


def _safe_path(rel: str) -> Path:
    """Resolve a repo-relative path and make sure it stays inside workspace/ or knowledge/."""
    path = (ROOT / rel).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in READABLE_ROOTS):
        raise PermissionError(rel)
    return path


def _is_running(agent: str) -> bool:
    proc = _spawned.get(agent)
    if proc and proc.poll() is None:
        return True
    lock = LOCK_DIR / f"{agent}.lock"
    return lock.exists() and time.time() - lock.stat().st_mtime < STALE_LOCK_SECONDS


def _auth_label(fleet) -> str:
    has_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip())
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    has_token = has_token or key.startswith("sk-ant-oat")  # mis-pasted token; runner fixes this up
    has_key = bool(key) and not key.startswith("sk-ant-oat")
    mode = fleet.auth if fleet.auth != "auto" else ("subscription" if has_token else "api_key")
    ready = has_token if mode == "subscription" else has_key
    return mode if ready else f"{mode} (未設定)"


def overview(samples: bool = True) -> dict:
    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    today = datetime.now(tz).date().isoformat()
    spent = spend_on(today, tz)
    agents = []
    runs = _last_runs()
    for spec in fleet.agents.values():
        last = last_success(spec.name)
        agents.append({
            "name": spec.name, "model": spec.model, "effort": spec.effort,
            "schedule": spec.schedule, "enabled": spec.enabled,
            "schedule_ja": [cronja.describe(e) for e in spec.schedule],
            "advisor": bool(spec.advisor),
            "last_success": last.astimezone(tz).isoformat(timespec="minutes") if last else None,
            "cost_today": round(spent.get(spec.name, 0.0), 4),
            "running": _is_running(spec.name),
            "last_run": runs.get(spec.name),
        })

    def count(kind: str, status: str | None = None) -> int:
        items = _list_kind(kind, samples)
        return sum(1 for i in items if status is None or i["meta"].get("status") == status)

    try:  # the ticket folder is optional and may be a slow synced drive: never break the overview
        my_open = sum(1 for r in my_tasks()["tasks"] if r["status"] == "open") if fleet.ticketing.get("base") else 0
    except Exception:  # noqa: BLE001
        log.exception("my_tasks failed")
        my_open = 0
    return {
        "now": datetime.now(tz).isoformat(timespec="minutes"),
        "timezone": fleet.timezone,
        "owner": fleet.owner.get("name", ""),
        "auth": _auth_label(fleet),
        "has_samples": bool(_sample_paths()),
        "budget": float(fleet.limits.get("daily_budget_usd") or 0),
        "spent": round(sum(spent.values()), 4),
        "agents": agents,
        "counts": {
            "drafts": count("drafts"),
            "action_items_open": count("action_items", "open"),
            "requests_open": count("requests", "open"),
            "inbox_new": count("inbox", "new"),
            "my_tasks_open": my_open,
        },
        "has_ticketing": bool((fleet.ticketing or {}).get("base")),
        "queues": _queues(fleet, samples),
    }


def start_run(agent: str) -> str:
    fleet = load_fleet()
    if agent not in fleet.agents:
        raise ValueError(f"unknown agent: {agent}")
    tz = ZoneInfo(fleet.timezone)
    budget = float(fleet.limits.get("daily_budget_usd") or 0)
    spent = sum(spend_on(datetime.now(tz).date().isoformat(), tz).values())
    if budget and spent >= budget:
        raise ValueError(f"日次予算 ${budget:.2f} に到達しています（${spent:.2f}）")
    with _spawn_lock:
        if _is_running(agent):
            raise ValueError(f"{agent} は実行中です")
        # --force skips the trigger/enabled checks (budget was checked above).
        _spawned[agent] = spawn_agent(agent, force=True)
    return f"{agent} を起動しました"


def create_request(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("依頼内容が空です")
    now = datetime.now(ZoneInfo(load_fleet().timezone))
    path = WORKSPACE_DIR / "requests" / f"{now.strftime('%Y%m%d-%H%M%S')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: open\ncreated: {now.isoformat(timespec='minutes')}\n---\n{text}\n",
                    encoding="utf-8")
    return path.relative_to(ROOT).as_posix()


def _cell(value: str, limit: int = 100) -> str:
    """One Markdown table cell: single line, no pipes."""
    return " ".join(str(value).split()).replace("|", "/")[:limit]


def add_my_attendance(date: str, kind: str, hours: str = "", memo: str = "") -> str:
    """Append one row to the owner's attendance master (workspace/attendance/mine.md)."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日付は YYYY-MM-DD で指定してください") from exc
    if not _cell(kind):
        raise ValueError("種別が空です")
    MINE_MD.parent.mkdir(parents=True, exist_ok=True)
    text = MINE_MD.read_text(encoding="utf-8") if MINE_MD.exists() else MINE_HEADER
    if not text.endswith("\n"):
        text += "\n"
    text += f"| {date} | {_cell(kind, 20)} | {_cell(hours, 40) or '終日'} | {_cell(memo)} |\n"
    MINE_MD.write_text(text, encoding="utf-8")
    return MINE_MD.relative_to(ROOT).as_posix()


def save_memo(text: str) -> str:
    """Quick note from the dashboard (things heard in person etc.). status: new until something handles it."""
    text = text.strip()
    if not text:
        raise ValueError("メモが空です")
    if len(text) > 20000:
        raise ValueError("メモが長すぎます（2 万字まで）")
    now = datetime.now(ZoneInfo(load_fleet().timezone))
    # Same text again within 60 s (double click, Ctrl+Enter + button): keep the first one only.
    for prev in sorted((WORKSPACE_DIR / "memos").glob("*.md"), reverse=True)[:5]:
        if time.time() - prev.stat().st_mtime < 60 and prev.read_text(encoding="utf-8").rstrip().endswith(text):
            return prev.relative_to(ROOT).as_posix()
    path = WORKSPACE_DIR / "memos" / f"{now.strftime('%Y%m%d-%H%M%S')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: new\ncreated: {now.isoformat(timespec='minutes')}\n---\n{text}\n", encoding="utf-8")
    return path.relative_to(ROOT).as_posix()


def save_paste(source: str, text: str) -> str:
    """Store text copied from Teams / Loop as a new input file for Kintai."""
    if source not in PASTE_SOURCES:
        raise ValueError(f"source は {sorted(PASTE_SOURCES)} のどれか")
    text = text.strip()
    if not text:
        raise ValueError("貼り付け内容が空です")
    now = datetime.now(ZoneInfo(load_fleet().timezone))
    path = ATTENDANCE_DIR / "sources" / f"{now.strftime('%Y%m%d-%H%M%S')}-{source}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nsource: {source}\npasted: {now.isoformat(timespec='minutes')}\n---\n{text}\n",
                    encoding="utf-8")
    return path.relative_to(ROOT).as_posix()


def _sample_paths() -> set[Path]:
    """Workspace files that came from samples/ via `init --demo` (same relative path / file name)."""
    found: set[Path] = set()
    for src in SAMPLES_DIR.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(SAMPLES_DIR)
        if rel.parts[0] == "calendar":  # copied into calendar/<date>/
            found.update(p.resolve() for p in (WORKSPACE_DIR / "calendar").glob(f"*/{rel.name}"))
        else:
            found.add((WORKSPACE_DIR / rel).resolve())
    return found


SAMPLE_ID_RE = re.compile(r"\b(?:[A-Z]-\d{4}|D-SAMPLE-\d+)\b")
_derived_cache: dict[Path, tuple[float, bool]] = {}
SAMPLE_MARKER = "【サンプル】"


def _sample_tokens() -> set[str]:
    """Ids (L-0001, M-0001, T-0001...) and file stems (kawasemi-demo...) that only exist in samples/."""
    tokens: set[str] = set()
    for src in SAMPLES_DIR.rglob("*"):
        if src.is_file():
            tokens.add(src.stem)
            try:
                tokens.update(SAMPLE_ID_RE.findall(src.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return tokens


def _is_sample(path: Path, sample_set: set[Path], tokens: set[str]) -> bool:
    """Copied from samples/, or produced by an agent from sample data (mentions a sample id in its name/head)."""
    path = path.resolve()
    if path in sample_set:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    hit = _derived_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        head = ""
    # Knowledge templates carry a 【サンプル】 marker until the owner rewrites them; an untouched copy of a
    # template counts as sample too (some templates have no marker).
    template = TEMPLATES_DIR / "knowledge" / path.name
    untouched = path.parent == KNOWLEDGE_DIR.resolve() and template.exists() and \
        template.read_bytes() == path.read_bytes()
    result = untouched or SAMPLE_MARKER in head or any(tok in path.name or tok in head for tok in tokens)
    _derived_cache[path] = (mtime, result)
    return result


# ---------- settings editor ----------
# Only these files are editable from the UI. .env (secrets) and mcp.json (starts programs) are not.
SETTINGS_BACKUP_DIR = LOGS_DIR / "settings-backup"


def settings_files() -> list[dict]:
    items = [
        {"path": "config/fleet.yaml", "group": "設定", "label": "エージェント・スケジュール・予算（fleet.yaml）"},
        {"path": "config/team.yaml", "group": "設定", "label": "チームメンバー・不在日（team.yaml）"},
    ]
    for local in sorted(AGENTS_DIR.glob("*/LOCAL.md")):
        if not local.parent.name.startswith("_"):
            items.append({"path": local.relative_to(ROOT).as_posix(), "group": "エージェントの個別チューニング",
                          "label": f"{local.parent.name} の追加指示（LOCAL.md）"})
    items.append({"path": "workspace/routine/routines.json", "group": "ルーティン",
                  "label": "定常ジョブの定義（routines.json）"})
    for md in sorted(KNOWLEDGE_DIR.glob("*.md")):
        if md.name != "README.md":
            items.append({"path": md.relative_to(ROOT).as_posix(), "group": "ナレッジ", "label": md.name})
    return items


def _settings_path(rel: str) -> Path:
    if rel not in {i["path"] for i in settings_files()}:
        raise PermissionError(rel)
    return ROOT / rel


def read_setting(rel: str) -> dict:
    path = _settings_path(rel)
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:  # not created yet: start from the committed example
        example = path.with_name(path.name.replace(".yaml", ".example.yaml"))
        content = example.read_text(encoding="utf-8") if example.exists() else ""
    return {"path": rel, "content": content, "exists": path.exists()}


def _validate_setting(rel: str, content: str) -> None:
    """Refuse to save config that would break the fleet (bad YAML, unknown keys, bad cron)."""
    if rel.endswith(".json"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON の書式エラー: {exc}") from exc
        if not isinstance(data, list) or not all(isinstance(r, dict) and r.get("id") and r.get("name") for r in data):
            raise ValueError("routines.json は [{id, name, schedule_text, ...}] の配列にしてください")
        if len({r["id"] for r in data}) != len(data):
            raise ValueError("id が重複しています")
        return
    if not rel.endswith(".yaml"):
        return
    import tempfile

    import yaml
    from apscheduler.triggers.cron import CronTrigger

    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML の書式エラー: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("トップレベルは key: value の形にしてください")
    if rel == "config/fleet.yaml":
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "fleet.yaml"
            probe.write_text(content, encoding="utf-8")
            fleet = load_fleet(probe)  # raises ConfigError with a readable message
        ZoneInfo(fleet.timezone)
        for spec in fleet.agents.values():
            for expr in spec.schedule:
                try:
                    CronTrigger.from_crontab(expr, timezone=fleet.timezone)
                except ValueError as exc:
                    raise ValueError(f"{spec.name} の schedule '{expr}' が不正: {exc}") from exc
    elif rel == "config/team.yaml" and not isinstance(data.get("members", []), list):
        raise ValueError("members はリストにしてください")


def save_setting(rel: str, content: str) -> str:
    path = _settings_path(rel)
    try:
        _validate_setting(rel, content)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if path.exists():  # keep the previous version (logs/ is git-ignored)
        SETTINGS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (SETTINGS_BACKUP_DIR / f"{rel.replace('/', '__')}.{stamp}.bak").write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return "保存しました" + ("（スケジュールは 1 分以内に反映）" if rel == "config/fleet.yaml" else "")


# ---------- my tasks (from the ticketing pipeline's promotion history) ----------
_json_cache: dict[Path, tuple[float, Any]] = {}  # path -> (mtime, parsed) so polling stays cheap


def _cached_json(path: Path) -> Any:
    try:
        mtime = path.stat().st_mtime
        hit = _json_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    _json_cache[path] = (mtime, data)
    return data


def _state_dir(base: Path) -> Path:
    from .helpers.ticketing import Paths

    return Paths(base, (load_fleet().ticketing or {}).get("layout")).state


def _latest_planner_state(base: Path) -> Path | None:
    """Most recently exported planner_state_*.json across the am/pm folders."""
    # Pick the newest name per folder first (stat on hundreds of cloud placeholders is slow),
    # then compare just those candidates by mtime.
    newest = []
    for sub in ("planner_state_am", "planner_state_pm"):
        names = sorted((_state_dir(base) / sub).glob("planner_state_*.json"), key=lambda p: p.name)
        newest += names[-1:]
    return max(newest, key=lambda p: p.stat().st_mtime) if newest else None


_ttl_cache: dict[tuple, tuple[float, Any]] = {}
MY_TASKS_TTL = 60  # seconds; the dashboard polls every 15 s and the synced folder changes a few times a day


def _ttl(key: tuple, seconds: int, fn):
    hit = _ttl_cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = fn()
    _ttl_cache[key] = (time.time(), value)
    return value


def _norm_title(s: str) -> str:
    """Planner titles drift in width/spacing (全角/半角, extra spaces); compare them normalised."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s)))


def my_tasks(include_done: bool = False) -> dict:
    """Cached wrapper (see MY_TASKS_TTL): reading the synced folder can take seconds."""
    full = _ttl(("my_tasks",), MY_TASKS_TTL, lambda: _my_tasks(True))  # one read serves both views
    if include_done:
        return full
    return {**full, "tasks": [r for r in full["tasks"] if r["status"] not in ("done", "stale")]}


def _my_tasks(include_done: bool = False) -> dict:
    cfg = load_fleet().ticketing or {}
    base, owner = cfg.get("base"), str(cfg.get("owner_id") or "")
    if not base:
        return {"tasks": [], "error": "ticketing.base が未設定です（設定タブの fleet.yaml）"}
    if not owner:
        return {"tasks": [], "error": "ticketing.owner_id（roster.json の自分の id）が未設定です"}
    base = Path(base).expanduser()
    paths = _ticket_paths()
    promoted = list(paths.tickets.glob("tickets_*.json")) + list(paths.archive.glob("*.json"))
    mine: dict[str, dict] = {}
    for f in sorted(promoted, key=lambda p: p.name):
        data = _cached_json(f)
        if not isinstance(data, dict):
            continue
        for t in data.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            ids = [str(t.get("assignee_id") or "")] + [str(i) for i in t.get("assignee_ids") or []]
            if owner in ids and t.get("title"):
                mine[str(t["title"])] = {  # later files win (re-issued titles keep the newest)
                    "title": str(t["title"]), "due": str(t.get("due") or ""), "priority": str(t.get("priority") or ""),
                    "memo": str(t.get("memo") or "")[:300], "issued": str(data.get("generated_at") or "")[:10],
                    "file": f.name, "unprocessed": f.name.startswith("_UNPROCESSED"),
                }
    state_file = _latest_planner_state(base)
    state = {_norm_title(s.get("title")): s for s in (_cached_json(state_file) or []) if isinstance(s, dict)} if state_file else {}
    stale_before = (datetime.now(ZoneInfo(load_fleet().timezone)).date().toordinal() - 30)
    rows = []
    for t in mine.values():
        s = state.get(_norm_title(t["title"]))
        status = "unknown" if s is None else ("done" if (s.get("percentComplete") or 0) >= 100 else "open")
        try:
            old = datetime.strptime(t["issued"], "%Y-%m-%d").date().toordinal() < stale_before
        except ValueError:
            old = True
        if status == "unknown" and old:
            status = "stale"  # not on the board any more (deleted / renamed / long gone): hidden by default
        if s is not None and s.get("due"):
            t["due"] = str(s["due"])[:10]  # Planner is the truth once the card exists
        t["status"], t["percent"] = status, (s or {}).get("percentComplete")
        t["link"] = _task_link(str((s or {}).get("id") or ""))
        if include_done or status not in ("done", "stale"):
            rows.append(t)
    order = {"open": 0, "unknown": 1, "done": 2, "stale": 3}
    rows.sort(key=lambda r: (order[r["status"]], r["due"] or "9999", r["title"]))
    return {"tasks": rows, "error": None, "state_file": state_file.name if state_file else None,
            "state_dir": state_file.parent.name if state_file else None, "history_files": len(promoted)}


# ---------- routines, holidays, unified task list, task notes ----------

def routine_month(month: str, mine_only: bool) -> dict:
    from . import routines

    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        month = datetime.now(ZoneInfo(load_fleet().timezone)).strftime("%Y-%m")
    return routines.month_view(month, mine_only)


def _month_or_current(month: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}", month or ""):
        return month
    return datetime.now(ZoneInfo(load_fleet().timezone)).strftime("%Y-%m")


def unified_tasks(include_done: bool = False, month: str = "") -> dict:
    """One list of my tasks for a month, each with its source (🗂 Planner / 🔁 routine / 📓 hand-written note).

    Hidden unless include_done: done / skipped items. An empty list therefore means "everything handled".
    - routine: windows of that month with that month's progress
    - planner: due in that month; for the current month also anything open and overdue / undated
    - note:    open items (no dates); only in the current month view (done ones with include_done)
    """
    from . import bizdays, routines, todo_note

    tz = ZoneInfo(load_fleet().timezone)
    today = datetime.now(tz).date()
    month = _month_or_current(month)
    year, mon = (int(x) for x in month.split("-"))
    current = month == today.strftime("%Y-%m")
    rows: list[dict] = []
    errors = []

    if (load_fleet().ticketing or {}).get("base"):
        planner = my_tasks(include_done=True)
        if planner.get("error"):
            errors.append(planner["error"])
        for t in planner["tasks"]:
            due = t.get("due") or ""
            in_month = due.startswith(month) or (current and (not due or due < month) and t["status"] != "done")
            if t["status"] == "stale" or not in_month or (t["status"] == "done" and not include_done):
                continue
            key = routines.planner_key(t.get("link") or t["title"])
            rows.append({"source": "planner", "title": t["title"], "due": due, "status": t["status"], "link": t.get("link", ""),
                         "priority": t.get("priority", ""), "memo": t.get("memo", ""), "key": key,
                         "notes": routines.note_count(key)})

    for r in routines.items_for_month(year, mon, today):
        if include_done or r["status"] not in ("done", "skip"):
            rows.append(r)

    note = todo_note.load(include_done) if current else {"configured": False, "items": []}
    if note.get("error"):
        errors.append(note["error"])
    for i in note["items"]:
        title = (f"{i['parent']} › " if i["parent"] else "") + i["text"]
        rows.append({"source": "note", "title": title, "due": "", "status": "done" if i["done"] else "open",
                     "star": i["star"], "section": i["section"], "line": i["line"], "raw": i["raw"],
                     "key": i["key"], "notes": routines.note_count(i["key"])})

    for r in rows:  # business-day label of the due date (e.g. "18営")
        try:
            d = date.fromisoformat(r["due"]) if r.get("due") else None
        except ValueError:
            d = None
        r["bd"] = bizdays.business_day_index(d) if d else None
    order = {"open": 0, "todo": 0, "doing": 0, "review": 0, "pending": 1, "unknown": 1, "done": 2, "skip": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 1), not r.get("star"), r.get("due") or "9999", r["title"]))
    idx = bizdays.business_day_index(today)
    return {"tasks": rows, "error": " / ".join(errors) or None, "today": today.isoformat(), "month": month,
            "current": current, "bd": idx, "bd_total": len(bizdays.business_days_of_month(today.year, today.month)),
            "holiday": bizdays.holidays().get(today.isoformat(), {}).get("name", ""),
            "note_configured": note.get("configured", False) or bool(todo_note.note_path())}


def complete_task(body: dict) -> dict:
    """✓ from the task list: routine -> that month's progress; note -> flip □ to ■ in the note file."""
    from . import routines, todo_note

    source = str(body.get("source", ""))
    done = body.get("done", True) is not False
    if source == "routine":
        return routines.set_progress(str(body.get("id", "")), _month_or_current(str(body.get("month", ""))),
                                     "done" if done else "todo")
    if source == "note":
        try:
            return todo_note.set_done(int(body.get("line", -1)), str(body.get("raw", "")), done)
        except todo_note.NoteError as exc:
            raise ValueError(str(exc)) from exc
    raise ValueError("Planner のタスクは Planner 側で完了にしてください（次の planner_state で反映）")


def add_note_task(text: str, star: bool) -> dict:
    from . import todo_note

    try:
        return todo_note.add_item(text, star=star)
    except todo_note.NoteError as exc:
        raise ValueError(str(exc)) from exc


def data_sources() -> dict:
    """Where each part of the dashboard comes from (shown in the tabs' detail areas)."""
    from . import todo_note
    from .bizdays import HOLIDAYS_FILE

    def read(path: Path) -> list:
        data = _cached_json(path) if path.exists() else None
        return data if isinstance(data, list) else []

    cfg = load_fleet().ticketing or {}
    return {
        "routine": read(WORKSPACE_DIR / "routine" / "source.json") + [
            {"what": "定義（Nakama の正本）", "kind": "file", "path": str(WORKSPACE_DIR / "routine" / "routines.json")}],
        "holidays": read(HOLIDAYS_FILE.with_name("holidays.source.json")) + [
            {"what": "祝日一覧（Nakama の正本）", "kind": "file", "path": str(HOLIDAYS_FILE)}],
        "tasks": ([{"what": "Planner（起票履歴 × planner_state）", "kind": "folder", "path": str(cfg["base"])}] if cfg.get("base") else [])
                 + ([{"what": "手書き ToDo（□/■/★ で双方向に更新）", "kind": "file", "path": str(todo_note.note_path())}]
                    if todo_note.note_path() else [])
                 + [{"what": "ルーティン", "kind": "file", "path": str(WORKSPACE_DIR / "routine" / "routines.json")}],
        "kihyo": ([{"what": "同期フォルダ", "kind": "folder", "path": str(cfg["base"])}] if cfg.get("base") else [])
                 + [{"what": "手順書（正本）", "kind": "file", "path": p} for p in
                    (load_fleet().agents.get("kihyo").instructions_files if load_fleet().agents.get("kihyo") else [])],
    }


# ---------- Claude assist: context for a "相談" chat (the chat itself is read-only) ----------

def assist_prompt(kind: str, ref: dict) -> dict:
    """Build a self-contained question for the chat panel. Nothing is changed by the assistant."""
    from . import bizdays, routines

    today = datetime.now(ZoneInfo(load_fleet().timezone)).date()
    head = [f"今日: {today.isoformat()}（{bizdays.business_day_index(today) or '休日'} 営業日目）"]
    ups = bizdays.upcoming_holidays(today, 45)
    head.append("今後 45 日の祝日: " + (", ".join(f"{d} {n}" for d, n in ups) or "なし"))
    cal = _cached_json(CALENDAR_JSON) if CALENDAR_JSON.exists() else None
    if isinstance(cal, dict):
        future = [e for e in cal.get("entries") or [] if isinstance(e, dict) and str(e.get("date", "")) >= today.isoformat()]
        head.append("チームの不在予定（Kintai）: " + ("; ".join(f"{e.get('date')} {e.get('member')} {e.get('kind')}" for e in future[:40]) or "なし"))
    mine = MINE_MD.read_text(encoding="utf-8")[-3000:] if MINE_MD.exists() else "（未登録）"
    head.append("自分の勤怠予定（mine.md）:\n" + mine)

    def task_lines(limit: int = 40) -> str:
        t = unified_tasks()
        return "\n".join(f"- [{x['source']}] {x['title']} / 期日 {x.get('due') or '-'} / 状態 {x['status']}"
                         for x in t["tasks"][:limit]) or "- なし"

    if kind == "task":
        title = str(ref.get("title", ""))[:300]
        notes = "\n".join(f"- {n['ts']} {n['text']}" for n in routines.notes(str(ref.get("key", "")))[-10:]) if ref.get("key") else ""
        body = (f"次のタスクについてアシストしてください。\n\nタスク: {title}\n出典: {ref.get('source')} / 期日: {ref.get('due') or '-'} / "
                f"状態: {ref.get('status')}\n補足: {str(ref.get('memo') or '')[:800]}\n作業メモ:\n{notes or '- なし'}\n\n"
                "お願い: ①次の一手（具体的に・30 分で終わる単位）②関係者への返答・連絡文の案 ③完了にしてよいかの判断と、"
                "Planner に追記するクローズ文の案（📝/🔴 の書式）④締切までの営業日と、他のタスク・不在予定とのぶつかり。")
    elif kind == "notice":
        body = (f"エスカレ欄の次の通知について判断材料と推奨を出してください。\n\n種類: {ref.get('kind')}\n内容: {str(ref.get('title', ''))[:500]}\n"
                f"詳細: {str(ref.get('detail') or '')[:2000]}\n\n"
                "お願い: ①何を判断すべきか ②推奨（起票する/しない、どの既存カードに寄せるか、誰に確認するか）③そのまま送れる返答・クローズ文の案。")
    elif kind == "overall":
        notices = notifications()["items"][:30]
        body = ("ダッシュボード全体を見て、総合的にアシストしてください。\n\n未確認の通知:\n"
                + ("\n".join(f"- {n['kind']}: {n['title']}" for n in notices) or "- なし")
                + "\n\n未完了のタスク:\n" + task_lines()
                + "\n\nお願い: ①今日やるべき順番（理由つき、上位 5 件）②返答・クローズ文の案が必要なもの ③期限切れ・詰まりの解消策 "
                  "④休暇の提案（締切・ルーティンの実施枠・チームの不在かぶり・祝日を踏まえて、休みを取りやすい日と避けるべき日）。")
    else:
        raise ValueError("kind は task / notice / overall")
    return {"title": {"task": "🤖 タスク: ", "notice": "🤖 通知: ", "overall": "🤖 総合アシスト "}[kind]
            + (str(ref.get("title", ""))[:30] if kind != "overall" else today.isoformat()),
            "prompt": body + "\n\n--- 参考情報 ---\n" + "\n".join(head)}


# ---------- profile, run-all, task due dates, memo history ----------

def save_profile(name: str, signature: str) -> dict:
    """Edit owner.name / owner.signature in config/fleet.yaml in place (keeps comments and layout)."""
    name, signature = name.strip(), signature.strip()
    if not name:
        raise ValueError("名前を入力してください")
    path = ROOT / "config" / "fleet.yaml"
    if not path.exists():
        raise ValueError("config/fleet.yaml がありません（python -m nakama init）")
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("owner:")
    except ValueError as exc:
        raise ValueError("fleet.yaml に owner: がありません") from exc
    quote = lambda v: '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    done = set()
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i].startswith((" ", "\t")):
            break  # end of the owner block
        for key, value in (("name", name), ("signature", signature)):
            if lines[i].lstrip().startswith(f"{key}:") and key not in done:
                indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                comment = lines[i].split(" #", 1)[1] if " #" in lines[i] else ""
                lines[i] = f"{indent}{key}: {quote(value)}" + (f"  #{comment}" if comment else "")
                done.add(key)
    text = "\n".join(lines) + "\n"
    _validate_setting("config/fleet.yaml", text)
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "updated": sorted(done)}


RUN_ALL_SKIP = {"kihyo"}  # creates Planner tickets: never as part of a bulk run


def start_run_all() -> dict:
    fleet = load_fleet()
    names = [n for n, s in fleet.agents.items() if s.enabled and n not in RUN_ALL_SKIP and not _is_running(n)]
    if not names:
        raise ValueError("実行できるエージェントがありません")
    with _spawn_lock:
        _spawned["_all"] = spawn_nakama(["run-all", *names])
    return {"started": names, "skipped": sorted(RUN_ALL_SKIP & set(fleet.agents))}


def set_task_due(body: dict) -> dict:
    from . import routines, todo_note

    source, due = str(body.get("source", "")), str(body.get("due", ""))
    if source == "routine":
        return routines.set_due_override(str(body.get("id", "")), _month_or_current(str(body.get("month", ""))), due)
    if source == "note":
        try:
            return todo_note.set_due(int(body.get("line", -1)), str(body.get("raw", "")), due)
        except todo_note.NoteError as exc:
            raise ValueError(str(exc)) from exc
    raise ValueError("Planner の期日は Planner で変更してください（起票済みのカードの期日を書き換える PA フローが無いため。要望 q071 に記載）")


def memo_history() -> dict:
    """Every memo with where it went: Kikitori's routes, actions taken, and requests that cite it."""
    from . import devreq

    applied = _load_applied()
    reqs = devreq.load()
    rows = []
    for p in sorted(MEMOS_DIR.glob("*.md"), reverse=True)[:300]:
        text = p.read_text(encoding="utf-8")
        meta = _front_matter(text)
        stem = p.stem
        created = meta.get("created", "")[:16].replace("T", " ")
        sug = _cached_json(SUGGEST_DIR / f"{stem}.json") if (SUGGEST_DIR / f"{stem}.json").exists() else None
        routes = [str(x.get("route")) for x in (sug or {}).get("items") or [] if isinstance(x, dict)] if isinstance(sug, dict) else []
        went = []
        for idx, a in (applied.get(stem) or {}).items():
            route = routes[int(idx)] if idx.isdigit() and int(idx) < len(routes) else ""
            went.append({"route": route, "action": a.get("action"), "job": a.get("job", ""), "path": a.get("path", ""), "at": a.get("at", "")})
        cites = [{"id": r["id"], "status": r["status"], "text": r["text"]} for r in reqs
                 if f"メモ {stem}" in (r.get("note") or "")]  # requests cite memos by their id (file name)
        rows.append({"memo": stem, "created": created, "status": meta.get("status", "new"), "route": meta.get("route", ""),
                     "title": _title(text, meta), "routes": routes, "went": went, "requests": cites})
    return {"rows": rows}


# ---------- chat side panel ----------
_chat_procs: dict[str, subprocess.Popen] = {}


def _chat_running(chat_id: str) -> bool:
    proc = _chat_procs.get(chat_id)
    if proc and proc.poll() is None:
        return True
    lock = LOCK_DIR / f"{chat.lock_name(chat_id)}.lock" if chat.CHAT_ID_RE.fullmatch(chat_id or "") else None
    return bool(lock and lock.exists() and time.time() - lock.stat().st_mtime < STALE_LOCK_SECONDS)


def send_chat(chat_id: str, text: str) -> dict:
    if _chat_running(chat_id):
        raise ValueError("前の質問に回答中です。終わってから送ってください")
    chat.add_user_message(chat_id, text)
    with _spawn_lock:
        _chat_procs[chat_id] = spawn_nakama(["chat-turn", chat_id])
    return {"ok": True}


# ---------- memo routing (Kikitori suggests, the owner clicks) ----------
MEMOS_DIR = WORKSPACE_DIR / "memos"
SUGGEST_DIR = WORKSPACE_DIR / "kikitori" / "suggestions"
APPLIED_FILE = WORKSPACE_DIR / "kikitori" / "applied.json"
MEMO_STEM_RE = re.compile(r"\d{8}-\d{6}")
ROUTES = ("task", "attendance", "knowledge", "keep")
ATT_KINDS = ("有休", "午前休", "午後休", "時間変更", "早退", "研修", "その他")


def _load_applied() -> dict[str, dict]:
    data = _cached_json(APPLIED_FILE) if APPLIED_FILE.exists() else None
    return data if isinstance(data, dict) else {}


def memo_detail(stem: str) -> dict:
    if not MEMO_STEM_RE.fullmatch(stem or ""):
        raise ValueError("bad memo id")
    path = MEMOS_DIR / f"{stem}.md"
    if not path.exists():
        raise FileNotFoundError(stem)
    text = path.read_text(encoding="utf-8")
    sug = _cached_json(SUGGEST_DIR / f"{stem}.json") if (SUGGEST_DIR / f"{stem}.json").exists() else None
    items = []
    for x in (sug or {}).get("items") or [] if isinstance(sug, dict) else []:
        if isinstance(x, dict) and x.get("route") in ROUTES:  # agent output is untrusted: keep known shapes only
            items.append({k: str(x.get(k) or "") for k in
                          ("route", "summary", "text", "who", "name", "date", "kind", "hours", "request")})
    return {"memo": stem, "meta": _front_matter(text), "body": re.sub(r"^---\r?\n.*?\r?\n---\r?\n?", "", text, count=1, flags=re.S),
            "items": items, "applied": _load_applied().get(stem, {}), "has_suggestions": sug is not None}


def apply_memo_item(stem: str, index: int, action: str) -> dict:
    """Run one Kikitori suggestion (or dismiss it). Every action goes through the normal, validated paths."""
    detail = memo_detail(stem)
    if not 0 <= index < len(detail["items"]):
        raise ValueError("bad item index")
    key = str(index)
    if key in detail["applied"]:
        raise ValueError("この提案は処理済みです")
    item = detail["items"][index]
    result: dict[str, Any] = {"action": action}
    if action == "apply":
        route = item["route"]
        if route == "task":
            result["job"] = start_manual_job("text", text=item["text"] or item["summary"])["job"]  # still needs approval
        elif route == "attendance":
            if item["kind"] not in ATT_KINDS:
                raise ValueError(f"種別が不正: {item['kind']}")
            if item["who"] == "self":
                result["path"] = add_my_attendance(item["date"], item["kind"], item["hours"], "")
            else:
                datetime.strptime(item["date"], "%Y-%m-%d")  # raises ValueError on a bad date
                line = f"{item['name']} {item['date']} {item['kind']} {item['hours']}".strip()
                result["path"] = save_paste("memo", line)
        elif route == "knowledge":
            result["path"] = create_request(f"{item['request'] or item['summary']}\n\n（メモ {stem} より）")
    elif action != "dismiss":
        raise ValueError("action は apply か dismiss")
    applied = _load_applied()
    applied.setdefault(stem, {})[key] = result | {"at": datetime.now().isoformat(timespec="seconds")}
    APPLIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPLIED_FILE.write_text(json.dumps(applied, ensure_ascii=False, indent=1), encoding="utf-8")
    if len(applied[stem]) >= len(detail["items"]):  # every suggestion handled -> the memo is done
        set_status(f"workspace/memos/{stem}.md", "done")
    return result


# ---------- notifications (right-hand column of the dashboard) ----------
DECISIONS_DIR = WORKSPACE_DIR / "kihyo" / "decisions"
ACKS_FILE = WORKSPACE_DIR / "notifications" / "acks.json"
MAX_DECISION_FILES = 6  # ~3 days of am/pm cycles


def _load_acks() -> dict[str, str]:
    try:
        data = json.loads(ACKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def ack_notifications(ids: list[str]) -> int:
    """Mark notifications as seen ("OK") and keep a snapshot of what was acknowledged.

    The snapshot means an OK'd item stays viewable ("確認済みも表示") even after its source
    decision file has rolled out of the feed window.
    """
    acks = _load_acks()
    now = datetime.now().isoformat(timespec="seconds")
    current = {i["id"]: i for i in notifications(include_acked=True)["items"]}
    for nid in ids:
        if isinstance(nid, str) and re.fullmatch(r"[0-9a-f]{16}", nid):
            item = current.get(nid, {})
            acks[nid] = {"acked_at": now, **{k: item.get(k) for k in
                         ("kind", "title", "group", "when", "detail", "link", "tab", "path", "extra") if k in item}}
    ACKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACKS_FILE.write_text(json.dumps(acks, indent=1), encoding="utf-8")
    return len(acks)


def _nid(*parts: str) -> str:
    import hashlib

    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _task_link(task_id: str) -> str:
    """Planner URL from `ticketing.task_url` (a copied task link with the id replaced by {id})."""
    template = str((load_fleet().ticketing or {}).get("task_url") or "")
    return template.replace("{id}", task_id) if task_id and "{id}" in template else ""


def _state_index() -> dict[str, dict]:
    """Normalised title -> planner_state card of the latest export (for links and status)."""
    base = (load_fleet().ticketing or {}).get("base")
    f = _latest_planner_state(Path(base).expanduser()) if base else None
    return {_norm_title(s.get("title")): s for s in (_cached_json(f) or []) if isinstance(s, dict)} if f else {}


def fmt_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M")


def notifications(include_acked: bool = False, samples: bool = True) -> dict:
    acks = _load_acks()
    state = _state_index()

    def link_for_title(title: str) -> str:
        return _task_link(str((state.get(_norm_title(title)) or {}).get("id") or ""))

    items: list[dict] = []

    def add(kind: str, title: str, *, key: str, when: str = "", detail: str = "", link: str = "",
            tab: str = "", path: str = "", group: str = "", extra: list | None = None) -> None:
        nid = _nid(kind, key, title)
        items.append({"id": nid, "kind": kind, "title": title, "when": when, "detail": detail, "link": link,
                      "tab": tab, "path": path, "group": group, "extra": extra or [], "acked": nid in acks})

    files = sorted(DECISIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_DECISION_FILES]
    for f in files:
        d = _cached_json(f)
        if not isinstance(d, dict):
            continue
        group = f"Kihyo {d.get('date', '')} {d.get('cycle', '')}".strip()
        when = str(d.get("generated_at") or "")[:16]

        def lst(k: str) -> list[dict]:  # agent output is untrusted: normalise to a list of dicts
            v = d.get(k)
            if not isinstance(v, list):
                return []
            return [x if isinstance(x, dict) else {"title": str(x)} for x in v if isinstance(x, (dict, str))]

        if d.get("outcome") == "escalated":
            add("escalated", str(d.get("message") or "ソース不足で起票を見送り"), key=f.name, when=when, group=group)
        for x in lst("needs_attention"):
            add("attention", str(x.get("title", "")), key=f.name, when=when, group=group)
        for x in lst("created"):
            title = str(x.get("title", ""))
            meta = " / ".join(v for v in (str(x.get("assignee") or ""), str(x.get("due") or ""), str(x.get("priority") or "")) if v)
            add("created", title, key=f.name, when=when, group=group, link=link_for_title(title), detail=meta)
        for x in lst("updates"):
            title = str(x.get("title", ""))
            add("close" if x.get("kind") == "close" else "append", title, key=f.name, when=when, group=group,
                detail=str(x.get("note") or ""),
                link=_task_link(str(x.get("planner_task_id") or "")) or link_for_title(title))
        dropped = lst("dropped")
        for x in (x for x in dropped if x.get("kind") == "unclear"):
            add("unclear", str(x.get("title", "")), key=f.name, when=when, group=group, detail=str(x.get("reason") or ""))
        dups = [x for x in dropped if x.get("kind") != "unclear"]
        if dups:  # certain duplicates are safe: fold them into a single row
            add("dup", f"重複で Drop {len(dups)} 件", key=f.name, when=when, group=group,
                extra=[{"title": str(x.get("title", "")), "detail": str(x.get("ground") or x.get("reason") or ""),
                        "link": link_for_title(str(x.get("ground") or ""))} for x in dups])
        for x in lst("unassigned"):
            title = str(x.get("title", ""))
            add("unassigned", title, key=f.name, when=when, group=group, link=link_for_title(title))

    if not files:  # before Kihyo's first run in Nakama: point at the pipeline's own Step 7 log
        base = (load_fleet().ticketing or {}).get("base")
        logs = sorted((Path(base).expanduser() / "logs").glob("report_*.md"), key=lambda p: p.name, reverse=True) if base else []
        if logs:
            try:
                content = logs[0].read_text(encoding="utf-8-sig", errors="replace")[:20000]
            except OSError as exc:
                content = f"読めません: {exc}"
            add("log", f"起票ログ {logs[0].stem.removeprefix('report_')}", key=logs[0].name, detail=content,
                group="Kihyo（移行前のログ）")

    from . import overtime  # 18:00 overtime reminder (code, not an agent)

    now = datetime.now(ZoneInfo(load_fleet().timezone))
    if overtime.reminder_due(now):
        add("overtime", f"残業申請を上げてください（{now:%m/%d}）", key=f"overtime-{now.date().isoformat()}",
            when=now.isoformat(timespec="minutes"), tab="overtime", group="リマインド",
            detail="勤怠 > 残業 で今日の残業（開始・終了・理由）を記録すると申請文をコピーできます。残業しないなら OK で消してください。")
    for a in _list_kind("action_items", samples):
        if a["meta"].get("status") == "open":
            add("action", a["title"] or a["name"], key=a["path"], when=fmt_mtime(a["mtime"]), tab="action_items",
                path=a["path"], group="要対応", detail=a["meta"].get("source", ""))

    if include_acked:  # older OK'd items whose source is no longer in the feed window
        live = {i["id"] for i in items}
        for nid, snap in sorted(acks.items(), key=lambda kv: str(kv[1].get("acked_at") if isinstance(kv[1], dict) else kv[1]), reverse=True):
            if nid in live or not isinstance(snap, dict) or not snap.get("title"):
                continue
            items.append({"id": nid, "kind": snap.get("kind", "log"), "title": snap["title"], "when": snap.get("when") or "",
                          "detail": snap.get("detail") or "", "link": snap.get("link") or "", "tab": snap.get("tab") or "",
                          "path": snap.get("path") or "", "group": f"確認済み（{str(snap.get('acked_at', ''))[:10]} OK）",
                          "extra": snap.get("extra") or [], "acked": True})
    unacked = sum(1 for i in items if not i["acked"])
    shown = [i for i in items if include_acked or not i["acked"]]
    return {"items": shown, "unacked": unacked, "total": len(items),
            "has_task_url": "{id}" in str((load_fleet().ticketing or {}).get("task_url") or "")}


# ---------- Kihyo page: history, manual ticketing with validation + approval ----------
KIHYO_DIR = WORKSPACE_DIR / "kihyo"
MANUAL_DIR = KIHYO_DIR / "manual"
ABOUT_MD = KIHYO_DIR / "about.md"  # background / design notes (local only, git-ignored)
JOB_RE = re.compile(r"\d{8}-\d{6}")
REPORT_RE = re.compile(r"report_[\w\-]+\.md")


def _ticket_paths():
    from .helpers.ticketing import Paths

    base = (load_fleet().ticketing or {}).get("base")
    if not base:
        raise ValueError("ticketing.base が未設定です")
    return Paths.from_config(load_fleet().ticketing)


def _job_dir(job: str) -> Path:
    if not JOB_RE.fullmatch(job or ""):
        raise ValueError("bad job id")
    return MANUAL_DIR / job


def _next_fire(spec, tz) -> str | None:
    from apscheduler.triggers.cron import CronTrigger

    now = datetime.now(tz)
    fires = []
    for expr in spec.schedule:
        try:
            nxt = CronTrigger.from_crontab(expr, timezone=tz).get_next_fire_time(None, now)
        except ValueError:
            continue
        if nxt:
            fires.append(nxt)
    return min(fires).strftime("%m-%d %H:%M") if fires else None


def kihyo_overview() -> dict:
    fleet = load_fleet()
    spec = fleet.agents.get("kihyo")
    tz = ZoneInfo(fleet.timezone)
    paths = _ticket_paths()
    last = last_success("kihyo")
    cycles = []
    for f in sorted((KIHYO_DIR / "decisions").glob("*.json"), key=lambda p: p.name, reverse=True)[:30]:
        d = _cached_json(f) or {}
        if isinstance(d, dict):
            dropped = d.get("dropped") if isinstance(d.get("dropped"), list) else []
            cycles.append({"name": f.stem, "outcome": str(d.get("outcome") or ""), "message": str(d.get("message") or ""),
                           "created": len(d.get("created") or []) if isinstance(d.get("created"), list) else 0,
                           "dropped": len(dropped)})
    logs = sorted(paths.logs.glob("report_*.md"), key=lambda p: p.name, reverse=True)[:40]
    jobs = []
    for d in sorted(MANUAL_DIR.glob("*"), key=lambda p: p.name, reverse=True)[:20]:
        if d.is_dir() and JOB_RE.fullmatch(d.name):
            meta = _cached_json(d / "job.json") or {}
            jobs.append({"job": d.name, "mode": meta.get("mode", "text"), "label": meta.get("label", ""),
                         "status": _job_status(d)})
    return {
        "enabled": bool(spec and spec.enabled), "next": _next_fire(spec, tz) if spec and spec.enabled else None,
        "last_success": last.astimezone(tz).isoformat(timespec="minutes") if last else None,
        "running": _is_running("kihyo"), "cycles": cycles,
        "logs": [{"name": p.name, "mtime": fmt_mtime(p.stat().st_mtime)} for p in logs],
        "jobs": jobs, "about": ABOUT_MD.read_text(encoding="utf-8") if ABOUT_MD.exists() else "",
        "root_ticket": next((p.name for p in paths.tickets.glob("tickets_*.json")), None),
    }


def _job_status(d: Path) -> str:
    if (d / "promoted.json").exists():
        return "promoted"
    if (d / "draft.json").exists():
        return "ready"
    if _is_running("kihyo") and (d / "job.json").exists():
        return "running"
    return "failed" if (d / "job.json").exists() and time.time() - (d / "job.json").stat().st_mtime > 1800 else "running"


def kihyo_log(name: str) -> dict:
    if not REPORT_RE.fullmatch(name or ""):
        raise ValueError("bad log name")
    path = _ticket_paths().logs / name
    return {"name": name, "content": path.read_text(encoding="utf-8-sig", errors="replace")[:60000]}


def start_manual_job(mode: str, text: str = "", cycle: str = "") -> dict:
    """Create a job folder and start Kihyo in review mode (no promotion possible from the agent side)."""
    if mode not in ("text", "cycle"):
        raise ValueError("mode は text か cycle")
    if mode == "text" and not text.strip():
        raise ValueError("議事メモ・タスクのテキストを貼り付けてください")
    if mode == "cycle" and cycle not in ("am", "pm"):
        raise ValueError("cycle は am か pm")
    if _is_running("kihyo"):
        raise ValueError("kihyo は実行中です。終わってからもう一度どうぞ")
    tz = ZoneInfo(load_fleet().timezone)
    now = datetime.now(tz)
    job = now.strftime("%Y%m%d-%H%M%S")
    d = _job_dir(job)
    d.mkdir(parents=True, exist_ok=False)
    rel = f"workspace/kihyo/manual/{job}"
    if mode == "text":
        (d / "input.md").write_text(text.strip() + "\n", encoding="utf-8")
        source = (f"入力は `{rel}/input.md`（ユーザーが貼った議事メモやタスクの箇条書き）。当日の議事 html は使わない。"
                  "手順書 Step 0/1 の「当日ソース必須・無ければエスカレーション」はこの入力には適用しない"
                  "（planner_state は dedup のため必ず最新を使う）。")
        label = text.strip().splitlines()[0][:40]
    else:
        source = (f"入力は手順書どおり、今日（{now.date().isoformat()}）の {cycle} サイクルのソース（議事 html と planner_state）。"
                  "当日ソースが無ければ draft を作らず dropped.json にその旨だけ書く。")
        label = f"{now.date().isoformat()} {cycle} の再現（昇格なし）"
    (d / "job.json").write_text(json.dumps({"mode": mode, "cycle": cycle or "manual", "label": label,
                                            "created": now.isoformat(timespec="seconds")}, ensure_ascii=False), encoding="utf-8")
    extra = (
        f"## 手動起票ジョブ {job}（この節が手順書より優先）\n"
        f"- {source}\n"
        "- 手順書の Step 1〜5（抽出、dedup、Step 3G の完了済み逆引き、roster 解決、close/追記）を通常どおり行う。\n"
        f"- 結果は手順書 Step 6 のスキーマで `{rel}/draft.json` に書く（cycle は \"{cycle or 'manual'}\"、"
        "status は \"draft\"、review_required は false）。\n"
        f"- Drop した項目は `{rel}/dropped.json` に `[{{\"title\", \"kind\": \"dup|unclear\", \"reason\", \"ground\"}}]` で書く。\n"
        "- Step 0.5 の退避、Step 6.5 の昇格、BASE の logs/ への書き込み、workspace/kihyo/decisions への書き込みはしない"
        "（このジョブはユーザーが Nakama 上で確認してから昇格する）。"
    )
    _spawned["kihyo"] = spawn_agent("kihyo", force=True, extra_args=["--review", "--extra", extra])
    return {"job": job}


def manual_job(job: str) -> dict:
    from .helpers import ticket_review as tr

    d = _job_dir(job)
    if not d.exists():
        raise FileNotFoundError(job)
    meta = _cached_json(d / "job.json") or {}
    out: dict[str, Any] = {"job": job, "status": _job_status(d), "meta": meta,
                           "input": (d / "input.md").read_text(encoding="utf-8")[:20000] if (d / "input.md").exists() else "",
                           "dropped": _cached_json(d / "dropped.json") or [], "promoted": _cached_json(d / "promoted.json")}
    if (d / "draft.json").exists():
        paths = _ticket_paths()
        out["validation"] = tr.validate(paths, _cached_json(d / "draft.json"))
        if meta.get("mode") == "cycle":  # compare with what the scheduled pipeline produced for the same cycle
            date_s = str(meta.get("created", ""))[:10]
            other = _cached_json(paths.draft / f"tickets_{date_s}_{meta.get('cycle')}.json") or {}
            out["compare"] = {"file": f"draft/tickets_{date_s}_{meta.get('cycle')}.json",
                              "tasks": [str(t.get("title")) for t in other.get("tasks") or [] if isinstance(t, dict)],
                              "closes": [str(c.get("title")) for c in other.get("close_candidates") or [] if isinstance(c, dict)]}
    return out


def approve_manual_job(job: str, tasks: list, closes: list, confirm: bool) -> dict:
    from .helpers import ticket_review as tr

    if confirm is not True:
        raise ValueError("確認が必要です")
    d = _job_dir(job)
    if (d / "promoted.json").exists():
        raise ValueError("このジョブは昇格済みです（二重起票防止）")
    meta = _cached_json(d / "job.json") or {}
    if meta.get("mode") == "cycle":
        raise ValueError("サイクル再現ジョブは検証用なので昇格できません（定時の Kihyo が本番を担当）")
    draft = _cached_json(d / "draft.json")
    if not isinstance(draft, dict):
        raise ValueError("draft.json がありません")
    paths = _ticket_paths()
    t_idx = [int(i) for i in tasks if isinstance(i, int)]
    c_idx = [int(i) for i in closes if isinstance(i, int)]
    result = tr.promote_manual(paths, draft, t_idx, c_idx, job)
    skipped = [str(t.get("title")) for i, t in enumerate(draft.get("tasks") or []) if isinstance(t, dict) and i not in t_idx]
    log_path = tr.write_cycle_log(paths, job, result, skipped)
    (d / "promoted.json").write_text(json.dumps({k: v for k, v in result.items() if k != "final"} | {"log": log_path.name},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    # Notify the dashboard feed in the same shape as scheduled cycles.
    dropped = [x for x in (_cached_json(d / "dropped.json") or []) if isinstance(x, dict)]
    decision = {
        "date": date_today(), "cycle": f"manual {job[-6:-2]}", "generated_at": datetime.now().isoformat(timespec="minutes"),
        "outcome": "promoted", "message": f"手動起票: 新規 {result['tasks']}・追記/Close {result['closes']}（未承認 {len(skipped)}）",
        "created": [{"title": t.get("title"), "assignee": t.get("assignee_hint") or "", "due": t.get("due") or ""} for t in result["final"]["tasks"]],
        "updates": [{"title": c.get("title"), "kind": "close" if "Close" in str(c.get("note")) else "append",
                     "planner_task_id": c.get("planner_task_id")} for c in result["final"]["close_candidates"]],
        "dropped": dropped + [{"title": s, "kind": "dup", "reason": "ユーザーが承認しなかった"} for s in skipped],
        "unassigned": [t.get("title") for t in result["final"]["tasks"] if not t.get("assignee_id")],
        "needs_attention": [],
    }
    (KIHYO_DIR / "decisions").mkdir(parents=True, exist_ok=True)
    (KIHYO_DIR / "decisions" / f"{date_today()}_manual_{job}.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=1), encoding="utf-8")
    return {k: v for k, v in result.items() if k != "final"} | {"log": log_path.name}


def date_today() -> str:
    return datetime.now(ZoneInfo(load_fleet().timezone)).date().isoformat()


# ---------- costs page ----------
PLANS_HELP = """\
- **① 実際の利用（API 換算）**: Nakama の各実行で Claude CLI が報告する推定額の合計。従量課金の API で動かした場合の目安。
- **ライセンス**: `config/fleet.yaml` の `cost_plans` で定義（設定タブで編集）。月額 =
  `基本料 + 追加利用 − 会社負担`（会社負担は基本料＋追加利用を上限）。
  - `included`: 基本料に含まれる利用額（API 換算、ドル）。空なら「基本料内で賄われる」とみなし追加利用 0。
  - `overage_rate`: included を超えた API 換算 1 ドルあたりの追加料金。
- 社内ダッシュボードの数値（Nakama 以外の利用も含む）とは一致しません。Nakama 分の比較用です。
"""


def _plan_cost(plan: dict, usage: float) -> tuple[float | None, str]:
    base = plan.get("base_fee")
    if base is None:
        return None, "規定が未入力（設定タブの fleet.yaml → cost_plans）"
    base = float(base)
    included, rate = plan.get("included"), float(plan.get("overage_rate") or 0)
    extra = max(0.0, usage - float(included)) * rate if included is not None else 0.0
    gross = base + extra
    funding = min(float(plan.get("central_funding") or 0), gross)
    return round(gross - funding, 2), f"基本 ${base:.0f} + 追加 ${extra:.2f} − 会社負担 ${funding:.0f}"


def costs_summary() -> dict:
    import calendar

    from .state import COSTS_FILE

    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    today = datetime.now(tz).date()
    month = today.strftime("%Y-%m")
    per_day: dict[str, float] = {}
    by_agent: dict[str, float] = {}
    runs_by_agent: dict[str, int] = {}
    try:
        lines = COSTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
            d = datetime.fromisoformat(row["ts"]).astimezone(tz).date()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if d.strftime("%Y-%m") != month:
            continue
        usd = float(row.get("cost_usd") or 0)
        per_day[d.isoformat()] = per_day.get(d.isoformat(), 0.0) + usd
        agent = str(row.get("agent"))
        by_agent[agent] = by_agent.get(agent, 0.0) + usd
        runs_by_agent[agent] = runs_by_agent.get(agent, 0) + 1
    total = sum(per_day.values())
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Average over the days since the first recorded run this month (a fleet started mid-month
    # would otherwise look nearly free), then scale to the whole month.
    first = min((int(k[-2:]) for k in per_day), default=today.day)
    span = max(1, today.day - first + 1)
    forecast = total / span * days_in_month
    plans = []
    for plan in fleet.cost_plans or []:
        if isinstance(plan, dict):
            monthly, formula = _plan_cost(plan, forecast)
            plans.append({"name": str(plan.get("name") or "plan"), "monthly": monthly, "formula": formula,
                          "note": str(plan.get("note") or "")})
    days = [{"date": f"{month}-{d:02d}", "total": round(per_day.get(f"{month}-{d:02d}", 0.0), 4)} for d in range(1, today.day + 1)]
    return {"month": month, "month_total": round(total, 4), "month_forecast": round(forecast, 2),
            "today": round(per_day.get(today.isoformat(), 0.0), 4), "month_runs": sum(runs_by_agent.values()),
            "days": days, "by_agent": {k: round(v, 4) for k, v in by_agent.items()}, "runs_by_agent": runs_by_agent,
            "plans": plans, "plans_help": PLANS_HELP}


# ---------- today's schedule (timeline on the dashboard) ----------

def schedule_today() -> dict:
    """Planned fire times (from cron) and recorded runs for every agent, for today in the fleet timezone."""
    from apscheduler.triggers.cron import CronTrigger

    from .state import COSTS_FILE

    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59)
    runs: dict[str, list[dict]] = {}
    try:
        for line in COSTS_FILE.read_text(encoding="utf-8").splitlines()[-3000:]:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row["ts"]).astimezone(tz)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if start <= ts <= end:
                runs.setdefault(str(row.get("agent")), []).append(
                    {"t": ts.strftime("%H:%M"), "error": bool(row.get("is_error"))})
    except OSError:
        pass
    rows = []
    for spec in fleet.agents.values():
        planned: list[str] = []
        if spec.enabled:
            for expr in spec.schedule:
                try:
                    trig = CronTrigger.from_crontab(expr, timezone=fleet.timezone)
                except ValueError:
                    continue
                prev, fire = None, trig.get_next_fire_time(None, start)
                while fire and fire <= end and len(planned) < 200:
                    planned.append(fire.strftime("%H:%M"))
                    prev, fire = fire, trig.get_next_fire_time(fire, fire + timedelta(seconds=1))
        rows.append({"name": spec.name, "enabled": spec.enabled, "planned": sorted(set(planned)),
                     "runs": runs.get(spec.name, []), "trigger": bool(spec.trigger_paths)})
    return {"now": now.strftime("%H:%M"), "agents": rows}


# ---------- agent flow (diagram on the dashboard) ----------

def _last_runs() -> dict[str, dict]:
    """Last recorded run per agent from logs/costs.jsonl (skipped runs are not recorded)."""
    from .state import COSTS_FILE

    last: dict[str, dict] = {}
    try:
        lines = COSTS_FILE.read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return last
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        last[str(row.get("agent"))] = {"ts": str(row.get("ts", ""))[:16], "error": bool(row.get("is_error"))}
    return last


def _queues(fleet, samples: bool = True) -> dict[str, int]:
    """Work waiting in front of each agent / for the owner. This is where things pile up."""
    from .state import changed_files

    def count(kind: str, pred) -> int:
        try:
            return sum(1 for i in _list_kind(kind, samples) if pred(i["meta"]))
        except OSError:
            return 0

    q = {
        "furiwake": count("leads", lambda m: m.get("status") == "new"),
        "sabaki": count("leads", lambda m: m.get("status") == "assigned" and m.get("owner") == fleet.owner.get("member_id")),
        "henji": count("inbox", lambda m: m.get("status") == "new"),
        "coordinator": count("requests", lambda m: m.get("status") == "open"),
        "you_drafts": count("drafts", lambda m: True),
        "you_actions": count("action_items", lambda m: m.get("status") == "open"),
    }
    spec = fleet.agents.get("kintai")
    if spec and spec.trigger_paths:
        q["kintai"] = len(changed_files(spec.trigger_paths, last_success("kintai")))
    return q


CALENDAR_JSON = ATTENDANCE_DIR / "outbox" / "calendar.json"
WEEKDAYS_JA = "月火水木金土日"


def attendance_calendar(days: int = 14, offset: int = 0) -> dict:
    """`days` days starting `offset` days from today, from Kintai's calendar.json, ready to draw as a grid."""
    days, offset = max(1, min(days, 120)), max(-120, min(offset, 120))
    tz = ZoneInfo(load_fleet().timezone)
    today = datetime.now(tz).date()
    span = [today.fromordinal(today.toordinal() + offset + i) for i in range(days)]
    result = {
        "days": [{"date": d.isoformat(), "label": f"{d.month}/{d.day}", "wd": WEEKDAYS_JA[d.weekday()],
                  "weekend": d.weekday() >= 5} for d in span],
        "members": [], "entries": [], "generated": None, "error": None,
    }
    if not CALENDAR_JSON.exists():
        result["error"] = "まだありません。Kintai を実行すると表示されます。"
        return result
    try:
        data = json.loads(CALENDAR_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # agent output is untrusted: degrade, don't crash
        result["error"] = f"calendar.json を読めません: {exc}"
        return result
    wanted = {d["date"] for d in result["days"]}
    entries = [
        {k: str(e.get(k, "")) for k in ("date", "member", "kind", "hours", "level")}
        for e in data.get("entries") or [] if isinstance(e, dict) and str(e.get("date")) in wanted
    ]
    names = [str(m.get("name")) for m in data.get("members") or [] if isinstance(m, dict) and m.get("name")]
    groups = {str(m.get("name")): str(m.get("group") or "") for m in data.get("members") or [] if isinstance(m, dict)}
    for e in entries:  # anyone mentioned but missing from members still gets a row
        if e["member"] not in names:
            names.append(e["member"])
    result.update(
        members=[{"name": n, "group": groups.get(n, "")} for n in names],
        entries=entries, generated=data.get("generated"),
    )
    return result


def set_status(rel: str, status: str) -> None:
    path = _safe_path(rel)
    allowed = _editable(path) or None
    if allowed is None or status not in allowed:
        raise PermissionError(f"status '{status}' cannot be set on {rel}")
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^(---\r?\n)(.*?)(\r?\n---)", text, re.S)
    if not m:
        raise ValueError("front matter not found")
    head = m.group(2)
    if re.search(r"^status:.*$", head, re.M):
        head = re.sub(r"^status:.*$", f"status: {status}", head, count=1, flags=re.M)
    else:
        head = f"status: {status}\n{head}"
    path.write_text(m.group(1) + head + m.group(3) + text[m.end():], encoding="utf-8")


def tail_log(lines: int = 200) -> str:
    path = LOGS_DIR / "nakama.log"
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 128 * 1024))
        data = f.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])


# ---------- HTTP ----------

class _Handler(BaseHTTPRequestHandler):
    index_html: str = ""
    token: str = ""
    allowed_hosts: set[str] = set()
    password: str = ""

    def log_message(self, fmt, *args):  # route access logs to our logger at debug level
        log.debug("ui %s - %s", self.address_string(), fmt % args)

    # --- guards ---
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in self.allowed_hosts

    def _basic_ok(self) -> bool:
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(pw, self.password)

    def _guard(self, api: bool) -> bool:
        if not self._host_ok():
            self._send(403, {"error": "host not allowed (set NAKAMA_UI_ALLOW_HOSTS)"})
            return False
        if not self._basic_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="nakama"')
            self.end_headers()
            return False
        if api and not hmac.compare_digest(self.headers.get("X-Nakama-Token", ""), self.token):
            self._send(403, {"error": "bad token (reload the page)"})
            return False
        return True

    # --- responses ---
    def _send(self, code: int, body, content_type: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self) -> dict:
        # 1 MB is plenty for a pasted month-long Teams thread while still bounding memory.
        length = min(int(self.headers.get("Content-Length") or 0), 1024 * 1024)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _dispatch(self, fn) -> None:
        try:
            self._send(200, fn())
        except (ValueError, KeyError, chat.ChatError, NoteError) as exc:
            self._send(400, {"error": str(exc)})
        except PermissionError as exc:
            self._send(403, {"error": f"forbidden: {exc}"})
        except FileNotFoundError as exc:
            self._send(404, {"error": f"not found: {exc}"})
        except ConfigError as exc:
            self._send(500, {"error": f"config error: {exc}"})
        except Exception as exc:  # noqa: BLE001 - never kill the server thread
            log.exception("ui handler failed")
            self._send(500, {"error": f"internal error: {exc}"})

    # --- routes ---
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            if not self._guard(api=False):
                return
            html = self.index_html.replace("__NAKAMA_TOKEN__", self.token)
            self._send(200, html.encode("utf-8"), "text/html")
            return
        if not url.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        if not self._guard(api=True):
            return

        def list_kind():
            if q.get("kind") not in KINDS:
                raise ValueError(f"bad kind: {q.get('kind')}")
            return _list_kind(q["kind"], q.get("samples", "1") != "0")

        if url.path == "/api/overview":
            self._dispatch(lambda: overview(q.get("samples", "1") != "0"))
        elif url.path == "/api/list":
            self._dispatch(list_kind)
        elif url.path == "/api/file":
            def read():
                path = _safe_path(q["path"])
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError("file too large to show")
                return {"path": q["path"], "content": path.read_text(encoding="utf-8", errors="replace"),
                        "editable_status": sorted(_editable(path))}
            self._dispatch(read)
        elif url.path == "/api/guide":
            self._dispatch(lambda: {"content": GUIDE_MD.read_text(encoding="utf-8")})
        elif url.path == "/api/attendance/calendar":
            self._dispatch(lambda: attendance_calendar(int(q.get("days", 14)), int(q.get("offset", 0))))
        elif url.path == "/api/kihyo":
            self._dispatch(kihyo_overview)
        elif url.path == "/api/kihyo/log":
            self._dispatch(lambda: kihyo_log(q.get("name", "")))
        elif url.path == "/api/chats":
            self._dispatch(lambda: {"chats": chat.list_chats()})
        elif url.path == "/api/chats/events":
            self._dispatch(lambda: {"events": chat.events(q.get("chat", ""), int(q.get("after", 0))),
                                    "running": _chat_running(q.get("chat", ""))})
        elif url.path == "/api/memo/detail":
            self._dispatch(lambda: memo_detail(q.get("memo", "")))
        elif url.path == "/api/kihyo/job":
            self._dispatch(lambda: manual_job(q.get("job", "")))
        elif url.path == "/api/costs":
            self._dispatch(costs_summary)
        elif url.path == "/api/schedule":
            self._dispatch(schedule_today)
        elif url.path == "/api/notifications":
            self._dispatch(lambda: notifications(q.get("acked") == "1", q.get("samples", "1") != "0"))
        elif url.path == "/api/routines":
            self._dispatch(lambda: routine_month(q.get("month", ""), q.get("all") != "1"))
        elif url.path == "/api/holidays":
            from . import bizdays
            self._dispatch(lambda: {"holidays": [{"date": d, **v} for d, v in sorted(bizdays.holidays().items())
                                                 if d >= q.get("from", "0000")]})
        elif url.path == "/api/notes":
            from . import routines
            self._dispatch(lambda: {"notes": routines.notes(q.get("key", ""))})
        elif url.path == "/api/tasks/all":
            self._dispatch(lambda: unified_tasks(q.get("done") == "1", q.get("month", "")))
        elif url.path == "/api/note/raw":
            from . import todo_note
            self._dispatch(todo_note.read_raw)
        elif url.path == "/api/devreq":
            from . import devreq
            self._dispatch(lambda: {"requests": devreq.load(), "statuses": devreq.STATUSES})
        elif url.path == "/api/cron/help":
            self._dispatch(lambda: {"help": cronja.HELP})
        elif url.path == "/api/overtime":
            from . import overtime
            self._dispatch(lambda: overtime.month_summary(_month_or_current(q.get("month", ""))))
        elif url.path == "/api/memo/history":
            self._dispatch(memo_history)
        elif url.path == "/api/routines/history":
            from . import routines
            self._dispatch(lambda: {"history": routines.history(q.get("id", ""), 6, _month_or_current(q.get("month", "")))})
        elif url.path == "/api/sources":
            self._dispatch(data_sources)
        elif url.path == "/api/tasks":
            self._dispatch(lambda: my_tasks(q.get("done") == "1"))
        elif url.path == "/api/settings":
            self._dispatch(settings_files)
        elif url.path == "/api/settings/file":
            self._dispatch(lambda: read_setting(q.get("path", "")))
        elif url.path == "/api/log":
            self._dispatch(lambda: {"log": tail_log()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not self._guard(api=True):
            return
        body = self._json_body()
        if url.path == "/api/run":
            self._dispatch(lambda: {"message": start_run(str(body.get("agent", "")))})
        elif url.path == "/api/request":
            self._dispatch(lambda: {"path": create_request(str(body.get("text", "")))})
        elif url.path == "/api/attendance/mine":
            self._dispatch(lambda: {"path": add_my_attendance(
                str(body.get("date", "")), str(body.get("kind", "")),
                str(body.get("hours", "")), str(body.get("memo", "")))})
        elif url.path == "/api/attendance/paste":
            self._dispatch(lambda: {"path": save_paste(str(body.get("source", "")), str(body.get("text", "")))})
        elif url.path == "/api/kihyo/manual":
            self._dispatch(lambda: start_manual_job(str(body.get("mode", "")), str(body.get("text", "")), str(body.get("cycle", ""))))
        elif url.path == "/api/kihyo/approve":
            self._dispatch(lambda: approve_manual_job(str(body.get("job", "")), list(body.get("tasks") or []),
                                                      list(body.get("closes") or []), body.get("confirm")))
        elif url.path == "/api/chats/new":
            self._dispatch(lambda: chat.new_chat(str(body.get("model") or chat.MODELS[0])))
        elif url.path == "/api/chats/send":
            self._dispatch(lambda: send_chat(str(body.get("chat", "")), str(body.get("text", ""))))
        elif url.path == "/api/tasks/done":
            self._dispatch(lambda: complete_task(body))
        elif url.path == "/api/devreq/add":
            from . import devreq
            self._dispatch(lambda: devreq.add(str(body.get("text", "")), note=str(body.get("note", ""))))
        elif url.path == "/api/devreq/update":
            from . import devreq
            self._dispatch(lambda: devreq.update(str(body.get("id", "")), body.get("status"), body.get("note"),
                                                 body.get("change"), body.get("usage")))
        elif url.path == "/api/profile":
            self._dispatch(lambda: save_profile(str(body.get("name", "")), str(body.get("signature", ""))))
        elif url.path == "/api/run_all":
            self._dispatch(start_run_all)
        elif url.path == "/api/overtime/add":
            from . import overtime
            self._dispatch(lambda: overtime.add(str(body.get("date", "")), str(body.get("start", "")),
                                                str(body.get("end", "")), str(body.get("reason", ""))))
        elif url.path == "/api/overtime/delete":
            from . import overtime
            self._dispatch(lambda: (overtime.delete(str(body.get("date", "")), str(body.get("start", ""))) or {"ok": True}))
        elif url.path == "/api/notify/test":
            from .notify import notify_all
            self._dispatch(lambda: notify_all(load_fleet().notify, "Nakama からのテスト通知です。", "Nakama テスト"))
        elif url.path == "/api/tasks/due":
            self._dispatch(lambda: set_task_due(body))
        elif url.path == "/api/note/save":
            from . import todo_note
            self._dispatch(lambda: todo_note.save_raw(str(body.get("text", "")), float(body.get("mtime") or 0)))
        elif url.path == "/api/note/add":
            self._dispatch(lambda: add_note_task(str(body.get("text", "")), bool(body.get("star"))))
        elif url.path == "/api/assist":
            self._dispatch(lambda: assist_prompt(str(body.get("kind", "")), dict(body.get("ref") or {})))
        elif url.path == "/api/routines/status":
            from . import routines
            self._dispatch(lambda: routines.set_progress(str(body.get("id", "")), str(body.get("month", "")), str(body.get("status", ""))))
        elif url.path == "/api/notes/add":
            from . import routines
            self._dispatch(lambda: routines.add_note(str(body.get("key", "")), str(body.get("text", "")), str(body.get("title", ""))))
        elif url.path == "/api/holidays/save":
            from . import bizdays
            self._dispatch(lambda: {"total": bizdays.upsert_holiday(str(body.get("date", "")), str(body.get("name", "")), str(body.get("note", "")))})
        elif url.path == "/api/holidays/delete":
            from . import bizdays
            self._dispatch(lambda: {"total": bizdays.delete_holiday(str(body.get("date", "")))})
        elif url.path == "/api/holidays/import":
            from . import bizdays
            self._dispatch(bizdays.import_cao_csv)
        elif url.path == "/api/memo/apply":
            self._dispatch(lambda: apply_memo_item(str(body.get("memo", "")), int(body.get("index", -1)), str(body.get("action", ""))))
        elif url.path == "/api/memo":
            self._dispatch(lambda: {"path": save_memo(str(body.get("text", "")))})
        elif url.path == "/api/notifications/ack":
            self._dispatch(lambda: {"acked": ack_notifications(list(body.get("ids") or []))})
        elif url.path == "/api/settings/save":
            self._dispatch(lambda: {"message": save_setting(str(body.get("path", "")), str(body.get("content", "")))})
        elif url.path == "/api/status":
            def update():
                set_status(str(body.get("path", "")), str(body.get("status", "")))
                return {"ok": True}
            self._dispatch(update)
        else:
            self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port silently and steal requests
    # (an old process keeps answering with old code). Fail loudly instead.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    extra = {h.strip().lower() for h in os.environ.get("NAKAMA_UI_ALLOW_HOSTS", "").split(",") if h.strip()}
    handler = type("NakamaHandler", (_Handler,), {
        # Read the page once so the HTML always matches this process's API (restart to update).
        "index_html": INDEX_HTML.read_text(encoding="utf-8"),
        "token": secrets.token_urlsafe(24),
        "allowed_hosts": {"localhost", "127.0.0.1", "::1"} | extra,
        "password": os.environ.get("NAKAMA_UI_PASSWORD", ""),
    })
    if host not in ("127.0.0.1", "localhost", "::1") and not handler.password:
        log.warning("UI listens on %s without NAKAMA_UI_PASSWORD; anyone allowed by Host can open it", host)
    return _Server((host, port), handler)


def serve_ui(host: str = "127.0.0.1", port: int = 8765, background: bool = False,
             open_browser: bool = False) -> ThreadingHTTPServer:
    server = make_server(host, port)  # the socket is bound here, so the browser can connect right away
    url = f"http://localhost:{port}/"
    log.info("Nakama UI: %s", url)
    if open_browser:
        import webbrowser

        try:
            if not webbrowser.open(url):
                log.warning("no browser available; open %s manually", url)
        except webbrowser.Error as exc:
            log.warning("could not open browser (%s); open %s manually", exc, url)
    def warm() -> None:  # read the synced ticket folder once so the first page load is fast
        try:
            my_tasks()
        except Exception:  # noqa: BLE001 - warming is best effort
            log.debug("cache warm-up failed", exc_info=True)

    threading.Thread(target=warm, name="nakama-warm", daemon=True).start()
    if background:
        threading.Thread(target=server.serve_forever, name="nakama-ui", daemon=True).start()
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("UI stopped")
    return server
