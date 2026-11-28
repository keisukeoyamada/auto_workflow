"""Local web dashboard: `python -m nakama ui` (or `serve --ui`). Standard library only.

Security model:
- Binds to 127.0.0.1 by default.
- Every /api call needs a per-process token embedded in the page (blocks cross-site requests).
- The Host header must be localhost or listed in NAKAMA_UI_ALLOW_HOSTS (blocks DNS rebinding).
- Optional login via NAKAMA_UI_PASSWORD (use it when exposing on a LAN, e.g. a NAS): a login page sets an
  HttpOnly session cookie (90 days); "ログアウト" drops it. Changing a password signs out its sessions.
- Optional guest login via NAKAMA_UI_GUEST_PASSWORD: a guest sees only NAKAMA_UI_GUEST_TABS (default
  "credit"); the server refuses every other API for that login, so editing the URL does not widen it.
- File access is limited to workspace/ and knowledge/; secrets (.env, config/) are never served.
"""
from __future__ import annotations

import base64
import hashlib
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
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote as url_quote
from zoneinfo import ZoneInfo

from .config import AGENTS_DIR, KNOWLEDGE_DIR, LOGS_DIR, ROOT, SAMPLES_DIR, TEMPLATES_DIR, WORKSPACE_DIR, ConfigError, load_fleet
from . import chat, cronja
from .todo_note import NoteError
from .vault import VaultError
from .wbs import WbsError
from .decks import DeckError
from .scripts import ScriptError
from .spawn import spawn_agent, spawn_nakama
from .state import LOCK_DIR, STALE_LOCK_SECONDS, last_success, spend_on

log = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).with_name("static") / "index.html"
GUIDE_MD = ROOT / "docs" / "GUIDE.md"
REFERENCES_DIR = ROOT / "references"   # q128: PDFs shown in the guide tab (git-ignored)
MAX_REFERENCE_BYTES = 30 * 1024 * 1024


def reference_pdfs() -> list[dict]:
    """PDFs directly under references/, newest first."""
    if not REFERENCES_DIR.is_dir():
        return []
    files = sorted(REFERENCES_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size} for p in files if p.is_file()]


def _reference_path(name: str) -> Path:
    """Resolve a reference by bare file name only (no sub-paths, no traversal)."""
    if not name or name != Path(name).name or not name.lower().endswith(".pdf"):
        raise PermissionError("bad reference name")
    path = REFERENCES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(name)
    if path.stat().st_size > MAX_REFERENCE_BYTES:
        raise PermissionError("reference too large")
    return path
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
        "features": {"credit": bool((fleet.credit or {}).get("enabled")),
                     "rongo": bool((fleet.rongo or {}).get("enabled"))},
        "queues": _queues(fleet, samples),
        "checks": _checks_nonblocking(datetime.now(tz).date()),
    }


_checks_last: dict = {}
_checks_busy = threading.Event()


def _checks_nonblocking(today: date) -> dict | None:
    """The overview is polled every 15 s and must stay fast: counts are refreshed in a thread, last value returned."""
    if not _checks_busy.is_set() and time.time() - _checks_last.get("at", 0) > 60:
        _checks_busy.set()

        def work():
            try:
                _checks_last.update(value=_check_counts(today), at=time.time())
            except Exception:  # noqa: BLE001
                log.exception("check counts failed")
            finally:
                _checks_busy.clear()

        threading.Thread(target=work, daemon=True).start()
    return _checks_last.get("value")


KIHYO_DETAIL_IN_FEED = False  # q120: created / append / close / dup rows are read in the Kihyo report instead


def add_reaction(body: dict) -> dict:
    """q123: a comment on a notice becomes a change request for Coordinator (runs in its batch, every 15 min)."""
    text, title = str(body.get("text", "")).strip(), str(body.get("title", "")).strip()
    if not text:
        raise ValueError("リアクションを書いてください")
    kind = str(body.get("kind", ""))
    path = create_request(f"エスカレへのリアクション（{kind}: {title[:80]}）\n\n{text}\n\n"
                          "→ 関係するエージェントの指示（agents/*/LOCAL.md）かナレッジに反映し、次回以降の判断・文面に活かすこと。"
                          "単発の対応で済むものは action_items に回すこと。")
    return {"ok": True, "path": path}


