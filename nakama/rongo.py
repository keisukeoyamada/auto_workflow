"""The Analects, one chapter a day (dashboard tab 「論語」). Optional; off unless fleet.yaml `rongo.enabled`.

- The original text (public domain, about 500 chapters, ~50 KB) is downloaded once from the Chinese Text
  Project API into workspace/rongo/source.json. Nothing else is sent anywhere for that.
- Each morning Claude writes the next few chapters' 書き下し文 / 現代語訳 / 語句 / 教訓 / 今日の問い into
  workspace/rongo/notes/<id>.md (one short call per chapter, no tools). Only the chapter text is sent.
- What you have read is kept in workspace/rongo/progress.json (all of workspace/ is git-ignored).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import ROOT, WORKSPACE_DIR, load_fleet

log = logging.getLogger(__name__)

DIR = WORKSPACE_DIR / "rongo"
API = "https://api.ctext.org/gettext?urn=ctp:analects/"
SOURCE_LABEL = "Chinese Text Project（ctext.org）"
# (ctext slug, 篇名)
BOOKS = [
    ("xue-er", "学而"), ("wei-zheng", "為政"), ("ba-yi", "八佾"), ("li-ren", "里仁"), ("gong-ye-chang", "公冶長"),
    ("yong-ye", "雍也"), ("shu-er", "述而"), ("tai-bo", "泰伯"), ("zi-han", "子罕"), ("xiang-dang", "郷党"),
    ("xian-jin", "先進"), ("yan-yuan", "顔淵"), ("zi-lu", "子路"), ("xian-wen", "憲問"), ("wei-ling-gong", "衛霊公"),
    ("ji-shi", "季氏"), ("yang-huo", "陽貨"), ("wei-zi", "微子"), ("zi-zhang", "子張"), ("yao-yue", "堯曰"),
]
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_SCHEDULE = "15 5 * * *"
SECTIONS = ("書き下し文", "現代語訳", "語句", "教訓", "今日の問い")
SYSTEM_PROMPT = (
    "あなたは『論語』を一般の社会人に教える講師です。渡された 1 章について、指定の見出しの Markdown だけを書いてください。"
    "前置き・結びの挨拶・見出し以外の文は書かない。\n"
    "- 書き下し文: 一般的な訓読に従い、現代仮名遣いで。\n"
    "- 現代語訳: 平易な現代日本語で。補った言葉は（ ）に入れる。\n"
    "- 語句: 難しい語や人物を 2〜4 個、「- 語: 意味」の形で 1 行ずつ。\n"
    "- 教訓: 2〜3 文。仕事・家庭など今の生活でどう生かせるか。\n"
    "- 今日の問い: 自分に問いかける短い質問を 1 つ。\n"
    "解釈が分かれる章は、その旨と主な解釈を短く添える。原文にないことを断定しない。"
)

_lock = threading.Lock()
_gen_lock = threading.Lock()


# ---------- config & files ----------

def config() -> dict[str, Any]:
    cfg = getattr(load_fleet(), "rongo", None) or {}
    return cfg if isinstance(cfg, dict) else {}


def enabled() -> bool:
    return bool(config().get("enabled"))


def _require_enabled() -> dict[str, Any]:
    cfg = config()
    if not cfg.get("enabled"):
        raise PermissionError("論語は無効です（fleet.yaml の rongo.enabled を true に）")
    return cfg


def _read_json(name: str, default: Any) -> Any:
    try:
        return json.loads((DIR / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(name: str, data: Any) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / f"{name}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DIR / name)


def chapters() -> list[dict[str, Any]]:
    return list(_read_json("source.json", {}).get("chapters") or [])


def _note_path(cid: str):
    if not re.fullmatch(r"\d{2}-\d{2}", cid or ""):
        raise ValueError("章の指定が不正です")
    return DIR / "notes" / f"{cid}.md"


def _today() -> date:
    return datetime.now(ZoneInfo(load_fleet().timezone)).date()


# ---------- import (once) ----------

def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "nakama-rongo/1 (personal use)"})
    with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310 - fixed https host
        return json.loads(res.read().decode("utf-8"))


def _paragraphs(data: Any, slug: str) -> list[str]:
    """The chapter texts of one book from a gettext response. Strict: anything unexpected is an error."""
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise ValueError(f"ctext がエラーを返しました（{slug}）: {err.get('description') if isinstance(err, dict) else err}")
    items = data.get("fulltext") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError(f"ctext の応答の形が想定と違います（{slug}）: {json.dumps(data, ensure_ascii=False)[:200]}")
    out = []
    for x in items:
        text = x if isinstance(x, str) else x.get("text") if isinstance(x, dict) else None
        if not isinstance(text, str):
            raise ValueError(f"ctext の応答の形が想定と違います（{slug}）: {json.dumps(x, ensure_ascii=False)[:200]}")
        if text.strip():
            out.append(text.strip())
    return out


def import_source(fetch: Callable[[str], Any] | None = None, pause: float = 1.0) -> dict[str, Any]:
    """Download all 20 books once. All or nothing: a failure saves nothing."""
    _require_enabled()
    fetch = fetch or _http_json
    found: list[dict[str, Any]] = []
    for i, (slug, name) in enumerate(BOOKS, start=1):
        try:
            texts = _paragraphs(fetch(API + slug), slug)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"原文を取得できませんでした（{name}）: {exc}") from exc
        found += [{"id": f"{i:02d}-{n:02d}", "book": i, "book_name": name, "no": n, "text": t}
                  for n, t in enumerate(texts, start=1)]
        if pause and i < len(BOOKS):
            time.sleep(pause)  # be polite to a free service
    with _lock:
        _write_json("source.json", {"source": SOURCE_LABEL, "fetched_at": datetime.now().isoformat(timespec="seconds"),
                                    "chapters": found})
    return {"chapters": len(found), "books": len(BOOKS)}


# ---------- reading progress ----------

def _progress() -> dict[str, str]:
    data = _read_json("progress.json", {})
    return dict(data.get("read") or {}) if isinstance(data, dict) else {}


def mark_read(cid: str, read: bool = True, today: date | None = None) -> dict[str, Any]:
    _require_enabled()
    if cid not in {c["id"] for c in chapters()}:
        raise ValueError("その章はありません")
    with _lock:
        done = _progress()
        if read:
            done.setdefault(cid, (today or _today()).isoformat())
        else:
            done.pop(cid, None)
        _write_json("progress.json", {"read": done})
    return status(cid)


def _streak(done: dict[str, str], today: date) -> int:
    days = set(done.values())
    d = today if today.isoformat() in days else today - timedelta(days=1)  # today not read yet keeps yesterday's run
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def _chapter_view(c: dict[str, Any], done: dict[str, str]) -> dict[str, Any]:
    path = _note_path(c["id"])
    return c | {"read_on": done.get(c["id"]), "note": path.read_text(encoding="utf-8") if path.exists() else None}


def status(cid: str = "", today: date | None = None) -> dict[str, Any]:
    cfg = config()
    base = {"enabled": bool(cfg.get("enabled")), "schedule": str(cfg.get("schedule") or DEFAULT_SCHEDULE),
            "generation": _read_json("generation.json", {})}
    if not base["enabled"]:
        return base
    chs = chapters()
    if not chs:
        return base | {"imported": False, "total": 0}
    done = _progress()
    today = today or _today()
    ids = [c["id"] for c in chs]
    nxt = next((c for c in chs if c["id"] not in done), None)
    current = next((c for c in chs if c["id"] == cid), None) if cid else nxt
    current = current or chs[-1]
    i = ids.index(current["id"])
    books = []
    for no, (_, name) in enumerate(BOOKS, start=1):
        in_book = [c for c in chs if c["book"] == no]
        books.append({"no": no, "name": name, "total": len(in_book),
                      "chapters": [{"id": c["id"], "no": c["no"], "read": c["id"] in done} for c in in_book]})
    return base | {
        "imported": True, "source": _read_json("source.json", {}).get("source", SOURCE_LABEL),
        "total": len(chs), "read": sum(1 for x in ids if x in done), "streak": _streak(done, today),
        "read_today": sum(1 for v in done.values() if v == today.isoformat()),
        "next_id": nxt["id"] if nxt else None, "current": _chapter_view(current, done),
        "prev_id": ids[i - 1] if i > 0 else None, "following_id": ids[i + 1] if i + 1 < len(ids) else None,
        "notes_ready": sum(1 for x in ids if x not in done and _note_path(x).exists()),
        "books": books,
    }


# ---------- explanations (Claude) ----------

Ask = Callable[[str], Awaitable[tuple[str, float]]]


async def _ask_claude(prompt: str) -> tuple[str, float]:
    """One call, no tools. Same CLI, credentials and isolation settings as the agents."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

    from .runner import resolve_auth

    fleet = load_fleet()
    resolve_auth(fleet)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT, model=str(config().get("model") or DEFAULT_MODEL),
        tools=[], allowed_tools=[], permission_mode="dontAsk", max_turns=1, cwd=str(ROOT),
        setting_sources=[], extra_args={"no-session-persistence": None},
    )
    text, cost = "", 0.0
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts = [b.text for b in message.content if isinstance(b, TextBlock)]
            if parts:
                text = "\n".join(parts)
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            if message.is_error:
                raise RuntimeError(f"Claude がエラーを返しました: {message.subtype}")
    return text, cost


