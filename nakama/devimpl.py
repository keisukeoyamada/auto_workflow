"""Dashboard side of the devbox: hand 着手OK requests to the devbox container and read back its results.

Queue (shared with the devbox container, nothing else is shared): workspace/devq/
  jobs/<id>.json      written here: the request, its spec and how to run (model, budget, repository)
  running/<id>.json   the devbox is working on it
  results/<id>.json   written by the devbox: the draft PR, or why it failed; read back by ingest()
  devbox.json         the devbox's heartbeat (what it is doing now)

Status flow on the request: 着手OK (ready) -> 実装中 (working) -> レビュー待ち (review, with the PR number)
or, on failure, back to 要相談 (consult) with the reason in the memo. Merging the PR is always the owner's.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any

from . import devreq
from .config import WORKSPACE_DIR, load_fleet

log = logging.getLogger(__name__)

QUEUE = WORKSPACE_DIR / "devq"
DEFAULT_SCHEDULE = "20 1 * * *"  # at night: picks up everything marked 着手OK during the day
_lock = threading.Lock()


def config() -> dict[str, Any]:
    cfg = (getattr(load_fleet(), "devreq", None) or {}).get("implement") or {}
    return cfg if isinstance(cfg, dict) else {}


def enabled() -> bool:
    return bool(config().get("enabled"))


def schedule() -> str:
    return str(config().get("schedule") or DEFAULT_SCHEDULE)


def _repo_slug() -> str:
    from .ui import build_info

    repo = build_info().get("repo") or ""
    return repo.removeprefix("https://github.com/")


def _queued_ids() -> set[str]:
    return {p.stem for d in ("jobs", "running") for p in (QUEUE / d).glob("*.json")}


def enqueue(ids: list[str] | None = None) -> dict[str, Any]:
    """Queue the given (or all) 着手OK requests for the devbox. Each becomes 実装中."""
    if not enabled():
        raise PermissionError("実装（devbox）は無効です（fleet.yaml の devreq.implement.enabled）")
    cfg = config()
    slug = _repo_slug()
    with _lock:
        ingest()
        busy = _queued_ids()
        rows = [r for r in devreq.load() if r.get("status") == "ready" and r["id"] not in busy
                and (not ids or r["id"] in ids)]
        (QUEUE / "jobs").mkdir(parents=True, exist_ok=True)
        for r in rows:
            job = {"id": r["id"], "text": r.get("text", ""), "instruction": r.get("instruction", ""),
                   "triage": r.get("triage") or {}, "repo_slug": slug,
                   "model": str(cfg.get("model") or "claude-sonnet-5-5"),
                   "budget_usd": float(cfg.get("budget_usd") or 3.0), "rounds": int(cfg.get("rounds") or 3),
                   "queued": datetime.now().isoformat(timespec="seconds")}
            tmp = QUEUE / "jobs" / f"{r['id']}.tmp"
            tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(QUEUE / "jobs" / f"{r['id']}.json")
            devreq.update(r["id"], status="working")
    return {"queued": [r["id"] for r in rows]}


def ingest() -> list[str]:
    """Apply the devbox's finished results to the requests (PR number, release note, status)."""
    done = []
    folder = QUEUE / "results"
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            res = json.loads(path.read_text(encoding="utf-8"))
            rid = str(res.get("id") or path.stem)
            if res.get("ok"):
                pr = res.get("pr") or {}
                devreq.update(rid, status="review", pr=str(pr.get("url") or ""),
                              change=res.get("change") or None, usage=res.get("usage") or None,
                              note=f"devbox が下書き PR #{pr.get('number')} を作りました。レビューしてマージしてください。")
            else:
                devreq.update(rid, status="consult",
                              note=f"devbox で実装できませんでした: {res.get('error', '')}"[:900])
            done.append(rid)
        except (OSError, ValueError) as exc:  # unknown id or a broken file: keep it aside, never loop on it
            log.warning("devbox result %s: %s", path.name, exc)
        (folder / "done").mkdir(exist_ok=True)
        path.replace(folder / "done" / f"{path.stem}-{datetime.now():%Y%m%d%H%M%S}.json")
    return done


def status() -> dict[str, Any]:
    try:
        beat = json.loads((QUEUE / "devbox.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        beat = {}
    alive = False
    if beat.get("at"):
        try:
            alive = datetime.now() - datetime.fromisoformat(beat["at"]) < timedelta(minutes=5)
        except ValueError:
            pass
    return {"enabled": enabled(), "schedule": schedule(), "devbox": beat, "alive": alive,
            "queued": sorted(_queued_ids())}


def run_scheduled() -> dict[str, Any]:
    if not enabled():
        return {"skipped": True}
    return enqueue()