def _check_counts(today: date) -> dict:
    """q111: the numbers worth a glance besides the agent queues (same 5-minute cache as the escalation feed)."""
    from . import devreq, wbs

    iso = today.isoformat()
    try:
        tasks = _ttl(("notif-tasks", iso), 300, lambda: unified_tasks())["tasks"]
    except Exception:  # noqa: BLE001
        tasks = []
    live = [t for t in tasks if t["status"] not in ("done", "skip")]
    try:
        wbs_pending = len(wbs.load_pending())
    except Exception:  # noqa: BLE001
        wbs_pending = 0
    return {
        "overdue": sum(1 for t in live if t.get("due") and t["due"] < iso),
        "duetoday": sum(1 for t in live if t.get("due") == iso),
        "routine_left": sum(1 for t in live if t["source"] == "routine"),
        "wbs_pending": wbs_pending,
        "devreq_open": sum(1 for q in devreq.load() if q.get("status") in ("open", "partial")),
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
            planner_due = due
            due = _load_planner_due().get(key) or due
            in_month = due.startswith(month) or (current and (not due or due < month) and t["status"] != "done")
            if not in_month:
                continue
            rows.append({"source": "planner", "title": t["title"], "due": due, "planner_due": planner_due, "status": t["status"], "link": t.get("link", ""),
                         "priority": t.get("priority", ""), "memo": t.get("memo", ""), "key": key,
                         "notes": routines.note_count(key)})

    for r in routines.items_for_month(year, mon, today):
        if include_done or r["status"] not in ("done", "skip"):
            rows.append(r)

    try:
        from . import wbs

        for w in _ttl(("wbs-mine",), 60, lambda: wbs.my_items(today)):
            due = w.get("end") or w.get("start") or ""
            in_month = due.startswith(month) or (current and (not due or due < month) and not w["done"])
            if not in_month or (w["done"] and not include_done):
                continue
            key = routines.planner_key(f"wbs:{w['file']}:{w['sheet']}:{w['no']}")
            rows.append({"source": "wbs", "title": f"{w['no']} {w['title']}", "due": due, "status": "done" if w["done"] else (w["status"] or "open"),
                         "wbs_file": w["file"], "wbs_sheet": w["sheet"], "wbs_row": w["row"], "wbs_no": w["no"],
                         "memo": f"{w['file_name']} / {w['owner']}",
                         "key": key, "notes": routines.note_count(key)})
    except Exception as exc:  # noqa: BLE001 - a broken spreadsheet must not break the task list
        errors.append(f"WBS: {exc}")

    note = todo_note.load(include_done) if current else {"configured": False, "items": []}
    if note.get("error"):
        errors.append(note["error"])
    for i in note["items"]:
        title = (f"{i['parent']} › " if i["parent"] else "") + i["text"]
        rows.append({"source": "note", "title": title, "text": i["text"], "due": i.get("due", ""), "status": "done" if i["done"] else "open",
                     "star": i["star"], "section": i["section"], "line": i["line"], "raw": i["raw"],
                     "parent_line": i.get("parent_line", -1), "key": i["key"], "notes": routines.note_count(i["key"])})

    stars = _load_stars()
    related = _wbs_link_index()
    for r in rows:  # business-day label of the due date (e.g. "18営")
        try:
            d = date.fromisoformat(r["due"]) if r.get("due") else None
        except ValueError:
            d = None
        r["bd"] = bizdays.business_day_index(d) if d else None
        if r["source"] != "note":  # note rows carry ★ in the file itself
            r["star"] = bool(stars.get(r["key"]))
        if r["source"] != "wbs":
            r["related"] = _related_wbs(r["title"], related)
    order = {"open": 0, "todo": 0, "doing": 0, "review": 0, "pending": 1, "unknown": 1, "done": 2, "skip": 2}
    rows.sort(key=lambda r: (not r.get("star"), order.get(r["status"], 1), r.get("due") or "9999", r["title"]))
    rows = _nest_note_rows(rows)
    idx = bizdays.business_day_index(today)
    return {"tasks": rows, "error": " / ".join(errors) or None, "today": today.isoformat(), "month": month,
            "current": current, "bd": idx, "bd_total": len(bizdays.business_days_of_month(today.year, today.month)),
            "holiday": bizdays.holidays().get(today.isoformat(), {}).get("name", ""),
            "note_configured": note.get("configured", False) or bool(todo_note.note_path())}


def _nest_note_rows(rows: list[dict]) -> list[dict]:
    """Note children follow their parent (in file order) with depth for indenting, like the Markdown outline.
    A child whose parent is not in the list (done and hidden) keeps its place and the "parent › " prefix."""
    by_line = {r["line"]: r for r in rows if r["source"] == "note"}
    kids: dict[int, list[dict]] = {}
    for r in rows:
        if r["source"] == "note" and r.get("parent_line", -1) in by_line:
            kids.setdefault(r["parent_line"], []).append(r)
    out: list[dict] = []

    def emit(r: dict, depth: int) -> None:
        r["depth"] = depth
        if depth:
            r["title"] = r.get("text") or r["title"]  # the parent is shown right above
        out.append(r)
        for c in sorted(kids.get(r["line"], []), key=lambda x: x["line"]):
            emit(c, depth + 1)

    for r in rows:
        if r["source"] == "note" and r.get("parent_line", -1) in by_line:
            continue  # placed under its parent
        emit(r, 0) if r["source"] == "note" else out.append(r)
    return out


def add_note_child(body: dict) -> dict:
    from . import todo_note

    try:
        return todo_note.add_child(int(body.get("line", -1)), str(body.get("raw", "")), str(body.get("text", "")), bool(body.get("star")))
    except todo_note.NoteError as exc:
        raise ValueError(str(exc)) from exc


STARS_FILE = WORKSPACE_DIR / "tasks" / "stars.json"


def _load_stars() -> dict[str, bool]:
    data = _cached_json(STARS_FILE) if STARS_FILE.exists() else None
    return data if isinstance(data, dict) else {}


def star_task(body: dict) -> dict:
    """★ pins a task to the top. Note rows: ★ is written into the note line; others: stored in workspace/tasks/stars.json."""
    from . import todo_note

    star = body.get("star", True) is not False
    if body.get("source") == "note":
        try:
            return todo_note.set_star(int(body.get("line", -1)), str(body.get("raw", "")), star)
        except todo_note.NoteError as exc:
            raise ValueError(str(exc)) from exc
    key = str(body.get("key", ""))
    if not re.fullmatch(r"(routine-[\w\-]+-\d{4}-\d{2}|(planner|note)-[0-9a-f]{12})", key):
        raise ValueError("bad task key")
    stars = dict(_load_stars())
    if star:
        stars[key] = True
    else:
        stars.pop(key, None)
    STARS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STARS_FILE.write_text(json.dumps(stars, indent=1), encoding="utf-8")
    return {"key": key, "star": star}


def _tokens(text: str) -> set[str]:
    """ASCII words of 6+ chars, lower-cased and without separators (e.g. "Winserver" -> "winserver")."""
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", unicodedata.normalize("NFKC", text))}


def _wbs_link_index() -> list[dict]:
    """[{id, name, path_key}] of detected WBS files (cached with the WBS index)."""
    try:
        from . import wbs

        files = _ttl(("wbs-index",), 60, lambda: wbs.index(date.today()))
    except Exception:  # noqa: BLE001 - links are a convenience
        return []
    out = []
    for f in files:
        if f.get("is_wbs"):
            key = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", f["path"]).lower())
            out.append({"id": f["id"], "name": f["name"], "path_key": key})
    return out


def _related_wbs(title: str, index: list[dict]) -> list[dict]:
    toks = _tokens(title)
    return [{"id": w["id"], "name": w["name"]} for w in index if any(t in w["path_key"] for t in toks)][:3]


def completed_index(limit: int = 300) -> dict:
    """Everything marked done, from every place it is stored, newest first, with where it lives."""
    from . import agenda, devreq, reminders, routines, todo_note

    rows: list[dict] = []

    def add(source, title, when, where, tab="", path="", link="", extra=None):
        rows.append({"source": source, "title": title, "when": when or "", "where": where, "tab": tab, "path": path,
                     "link": link, **(extra or {})})

    # routines: every month file
    names = {r["id"]: r["name"] for r in routines.load_routines()}
    for f in sorted(routines.PROGRESS_DIR.glob("*.json"), reverse=True):
        for rid, p in (_cached_json(f) or {}).items():
            if isinstance(p, dict) and p.get("status") in ("done", "skip"):
                add("routine", f"{names.get(rid, rid)}（{f.stem}）" + ("・今月なし" if p["status"] == "skip" else ""),
                    p.get("updated", ""), f"workspace/routine/progress/{f.name}", tab="routine", extra={"rid": rid, "month": f.stem})
    # hand-written note: ■ lines
    try:
        note = todo_note.load(include_done=True)
        for i in note.get("items", []):
            if i["done"]:
                add("note", (f"{i['parent']} › " if i["parent"] else "") + i["text"], "", f"{note.get('path')}（{i['line'] + 1} 行目・■）",
                    tab="note", extra={"line": i["line"]})
    except Exception:  # noqa: BLE001
        pass
    # Planner (from the promotion history x latest planner_state)
    try:
        if (load_fleet().ticketing or {}).get("base"):
            for t in my_tasks(include_done=True)["tasks"]:
                if t["status"] == "done":
                    add("planner", t["title"], t.get("issued", ""), "Planner（planner_state で完了）", link=t.get("link", ""))
    except Exception:  # noqa: BLE001
        pass
    # WBS rows marked 完了
    try:
        from . import wbs

        for w in _ttl(("wbs-mine",), 60, lambda: wbs.my_items(date.today())):
            if w["done"]:
                add("wbs", f"{w['no']} {w['title']}", w.get("actual_end", ""), f"{w['file_name']}（{w['sheet']} {w['row']} 行目）",
                    tab="wbs", extra={"wbs_file": w["file"], "wbs_row": w["row"]})
    except Exception:  # noqa: BLE001
        pass
    for kind, label in (("action_items", "要対応"), ("requests", "変更依頼"), ("memos", "メモ")):
        for it in _list_kind(kind, True):
            if it["meta"].get("status") in ("done", "skipped", "closed"):
                add(kind, it["title"] or it["name"], fmt_mtime(it["mtime"]), it["path"], tab=kind, path=it["path"])
    for a in agenda.load():
        if a["status"] == "closed":
            add("agenda", f"{a['meeting']}: {a['topic']}", a.get("last", ""), "workspace/agenda/items.json", tab="agenda")
    for r in reminders.load():
        if r.get("done"):
            add("reminder", r["title"], r.get("done_at") or r["date"], "workspace/reminders.json（✓ 済んだ）", link=r.get("link", ""))
    for snap in _load_acks().values():  # notices you OK'd in the escalation feed
        if isinstance(snap, dict) and snap.get("title"):
            add("notice", snap["title"], str(snap.get("acked_at", "")), "エスカレで OK → 確認済み（workspace/notifications/acks.json）",
                tab=snap.get("tab") or "", path=snap.get("path") or "", link=snap.get("link") or "")
    for q in devreq.load():
        if q["status"] == "done":
            add("devreq", f"{q['id']} {q['text']}", q.get("updated", ""), "workspace/dev/requests.json", tab="devreq")
    rows.sort(key=lambda r: r["when"], reverse=True)
    from collections import Counter

    return {"rows": rows[:limit], "total": len(rows), "by_source": dict(Counter(r["source"] for r in rows))}


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
            {"what": "定義（ダッシュボードの正本）", "kind": "file", "path": str(WORKSPACE_DIR / "routine" / "routines.json")}],
        "holidays": read(HOLIDAYS_FILE.with_name("holidays.source.json")) + [
            {"what": "祝日一覧（ダッシュボードの正本）", "kind": "file", "path": str(HOLIDAYS_FILE)}],
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
    elif kind == "notice" and ref.get("kind") == "reminder":
        body = (f"次のリマインドは、自分が関係者に催促する立場のものです。送る文面を整えてください。\n\n内容: {str(ref.get('title', ''))[:300]}\n"
                f"リンク: {str(ref.get('link') or '')[:500]}\n補足: {str(ref.get('detail') or '')[:800]}\n今の文面案:\n{str(ref.get('draft') or '')[:1500]}\n\n"
                "お願い: ①Teams のチャネル投稿用（短く・柔らかく）②個別にメンションで送る用 ③締切を過ぎた場合の再催促用 の 3 案。"
                "相手を責めない言い回しで、リンクはそのまま残してください。最後に「こちらでいかがでしょう？」と確認してください。")
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
    from . import projects

    if projects.prompt_block():  # q076: always ground project work in the registered folders, cite paths
        body += ("\n\nプロジェクトに関わる内容なら、下の参照フォルダを先に調べ、根拠のファイルをフルパスで引用してください。\n"
                 + projects.prompt_block())
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
    if source == "planner":
        # PA cannot update a card's due date, so Nakama keeps its own date and reminds the owner to fix Planner too
        key = str(body.get("key", ""))
        if not key:
            raise ValueError("key がありません")
        data = _load_planner_due()
        if due:
            date.fromisoformat(due)
            data[key] = due
        else:
            data.pop(key, None)
        PLANNER_DUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLANNER_DUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "due": due, "note": "ダッシュボード上の期日を変えました。Planner 側も直してください"}
    if source == "wbs":
        from . import wbs

        try:
            r = wbs.stage(str(body.get("wbs_file", "")), str(body.get("wbs_sheet", "")), int(body.get("wbs_row", 0)), "end", due,
                          expected_no=str(body.get("wbs_no", "")))
        except wbs.WbsError as exc:
            raise ValueError(str(exc)) from exc
        _ttl_cache.pop(("wbs-mine",), None)
        return {"ok": True, "due": due, "staged": r, "note": "WBS の終了予定に反映待ちで記録しました（Excel へは WBS タブの転記で）"}
    raise ValueError(f"この出典の期日は変えられません: {source}")