def _prompt(c: dict[str, Any]) -> str:
    heads = "\n".join(f"## {s}" for s in SECTIONS)
    return f"論語 {c['book_name']}篇 第{c['no']}章\n\n原文:\n{c['text']}\n\n次の見出しで書いてください。\n{heads}"


async def generate(ids: list[str], ask: Ask | None = None) -> dict[str, Any]:
    """Write notes for the given chapters (skips ones that exist). Stops at the first failure."""
    from .state import record_cost, spend_on

    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    ask = ask or _ask_claude
    by_id = {c["id"]: c for c in chapters()}
    made, cost = [], 0.0
    budget = float(fleet.limits.get("daily_budget_usd") or 0)
    for cid in ids:
        path = _note_path(cid)
        if path.exists() or cid not in by_id:
            continue
        now = datetime.now(tz)
        if budget and sum(spend_on(now.date().isoformat(), tz).values()) >= budget:
            raise RuntimeError(f"日次予算 ${budget:.2f} に到達しているので作りませんでした")
        text, c = await ask(_prompt(by_id[cid]))
        record_cost("rongo", now, c, 1, False)
        cost += c
        if "## 現代語訳" not in text:
            raise RuntimeError(f"{cid}: 解説の形が想定と違ったので保存しませんでした")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", encoding="utf-8")
        made.append(cid)
    return {"made": made, "cost_usd": round(cost, 4)}


