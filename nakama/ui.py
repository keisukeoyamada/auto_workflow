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
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import KNOWLEDGE_DIR, LOGS_DIR, ROOT, WORKSPACE_DIR, ConfigError, load_fleet
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
}
# Which statuses the UI may set, per folder
EDITABLE_STATUS = {
    WORKSPACE_DIR / "action_items": {"open", "waiting", "done"},
    WORKSPACE_DIR / "requests": {"open", "done"},
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


def _list_kind(kind: str) -> list[dict]:
    folder, pattern = KINDS[kind]
    if not folder.exists():
        return []
    files = [p for p in folder.glob(pattern) if p.name != "README.md"]  # folder docs, not data
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


def overview() -> dict:
    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    today = datetime.now(tz).date().isoformat()
    spent = spend_on(today, tz)
    agents = []
    for spec in fleet.agents.values():
        last = last_success(spec.name)
        agents.append({
            "name": spec.name, "model": spec.model, "effort": spec.effort,
            "schedule": spec.schedule, "enabled": spec.enabled,
            "advisor": bool(spec.advisor),
            "last_success": last.astimezone(tz).isoformat(timespec="minutes") if last else None,
            "cost_today": round(spent.get(spec.name, 0.0), 4),
            "running": _is_running(spec.name),
        })

    def count(kind: str, status: str | None = None) -> int:
        items = _list_kind(kind)
        return sum(1 for i in items if status is None or i["meta"].get("status") == status)

    return {
        "now": datetime.now(tz).isoformat(timespec="minutes"),
        "timezone": fleet.timezone,
        "owner": fleet.owner.get("name", ""),
        "auth": _auth_label(fleet),
        "budget": float(fleet.limits.get("daily_budget_usd") or 0),
        "spent": round(sum(spent.values()), 4),
        "agents": agents,
        "counts": {
            "drafts": count("drafts"),
            "action_items_open": count("action_items", "open"),
            "requests_open": count("requests", "open"),
            "inbox_new": count("inbox", "new"),
        },
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
        cmd = [sys.executable, "-m", "nakama", "run", agent, "--force"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console popup on Windows
        _spawned[agent] = subprocess.Popen(
            cmd, cwd=ROOT, env={**os.environ, "NAKAMA_HOME": str(ROOT)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
        )
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
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _dispatch(self, fn) -> None:
        try:
            self._send(200, fn())
        except (ValueError, KeyError) as exc:
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
            html = INDEX_HTML.read_text(encoding="utf-8").replace("__NAKAMA_TOKEN__", self.token)
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
            return _list_kind(q["kind"])

        if url.path == "/api/overview":
            self._dispatch(overview)
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
        elif url.path == "/api/status":
            def update():
                set_status(str(body.get("path", "")), str(body.get("status", "")))
                return {"ok": True}
            self._dispatch(update)
        else:
            self._send(404, {"error": "not found"})


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    extra = {h.strip().lower() for h in os.environ.get("NAKAMA_UI_ALLOW_HOSTS", "").split(",") if h.strip()}
    handler = type("NakamaHandler", (_Handler,), {
        "token": secrets.token_urlsafe(24),
        "allowed_hosts": {"localhost", "127.0.0.1", "::1"} | extra,
        "password": os.environ.get("NAKAMA_UI_PASSWORD", ""),
    })
    if host not in ("127.0.0.1", "localhost", "::1") and not handler.password:
        log.warning("UI listens on %s without NAKAMA_UI_PASSWORD; anyone allowed by Host can open it", host)
    return ThreadingHTTPServer((host, port), handler)


def serve_ui(host: str = "127.0.0.1", port: int = 8765, background: bool = False) -> ThreadingHTTPServer:
    server = make_server(host, port)
    log.info("Nakama UI: http://localhost:%d/", port)
    if background:
        threading.Thread(target=server.serve_forever, name="nakama-ui", daemon=True).start()
    else:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("UI stopped")
    return server