PLANNER_DUE_FILE = WORKSPACE_DIR / "tasks" / "planner_due.json"


def _load_planner_due() -> dict:
    try:
        return json.loads(PLANNER_DUE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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


# ---------- decks (Tensaku) + agenda ----------

def deck_job(kind: str, meeting: str, path: str = "") -> dict:
    """Prepare extracts and start Tensaku (learn = style guide + carry-over proposals, review = one deck)."""
    from . import decks

    if _is_running("tensaku"):
        raise ValueError("Tensaku は実行中です。終わってからもう一度どうぞ")
    m = decks.meeting(meeting)
    slug = decks.slug(meeting)
    if kind == "learn":
        files = decks.extract(meeting)
        extra = (f"## 学習モード（定例: {meeting}）\n- 過去の資料の抜き出し（新しい順）: " + ", ".join(f"`{f}`" for f in files)
                 + f"\n- スタイルガイド: `knowledge/decks/{slug}-style.md` を作成・更新する\n"
                 f"- 持ち越しの候補は `workspace/agenda/suggestions/` に書く（定例名は「{meeting}」）")
    elif kind == "review":
        found = decks.list_decks(meeting)
        if not path and not found:
            raise ValueError("この定例のフォルダに資料がありません")
        target = path or str(found[0])
        review = decks.extract_file(target)
        refs = decks.extract(meeting, 3)
        extra = (f"## 添削モード（定例: {meeting}）\n- 添削する資料: `{review}`（元ファイル: {target}）\n"
                 f"- スタイルガイド: `knowledge/decks/{slug}-style.md`（無ければ過去の資料から直接判断）\n"
                 "- 比較用の過去の資料: " + ", ".join(f"`{f}`" for f in refs if f != review)
                 + "\n- 結果は `workspace/decks/reviews/` に書く")
    else:
        raise ValueError("kind は learn / review")
    with _spawn_lock:
        _spawned["tensaku"] = spawn_agent("tensaku", force=True, extra_args=["--extra", extra])
    return {"started": kind, "meeting": m["name"]}


# ---------- morning brief (Asa) ----------
BRIEF_DIR = WORKSPACE_DIR / "brief"


def brief_material() -> str:
    """Everything Asa needs, prepared by Nakama (the agent cannot reach the synced folders itself)."""
    from . import agenda

    base = assist_prompt("overall", {})["prompt"]
    carry = [f"- {i['meeting']}: {i['topic']}（{i['status']}・次: {i.get('next_action') or '-'}）"
             for i in agenda.load() if i["status"] != "closed"][:30]
    nl = chr(10)
    today = datetime.now(ZoneInfo(load_fleet().timezone)).date().isoformat()
    return (f"## Asa への材料（Nakama が作成）{nl}{base}{nl}{nl}## 定例の持ち越し課題{nl}"
            + (nl.join(carry) or "- なし") + f"{nl}{nl}出力: `workspace/brief/{today}.md`")


def latest_brief() -> dict:
    files = sorted(BRIEF_DIR.glob("*.md"), reverse=True)
    if not files:
        return {"date": "", "content": ""}
    return {"date": files[0].stem, "content": files[0].read_text(encoding="utf-8")[:20000]}


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
ROUTES = ("task", "attendance", "knowledge", "reminder", "keep")
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
                          ("route", "summary", "text", "who", "name", "date", "kind", "hours", "request", "link")})
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
        elif route == "reminder":
            from . import reminders
            result["path"] = "reminder " + reminders.add(item["date"], item["summary"], item.get("link", ""), f"メモ {stem} より")["id"]
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