def upcoming(n: int | None = None) -> list[str]:
    """The next n unread chapters in order (the current one first)."""
    n = int(n or config().get("ahead") or 3)
    done = _progress()
    return [c["id"] for c in chapters() if c["id"] not in done][:max(1, min(n, 20))]


def run_generation(ids: list[str] | None = None, ask: Ask | None = None) -> dict[str, Any]:
    """Blocking entry point for the scheduler and the dashboard; one run at a time, result in generation.json."""
    if not enabled() or not chapters():
        return {"skipped": True}
    if not _gen_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "running"}
    try:
        _write_json("generation.json", {"running": True, "started": datetime.now().isoformat(timespec="seconds")})
        try:
            result = asyncio.run(generate(ids or upcoming(), ask))
            _write_json("generation.json", {"running": False, "at": datetime.now().isoformat(timespec="seconds"), **result})
            return result
        except Exception as exc:  # noqa: BLE001 - reported on the tab, never crashes the caller
            log.exception("rongo generation failed")
            _write_json("generation.json", {"running": False, "at": datetime.now().isoformat(timespec="seconds"),
                                            "error": str(exc)[:300]})
            return {"error": str(exc)}
    finally:
        _gen_lock.release()


def start_generation(cid: str = "") -> dict[str, Any]:
    """Dashboard button: make the explanation for this chapter (and the next ones) in the background."""
    _require_enabled()
    if not chapters():
        raise ValueError("先に原文を取り込んでください")
    ids = ([cid] if cid else []) + [x for x in upcoming() if x != cid]
    if cid:
        _note_path(cid)
    threading.Thread(target=run_generation, args=(ids,), name="rongo-gen", daemon=True).start()
    return {"started": ids}