def _prompt_section(text: str) -> str:
    """The part of a prompt note under '## プロンプト' (up to the next '## ' heading); whole text if absent."""
    m = re.search(r"^##\s*プロンプト\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    body = m.group(1) if m else text
    return body.strip().strip("＋+").strip()


def _meeting_prompt_notices(add, now) -> None:
    """q127: around each recurring meeting, remind to take AI minutes and show the prompt note (copyable)."""
    hm = now.strftime("%H:%M")
    for mp in load_fleet().meeting_prompts:
        if not isinstance(mp, dict) or not (str(mp.get("from", "")) <= hm < str(mp.get("until", ""))):
            continue
        name, note = str(mp.get("name") or "定例"), Path(str(mp.get("note") or ""))
        try:
            prompt = _prompt_section(note.read_text(encoding="utf-8"))
        except OSError as exc:
            prompt = f"(プロンプトのノートを読めません: {exc})"
        link = f"obsidian://open?path={url_quote(str(note))}" if mp.get("obsidian", True) and note.name else ""
        add("meeting", f"{name}の AI 議事メモを取りましょう 📝", key=f"meeting-{name}-{now.date().isoformat()}",
            when=now.isoformat(timespec="minutes"), group="リマインド", link=link,
            detail=f"会議が終わったら Copilot にこのプロンプトを貼って議事メモを作り、OneNote に保存（起票の入力になります）。元ノート: {note}",
            draft=prompt[:4000])


def _more_notices(add, today, samples: bool) -> None:
    """Things that need you but no agent reports: overdue / due-today tasks, drafts waiting for review,
    memo routing suggestions not yet applied, agent runs that failed today, WBS problems.
    Counts are part of the key, so an OK'd summary comes back when something new is added."""
    iso = today.isoformat()
    try:
        tasks = _ttl(("notif-tasks", iso), 300, lambda: unified_tasks())["tasks"]
    except Exception:  # noqa: BLE001 - the feed must never fail because one source did
        tasks = []
    live = [t for t in tasks if t["status"] not in ("done", "skip") and t.get("due")]
    for kind, rows, label in (("overdue", [t for t in live if t["due"] < iso], "期限切れのタスク"),
                              ("duetoday", [t for t in live if t["due"] == iso], "今日が期日のタスク")):
        if rows:
            add(kind, f"{label} {len(rows)} 件", key=f"{kind}-{iso}-{len(rows)}", when=iso, tab="tasks", group="タスク",
                extra=[{"title": t["title"], "detail": t["due"], "link": t.get("link") or ""} for t in rows[:15]])
    drafts = _list_kind("drafts", samples)
    if drafts:
        add("draft", f"レビュー待ちの下書き {len(drafts)} 件", key=f"drafts-{len(drafts)}-{max(d['mtime'] for d in drafts):.0f}",
            when=fmt_mtime(max(d["mtime"] for d in drafts)), tab="drafts", group="下書き・メモ",
            extra=[{"title": d["title"] or d["name"], "detail": fmt_mtime(d["mtime"]), "link": ""} for d in drafts[:10]])
    applied, memos = _load_applied(), []
    for f in sorted(SUGGEST_DIR.glob("*.json"), reverse=True)[:30] if SUGGEST_DIR.exists() else []:
        sug = _cached_json(f)
        n = len([x for x in (sug or {}).get("items") or [] if isinstance(x, dict)]) if isinstance(sug, dict) else 0
        left = n - len(applied.get(f.stem) or {})
        if left > 0 and (MEMOS_DIR / f"{f.stem}.md").exists():
            text = (MEMOS_DIR / f"{f.stem}.md").read_text(encoding="utf-8")
            memos.append({"title": _title(text, _front_matter(text))[:60], "detail": f"{f.stem}・案 {left} 件", "link": ""})
    if memos:  # one row: memo suggestions are low-urgency and can be many
        add("memo", f"メモの振り分け案が未処理 {len(memos)} 件", key=f"memo-{len(memos)}-{memos[0]['detail']}", when=iso, tab="memos",
            group="下書き・メモ", extra=memos, detail="Kikitori の提案です。メモタブで、実行するものだけボタンを押してください（不要なら「不要」）。")
    for agent, run in _last_runs().items():
        if run.get("error") and run.get("ts", "").startswith(iso):
            add("agent_error", f"{agent} の実行が失敗（{run['ts'][11:16]}）", key=f"err-{agent}-{run['ts']}", when=run["ts"],
                tab="logs", group="エージェント", detail="ログタブで原因を確認し、必要なら「今すぐ実行」でやり直してください。")
    try:
        from . import wbs

        ov = _ttl(("notif-wbs", iso), 300, lambda: wbs.overview(today))
        for f in ov["files"]:
            if f["deadline"] and f["finish"] and f["finish"] > f["deadline"]:
                add("wbs", f"WBS {f['name']}: 期限 {f['deadline']} を超える見込み（{f['finish']}）", key=f"wbs-over-{f['id']}-{f['finish']}",
                    when=iso, tab="wbs", group="WBS", detail=f"残り {f['remaining_effort']} 人日・余裕 {f['slack']} 日（先行関係からの推定）")
        bad = [p for p in ov["pending"] if p.get("error")]
        if bad:
            add("wbs", f"WBS の Excel 転記に失敗 {len(bad)} 件", key=f"wbs-err-{len(bad)}-{bad[-1]['at']}", when=bad[-1]["at"], tab="wbs",
                group="WBS", extra=[{"title": f"{p['file_name']} {p['no']}", "detail": p["error"], "link": ""} for p in bad[:10]])
    except Exception:  # noqa: BLE001
        pass
    try:  # q116: ask for this month's budget before the ops script is run
        from . import scripts

        for x in scripts.listing():
            missing = [e for e in x["envs"] if e not in x["plans"]]
            if x["routine"] and missing:
                add("script", f"{x['name']}: {x['month']} の計画値（予算）が未入力（{'・'.join(missing)}）", key=f"plan-{x['id']}-{x['month']}",
                    when=iso, tab="routine", group="おしらせ",
                    detail="ルーティンの該当ルーチンを開き、🛠 カードの計画値に入れて保存すると、文案の $XXXXX と内着地/超過が埋まります。")
    except Exception:  # noqa: BLE001
        pass


def recent_handled(limit: int = 12) -> dict:
    """What left the escalation feed recently and where it went (OK'd notices + finished items)."""
    rows = [r for r in completed_index(400)["rows"] if r["source"] in ("notice", "reminder", "action_items", "memos")]
    return {"rows": rows[:limit]}


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
            tab: str = "", path: str = "", group: str = "", extra: list | None = None, draft: str = "") -> None:
        nid = _nid(kind, key, title)
        items.append({"id": nid, "kind": kind, "title": title, "when": when, "detail": detail, "link": link,
                      "tab": tab, "path": path, "group": group, "extra": extra or [], "acked": nid in acks, "draft": draft})

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
            add("escalated", str(d.get("message") or "ソース不足で起票を見送り"), key=f.name, when=when, group=group,
                detail="議事メモが届かず起票できませんでした。メモの書き出し（OneNote → HTML）と OneDrive の同期を確認し、"
                       "届いたら起票タブで再実行。急ぐなら右のチャット（✏️ 起票操作）に議事メモを貼れば起票できます。")
        for x in lst("needs_attention"):
            add("attention", str(x.get("title", "")), key=f.name, when=when, group=group,
                detail=str(x.get("reason") or x.get("note") or "Kihyo が判断を保留した案件です。レポートで背景を確認してください。"))
        if not KIHYO_DETAIL_IN_FEED:
            continue
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
            detail="定時を過ぎる見込みなら申請を。勤怠 > 残業 で開始・終了・理由を記録すると申請文になります。残業しないなら OK。",
            draft=overtime.render({"date": now.date().isoformat(), "start": "18:00", "end": "20:00", "hours": 2.0,
                                   "reason": "〇〇の対応（期限 〇/〇）"}))
    from . import bizdays, devreq, reminders

    today = now.date()
    for r in reminders.due(today):
        when = "今日が期限" if r["date"] == today.isoformat() else ("期限切れ " + r["date"] if r["date"] < today.isoformat() else "期限 " + r["date"])
        add("reminder", f"{r['title']}（{when}）", key=f"reminder-{r['id']}-{r['date']}", when=r["date"], group="リマインド",
            detail=r.get("note") or "", link=r.get("link") or "", extra=[{"title": "rid", "detail": r["id"], "link": ""}],
            draft=reminders.draft_message(r, today))
    for q in devreq.load():
        if q.get("status") == "consult":
            add("devreq", f"{q['id']} {q['text']}", key=f"devreq-{q['id']}-{q.get('updated', '')}", when=q.get("updated", ""),
                tab="devreq", group="あなたの確認が必要な要望", detail=(q.get("note") or "")[:400])
    for a in _list_kind("action_items", samples):
        if a["meta"].get("status") == "open":
            add("action", a["title"] or a["name"], key=a["path"], when=fmt_mtime(a["mtime"]), tab="action_items",
                path=a["path"], group="要対応", detail=a["meta"].get("source", ""))

    _more_notices(add, today, samples)
    # q103: a small nudge before lunch on business days; open requests are applied together during the break
    if bizdays.is_business_day(today) and "11:30" <= now.strftime("%H:%M") < "12:30":
        n_open = sum(1 for q in devreq.load() if q.get("status") in ("open", "partial"))
        add("lunch", f"そろそろお昼ですか？ 🍙 システム改善要望 {n_open} 件", key=f"lunch-{today.isoformat()}", when=now.isoformat(timespec="minutes"),
            tab="devreq", group="おしらせ",
            detail="お昼に行くときに Claude Code へ「昼休み行く」と伝えると、未対応の要望をまとめて反映しておきます。")

    hm = now.strftime("%H:%M")
    if bizdays.is_business_day(today):
        _meeting_prompt_notices(add, now)
        live_tasks = [t for t in _ttl(("notif-tasks", today.isoformat()), 300, lambda: unified_tasks())["tasks"]
                      if t["status"] not in ("done", "skip") and t.get("due") and t["due"] <= today.isoformat()] if "15:00" <= hm else []
        if "15:00" <= hm < "15:30":
            add("break", "ひと息つきませんか？ ☕", key=f"break-{today.isoformat()}", when=now.isoformat(timespec="minutes"), group="おしらせ",
                detail=f"今日までのタスク残り {len(live_tasks)} 件。休憩に行くとき Claude Code に「休憩」と伝えれば、その間に要望を反映します。")
        if "18:30" <= hm < "20:30":
            add("evening", f"そろそろ上がりますか？ 🌙 今日までのタスク残り {len(live_tasks)} 件", key=f"evening-{today.isoformat()}",
                when=now.isoformat(timespec="minutes"), tab="tasks", group="おしらせ",
                detail="残りは明日に回すなら期日を変更、終わったものは ✓。残業するなら残業申請を。")
    if hm >= "21:30" or hm < "05:00":
        add("night", "もう夜です 🌙 今日はこのへんで", key=f"night-{today.isoformat()}", when=now.isoformat(timespec="minutes"), group="おしらせ",
            detail="続きは明日のブリーフ（Asa・平日 8:30）に載ります。")
    # q122: time-sensitive things first, newest first inside a group (the feed renders groups in this order)
    prio = {k: 0 for k in ("overtime", "reminder", "overdue", "duetoday", "escalated", "agent_error")}
    prio |= {k: 1 for k in ("lunch", "break", "evening", "night", "action", "script", "attention")}
    prio |= {k: 2 for k in ("devreq", "wbs")}
    gmin: dict[str, int] = {}
    for i in items:
        gmin[i["group"]] = min(gmin.get(i["group"], 9), prio.get(i["kind"], 3))
    items.sort(key=lambda i: str(i.get("when") or ""), reverse=True)
    items.sort(key=lambda i: (gmin[i["group"]], i["group"]))
    for i in items:
        i["hot"] = prio.get(i["kind"], 3) == 0

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
    week = (today - timedelta(days=6)).isoformat()
    handled = [str(a.get("acked_at", ""))[:10] for a in acks.values() if isinstance(a, dict)]
    return {"items": shown, "unacked": unacked, "total": len(items),
            "handled_today": sum(1 for d in handled if d == today.isoformat()), "handled_week": sum(1 for d in handled if d >= week),
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
- **① 実際の利用（API 換算）**: このダッシュボード経由のすべての Claude 利用（エージェントの定期・手動実行、右のチャット、🤖 相談・アシスト）で
  Claude CLI が報告する推定額の合計。従量課金の API で動かした場合の目安。Claude Code を直接使った分（このリポジトリの開発など）は含まない。
- **ライセンス**: `config/fleet.yaml` の `cost_plans` で定義（設定タブで編集）。月額 =
  `基本料 + 追加利用 − 会社負担`（会社負担は基本料＋追加利用を上限）。
  - `included`: 基本料に含まれる利用額（API 換算、ドル）。空なら「基本料内で賄われる」とみなし追加利用 0。
  - `overage_rate`: included を超えた API 換算 1 ドルあたりの追加料金。
- 社内ダッシュボードの数値（ほかの利用も含む）とは一致しません。このダッシュボード分の比較用です。
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

# Which API paths each tab needs, for a guest login (prefix match). Anything else is refused.
GUEST_TAB_APIS: dict[str, tuple[str, ...]] = {
    "credit": ("/api/credit/",),
    "rongo": ("/api/rongo",),
    "devreq": ("/api/devreq",),  # the family's requests: read + add (see GUEST_DENY)
}
GUEST_DENY = {"/api/devreq/update"}  # changing a request's status or notes stays owner-only


UI_COLORS = ("blue", "pink", "green", "orange", "purple", "gray")

# ---------- login sessions (cookie) ----------
SESSION_COOKIE = "nakama_session"
SESSION_DAYS = 90
SESSION_FILE = LOGS_DIR / "ui-sessions.json"  # hashes only; logs/ is git-ignored
_session_lock = threading.Lock()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sessions_load() -> dict:
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sessions_save(data: dict) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(SESSION_FILE)


def session_new(role: str, password: str, name: str = "") -> str:
    """A random token for the cookie; only its hash is stored, with the role, the user name typed at login
    and (a short hash of) the password it was opened with."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _session_lock:
        data = {k: v for k, v in _sessions_load().items() if v.get("exp", 0) > now}
        data[_sha(token)] = {"role": role, "name": _clean_name(name), "pw": _sha(password)[:16],
                             "at": now, "exp": now + SESSION_DAYS * 86400}
        _sessions_save(data)
    return token


def _latest_session_name(role: str) -> str:
    """The user name most recently typed at login for this role (shown for someone who is not the viewer)."""
    now = time.time()
    entries = [v for v in _sessions_load().values() if v.get("role") == role and v.get("name") and v.get("exp", 0) > now]
    return max(entries, key=lambda v: v.get("at", 0))["name"] if entries else ""


def session_get(token: str) -> dict | None:
    if not token:
        return None
    entry = _sessions_load().get(_sha(token))
    return entry if entry and entry.get("exp", 0) > time.time() else None


def session_drop(token: str) -> None:
    if not token:
        return
    with _session_lock:
        data = _sessions_load()
        if data.pop(_sha(token), None) is not None:
            _sessions_save(data)


LOGIN_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ログイン</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --text:#1d2330; --muted:#667085; --line:#e3e6eb; --accent:#2f6fde; --bad:#c4352b; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151b; --panel:#1a1e26; --text:#e6e9ef; --muted:#98a1b3; --line:#2a303c; --accent:#6b9cff; --bad:#ff6b60; } }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:var(--bg); color:var(--text);
       font:15px/1.6 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Yu Gothic UI",sans-serif; padding:16px; }
form { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:28px 24px; width:min(360px,100%); }
h1 { font-size:18px; margin:0 0 18px; }
label { display:block; font-size:13px; color:var(--muted); margin:12px 0 4px; }
input { width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:var(--panel); color:var(--text); font:inherit; font-size:16px; }
button { margin-top:18px; width:100%; padding:11px; border:0; border-radius:999px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
.err { color:var(--bad); font-size:13px; margin:0 0 6px; } .note { color:var(--muted); font-size:12px; margin-top:12px; }
</style></head><body>
<form method="post" action="/login">
  <h1>ログイン</h1>__ERROR__
  <label for="u">ユーザー名（画面に表示されます）</label><input id="u" name="username" autocomplete="username" value="" required>
  <label for="p">パスワード</label><input id="p" name="password" type="password" autocomplete="current-password" required autofocus>
  <button type="submit">ログイン</button>
  <div class="note">この端末では 90 日間ログインしたままになります。右上の名前からログアウトできます。</div>
</form></body></html>"""


def _clean_name(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:40]


def ui_user(role: str, name: str = "") -> dict:
    """Who a login is, for display. Name: the user name typed at login (this session, else the latest one
    for that login), else fleet.yaml `ui_users.<owner|guest>.name`, else a plain default. Colour from ui_users."""
    fleet = load_fleet()
    cfg = (getattr(fleet, "ui_users", None) or {}).get(role) or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    default_name = "オーナー" if role == "owner" else "ゲスト"
    color = str(cfg.get("color") or ("blue" if role == "owner" else "pink"))
    return {"role": role, "name": _clean_name(name) or _latest_session_name(role) or _clean_name(cfg.get("name")) or default_name,
            "color": color if color in UI_COLORS else "gray"}


def guest_overview(tabs: list[str]) -> dict:
    """What the page needs to draw itself for a guest: no names, agents, costs or counts."""
    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    return {"now": datetime.now(tz).isoformat(timespec="minutes"), "timezone": fleet.timezone, "owner": "",
            "auth": "", "has_samples": False, "budget": 0, "spent": 0, "agents": [], "counts": {},
            "queues": {}, "has_ticketing": False, "features": {t: True for t in tabs}, "guest": True}


class _Handler(BaseHTTPRequestHandler):
    index_html: str = ""
    token: str = ""
    allowed_hosts: set[str] = set()
    password: str = ""
    guest_password: str = ""
    guest_tabs: tuple[str, ...] = ("credit",)

    def log_message(self, fmt, *args):  # route access logs to our logger at debug level
        log.debug("ui %s - %s", self.address_string(), fmt % args)

    # --- guards ---
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in self.allowed_hosts

    def _role_for_password(self, pw: str) -> str | None:
        if self.password and hmac.compare_digest(pw.encode(), self.password.encode()):
            return "owner"
        if self.password and self.guest_password and hmac.compare_digest(pw.encode(), self.guest_password.encode()):
            return "guest"
        return None

    def _cookie_token(self) -> str:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _session(self) -> dict | None:
        """The valid session of this request: only while the password it was opened with is still current."""
        entry = session_get(self._cookie_token())
        if not entry:
            return None
        current = {"owner": self.password, "guest": self.guest_password}.get(entry.get("role"), "")
        return entry if current and entry.get("pw") == _sha(current)[:16] else None

    def _role(self) -> str | None:
        """"owner", "guest" or None (not logged in), from the session cookie."""
        if not self.password:
            return "owner"  # no login configured (localhost use)
        entry = self._session()
        return entry["role"] if entry else None

    def _basic_ok(self) -> bool:
        return self._role() is not None

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, error: bool) -> None:
        msg = '<p class="err">パスワードが違います</p>' if error else ""
        self._send(200, LOGIN_HTML.replace("__ERROR__", msg).encode("utf-8"), "text/html")

    def _form_body(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 16 * 1024)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                data = json.loads(raw or "{}")
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _do_login(self) -> None:
        if not self._host_ok():
            self._send(403, {"error": "host not allowed (set NAKAMA_UI_ALLOW_HOSTS)"})
            return
        if not self.password:
            self._redirect("/")
            return
        form = self._form_body()
        pw = str(form.get("password", ""))
        role = self._role_for_password(pw)
        if role is None:
            time.sleep(1)  # slow down guessing
            self._redirect("/login?e=1")
            return
        token = session_new(role, pw, str(form.get("username", "")))
        self._redirect("/", f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax")

    def _do_logout(self) -> None:
        session_drop(self._cookie_token())
        self._redirect("/login", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def _me(self) -> dict:
        entry = self._session() if self.password else None
        return ui_user(self._role() or "owner", (entry or {}).get("name", ""))

    def _approvers(self) -> list[dict]:
        """Who must approve a credit month: the owner, plus the guest when a guest login exists."""
        return [ui_user("owner")] + ([ui_user("guest")] if self.password and self.guest_password else [])

    def _readers(self) -> list[dict]:
        """Whose reading the 論語 tab shows side by side: the owner, plus the guest when a guest login exists."""
        return self._approvers()

    def _guest_allowed(self, path: str) -> bool:
        if path == "/api/overview":
            return True
        if path in GUEST_DENY:
            return False
        return any(path.startswith(prefix) for t in self.guest_tabs for prefix in GUEST_TAB_APIS.get(t, ()))

    def _guard(self, api: bool, path: str = "") -> bool:
        if not self._host_ok():
            self._send(403, {"error": "host not allowed (set NAKAMA_UI_ALLOW_HOSTS)"})
            return False
        role = self._role()
        if role is None:
            if api:
                self._send(401, {"error": "login required"})
            else:
                self._redirect("/login")
            return False
        if api and not hmac.compare_digest(self.headers.get("X-Nakama-Token", ""), self.token):
            # a tab left open across a restart still polls (with the old token): it is open, so count it
            _mark_ui_seen()
            self._send(403, {"error": "bad token (reload the page)"})
            return False
        if api and role == "guest" and not self._guest_allowed(path):
            self._send(403, {"error": "forbidden for this login"})
            return False
        if api:
            _mark_ui_seen()
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
        except (ValueError, KeyError, chat.ChatError, NoteError, VaultError, WbsError, DeckError, ScriptError) as exc:
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
        if url.path == "/login":
            if not self._host_ok():
                self._send(403, {"error": "host not allowed (set NAKAMA_UI_ALLOW_HOSTS)"})
            elif not self.password or self._role():
                self._redirect("/")
            else:
                self._login_page(q.get("e") == "1")
            return
        if url.path in ("/", "/index.html"):
            if not self._guard(api=False):
                return
            guest = ",".join(self.guest_tabs) if self._role() == "guest" else ""
            html = self.index_html.replace("__NAKAMA_TOKEN__", self.token).replace("__NAKAMA_GUEST_TABS__", guest)
            self._send(200, html.encode("utf-8"), "text/html")
            return
        if not url.path.startswith("/api/"):
            self._send(404, {"error": "not found"})
            return
        if not self._guard(api=True, path=url.path):
            return
        if url.path == "/api/overview" and self._role() == "guest":
            self._dispatch(lambda: guest_overview(list(self.guest_tabs)) | {"me": self._me()})
            return

        def list_kind():
            if q.get("kind") not in KINDS:
                raise ValueError(f"bad kind: {q.get('kind')}")
            return _list_kind(q["kind"], q.get("samples", "1") != "0")

        if url.path == "/api/overview":
            self._dispatch(lambda: overview(q.get("samples", "1") != "0") | {"me": self._me()})
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
            self._dispatch(lambda: {"content": GUIDE_MD.read_text(encoding="utf-8"), "references": reference_pdfs()})
        elif url.path == "/api/reference":
            # q128: raw PDF bytes for the embedded preview (fetched with the token, shown via a blob URL)
            try:
                path = _reference_path(q.get("name", ""))
            except (FileNotFoundError, PermissionError) as exc:
                self._send(404, {"error": f"not found: {exc}"})
                return
            self._send(200, path.read_bytes(), "application/pdf")
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
            guest = self._role() == "guest"
            self._dispatch(lambda: {"requests": devreq.for_family() if guest else devreq.load(),
                                    "statuses": devreq.STATUSES, "guest": guest})
        elif url.path == "/api/cron/help":
            self._dispatch(lambda: {"help": cronja.HELP})
        elif url.path == "/api/overtime":
            from . import overtime
            self._dispatch(lambda: overtime.month_summary(_month_or_current(q.get("month", ""))))
        elif url.path == "/api/memo/history":
            self._dispatch(memo_history)
        elif url.path == "/api/rongo":
            from . import rongo
            who = self._role() or "owner"
            self._dispatch(lambda: rongo.status(q.get("id", ""), user=who, people=self._readers()))
        elif url.path == "/api/credit/months":
            from . import credit
            self._dispatch(credit.months)
        elif url.path == "/api/credit/month":
            from . import credit
            self._dispatch(lambda: credit.month_view(q.get("month", ""), self._approvers()))
        elif url.path == "/api/credit/export":
            from . import credit
            self._dispatch(lambda: credit.export_csv(q.get("month", "")))
        elif url.path == "/api/routines/history":
            from . import routines
            self._dispatch(lambda: {"history": routines.history(q.get("id", ""), 6, _month_or_current(q.get("month", "")))})
        elif url.path == "/api/wbs":
            from . import wbs
            self._dispatch(lambda: {"files": _ttl(("wbs-index",), 60, lambda: wbs.index(date.today()))})
        elif url.path == "/api/wbs/file":
            from . import wbs
            self._dispatch(lambda: wbs.view(wbs.by_id(q.get("id", ""))) | {"aliases": wbs.owner_aliases(), "sync_minutes": wbs.sync_minutes()})
        elif url.path == "/api/wbs/overview":
            from . import wbs
            self._dispatch(lambda: wbs.overview(date.today()))
        elif url.path == "/api/decks":
            from . import decks
            self._dispatch(lambda: {"meetings": decks.overview(), "reviews": decks.reviews(), "running": _is_running("tensaku")})
        elif url.path == "/api/agenda":
            from . import agenda, decks
            self._dispatch(lambda: {"items": agenda.load(), "suggestions": agenda.suggestions(),
                                    "meetings": [m["name"] for m in decks.meetings()]})
        elif url.path == "/api/brief":
            self._dispatch(latest_brief)
        elif url.path == "/api/reminders":
            from . import reminders
            self._dispatch(lambda: {"reminders": reminders.load()})
        elif url.path == "/api/projects":
            from . import projects
            self._dispatch(lambda: {"projects": projects.load()})
        elif url.path == "/api/vault":
            from . import vault
            self._dispatch(lambda: vault.configure() | {"root_ok": vault.root() is not None})
        elif url.path == "/api/vault/list":
            from . import vault
            self._dispatch(lambda: vault.listing(q.get("rel", ""), self.headers.get("X-Vault-Token", "")))
        elif url.path == "/api/vault/file":
            from . import vault
            self._dispatch(lambda: vault.read(q.get("rel", ""), self.headers.get("X-Vault-Token", "")))
        elif url.path == "/api/notifications/handled":
            self._dispatch(recent_handled)
        elif url.path == "/api/scripts":
            from . import scripts
            self._dispatch(lambda: {"scripts": scripts.listing()})
        elif url.path == "/api/scripts/result":
            from . import scripts
            self._dispatch(lambda: scripts.result(q.get("id", "")))
        elif url.path == "/api/completed":
            self._dispatch(completed_index)
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
        if url.path == "/login":
            self._do_login()
            return
        if url.path == "/logout":
            self._do_logout()
            return
        if not self._guard(api=True, path=url.path):
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
        elif url.path == "/api/scripts/run":
            from . import scripts
            self._dispatch(lambda: scripts.run(str(body.get("id", ""))))
        elif url.path == "/api/notifications/react":
            self._dispatch(lambda: add_reaction(body))
        elif url.path == "/api/scripts/plans":
            from . import scripts
            self._dispatch(lambda: scripts.save_plans(str(body.get("id", "")), body.get("values") or {}))
        elif url.path == "/api/chats/actions":
            self._dispatch(lambda: chat.set_actions(str(body.get("chat", "")), body.get("on") is True))
        elif url.path == "/api/chats/send":
            self._dispatch(lambda: send_chat(str(body.get("chat", "")), str(body.get("text", ""))))
        elif url.path == "/api/tasks/star":
            self._dispatch(lambda: star_task(body))
        elif url.path == "/api/tasks/done":
            self._dispatch(lambda: complete_task(body))
        elif url.path == "/api/devreq/add":
            from . import devreq
            me = self._me()
            guest = me["role"] == "guest"
            self._dispatch(lambda: devreq.add(str(body.get("text", "")), note="" if guest else str(body.get("note", "")),
                                              by=me["role"], by_name=me["name"], family=bool(body.get("family"))))
        elif url.path == "/api/devreq/update":
            from . import devreq
            self._dispatch(lambda: devreq.update(str(body.get("id", "")), body.get("status"), body.get("note"),
                                                 body.get("change"), body.get("usage"), body.get("instruction"),
                                                 None if body.get("family") is None else bool(body.get("family"))))
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
            self._dispatch(lambda: notify_all(load_fleet().notify, "ダッシュボードからのテスト通知です。", "テスト通知"))
        elif url.path == "/api/tasks/due":
            self._dispatch(lambda: set_task_due(body))
        elif url.path == "/api/wbs/update":
            from . import wbs
            def wbs_update():
                r = wbs.stage(str(body.get("id", "")), str(body.get("sheet", "")), int(body.get("row", 0)),
                              str(body.get("field", "")), str(body.get("value", "")), str(body.get("reason", "")),
                              expected_no=str(body.get("no", "")))
                for k in (("wbs-index",), ("wbs-mine",)):  # the task list must show the change right away
                    _ttl_cache.pop(k, None)
                return r
            self._dispatch(wbs_update)
        elif url.path in ("/api/wbs/flush", "/api/wbs/discard"):
            from . import wbs
            def wbs_flush():
                r = wbs.flush(str(body.get("id", ""))) if url.path.endswith("flush") else wbs.discard(str(body.get("pid", "")))
                for k in (("wbs-index",), ("wbs-mine",)):
                    _ttl_cache.pop(k, None)
                return r
            self._dispatch(wbs_flush)
        elif url.path == "/api/decks/job":
            self._dispatch(lambda: deck_job(str(body.get("kind", "")), str(body.get("meeting", "")), str(body.get("path", ""))))
        elif url.path == "/api/decks/template":
            from . import decks
            self._dispatch(lambda: {"path": decks.save_template(str(body.get("meeting", "")))})
        elif url.path == "/api/agenda/add":
            from . import agenda
            self._dispatch(lambda: agenda.add(str(body.get("meeting", "")), str(body.get("topic", "")), str(body.get("owner", "")),
                                              str(body.get("next_action", "")), str(body.get("due", "")), str(body.get("status") or "carry")))
        elif url.path == "/api/agenda/update":
            from . import agenda
            self._dispatch(lambda: agenda.update(str(body.get("id", "")), body.get("status"), str(body.get("note", "")),
                                                 body.get("next_action"), body.get("owner"), body.get("due")))
        elif url.path == "/api/agenda/suggestion":
            from . import agenda
            self._dispatch(lambda: {"item": agenda.handle_suggestion(str(body.get("file", "")), int(body.get("index", -1)), bool(body.get("accept")))})
        elif url.path == "/api/brief/run":
            def run_brief():
                if _is_running("asa"):
                    raise ValueError("Asa は実行中です")
                with _spawn_lock:
                    _spawned["asa"] = spawn_agent("asa", force=True, extra_args=["--extra", brief_material()])
                return {"started": True}
            self._dispatch(run_brief)
        elif url.path == "/api/reminders/add":
            from . import reminders
            self._dispatch(lambda: reminders.add(str(body.get("date", "")), str(body.get("title", "")), str(body.get("link", "")),
                                                 str(body.get("note", "")), int(body.get("lead_days", 1))))
        elif url.path == "/api/reminders/done":
            from . import reminders
            self._dispatch(lambda: reminders.set_done(str(body.get("id", "")), body.get("done", True) is not False))
        elif url.path == "/api/projects/save":
            from . import projects
            self._dispatch(lambda: {"projects": projects.upsert(str(body.get("name", "")),
                                                               [str(f) for f in body.get("folders") or []], str(body.get("note", "")))})
        elif url.path == "/api/projects/delete":
            from . import projects
            self._dispatch(lambda: {"projects": projects.delete(str(body.get("name", "")))})
        elif url.path == "/api/vault/config":
            from . import vault
            claude = body.get("claude")
            self._dispatch(lambda: vault.configure(body.get("root"), body.get("folder"), body.get("mode"),
                                                   None if claude is None else bool(claude),
                                                   token=self.headers.get("X-Vault-Token", "")))
        elif url.path == "/api/vault/password":
            from . import vault
            self._dispatch(lambda: (vault.set_password(str(body.get("new", "")), str(body.get("current", ""))) or {"ok": True}))
        elif url.path == "/api/vault/unlock":
            from . import vault
            self._dispatch(lambda: {"token": vault.unlock(str(body.get("password", "")))})
        elif url.path == "/api/vault/save":
            from . import vault
            self._dispatch(lambda: vault.write(str(body.get("rel", "")), str(body.get("text", "")), float(body.get("mtime") or 0),
                                               self.headers.get("X-Vault-Token", "")))
        elif url.path == "/api/note/save":
            from . import todo_note
            self._dispatch(lambda: todo_note.save_raw(str(body.get("text", "")), float(body.get("mtime") or 0)))
        elif url.path == "/api/note/addchild":
            self._dispatch(lambda: add_note_child(body))
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
        elif url.path == "/api/rongo/read":
            from . import rongo
            who = self._role() or "owner"
            self._dispatch(lambda: rongo.mark_read(str(body.get("id", "")), bool(body.get("read", True)), user=who)
                           | {"readers": rongo.status(user=who, people=self._readers())["readers"]})
        elif url.path == "/api/rongo/fav":
            from . import rongo
            who = self._role() or "owner"
            self._dispatch(lambda: rongo.set_fav(str(body.get("id", "")), bool(body.get("fav", True)), user=who))
        elif url.path == "/api/rongo/generate":
            from . import rongo
            self._dispatch(lambda: rongo.start_generation(str(body.get("id", ""))))
        elif url.path == "/api/credit/import":
            from . import credit
            self._dispatch(lambda: credit.import_csv(str(body.get("filename", "")), str(body.get("data", "")),
                                                     str(body.get("month", "") or "")))
        elif url.path == "/api/credit/update":
            from . import credit
            self._dispatch(lambda: credit.update_rows(str(body.get("month", "")), list(body.get("ids") or []),
                                                      dict(body.get("change") or {}), self._approvers()))
        elif url.path == "/api/credit/meta":
            from . import credit
            meta = {k: body[k] for k in ("balance", "billed") if k in body}
            self._dispatch(lambda: credit.set_meta(str(body.get("month", "")), **meta, required=self._approvers()))
        elif url.path == "/api/credit/close":
            from . import credit
            self._dispatch(lambda: credit.close(str(body.get("month", "")), bool(body.get("closed")),
                                                required=self._approvers()))
        elif url.path == "/api/credit/approve":
            from . import credit
            self._dispatch(lambda: credit.approve(str(body.get("month", "")), self._me(), bool(body.get("approved", True)),
                                                  required=self._approvers()))
        elif url.path == "/api/credit/delete":
            from . import credit
            self._dispatch(lambda: credit.delete_month(str(body.get("month", ""))))
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
        "guest_password": os.environ.get("NAKAMA_UI_GUEST_PASSWORD", ""),
        "guest_tabs": tuple(t for t in (x.strip() for x in os.environ.get("NAKAMA_UI_GUEST_TABS", "credit").split(","))
                            if t in GUEST_TAB_APIS) or ("credit",),
    })
    if host not in ("127.0.0.1", "localhost", "::1") and not handler.password:
        log.warning("UI listens on %s without NAKAMA_UI_PASSWORD; anyone allowed by Host can open it", host)
    if handler.guest_password and not handler.password:
        log.warning("NAKAMA_UI_GUEST_PASSWORD is ignored: set NAKAMA_UI_PASSWORD too (otherwise there is no login)")
    if handler.guest_password and handler.guest_password == handler.password:
        log.warning("NAKAMA_UI_GUEST_PASSWORD equals NAKAMA_UI_PASSWORD; that login gets full access")
    return _Server((host, port), handler)


def _warm_wbs() -> None:
    """q118: keep the WBS file discovery warm so the WBS tab opens in well under a second."""
    from . import wbs

    while True:
        try:
            wbs.discover(refresh=True)  # re-walk in the background; users always hit the cache
            wbs.index(date.today())
        except Exception:  # noqa: BLE001 - warming is best effort
            log.exception("WBS warm-up failed")
        time.sleep(wbs.DISCOVER_TTL - 60)


# An open dashboard tab polls the API, and after a restart it reconnects by itself, so
# opening the browser again would only add a duplicate tab. We remember the last API hit.
UI_SEEN_FILE = WORKSPACE_DIR / ".ui_last_seen"
UI_SEEN_WINDOW = 15 * 60   # seconds: a tab seen this recently is assumed to still be open
_ui_seen_at = 0.0


def _mark_ui_seen() -> None:
    global _ui_seen_at
    now = time.time()
    if now - _ui_seen_at < 30:  # touch the file at most every 30 s
        return
    _ui_seen_at = now
    try:
        UI_SEEN_FILE.touch()
    except OSError:
        log.debug("could not touch %s", UI_SEEN_FILE, exc_info=True)


def _tab_recently_open() -> bool:
    try:
        return time.time() - UI_SEEN_FILE.stat().st_mtime < UI_SEEN_WINDOW
    except OSError:
        return False


def serve_ui(host: str = "127.0.0.1", port: int = 8765, background: bool = False,
             open_browser: bool = False) -> ThreadingHTTPServer:
    server = make_server(host, port)  # the socket is bound here, so the browser can connect right away
    url = f"http://localhost:{port}/"
    log.info("Nakama UI: %s", url)
    threading.Thread(target=_warm_wbs, daemon=True).start()
    if open_browser and _tab_recently_open():
        log.info("a dashboard tab was active %ss ago; not opening another one", int(time.time() - UI_SEEN_FILE.stat().st_mtime))
    elif open_browser:
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
