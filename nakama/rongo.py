"""The Analects, one chapter a day (dashboard tab 「論語」). Optional; off unless fleet.yaml `rongo.enabled`.

- The original text (public domain, 20 books / 512 chapters, simplified characters) ships with the app in
  nakama/data/lunyu.json (from the chinese-poetry project, MIT; see nakama/data/README.md). No download.
- Each morning Claude writes the next few chapters' 書き下し文 / 現代語訳 / 語句 / 教訓 / 今日の問い into
  workspace/rongo/notes/<id>.md (one short call per chapter, no tools). Only the chapter text is sent.
- What each login (owner / guest) has read and starred is kept in workspace/rongo/progress.json
  (all of workspace/ is git-ignored).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import ROOT, WORKSPACE_DIR, load_fleet

log = logging.getLogger(__name__)

DIR = WORKSPACE_DIR / "rongo"
DATA_FILE = Path(__file__).with_name("data") / "lunyu.json"
SOURCE_LABEL = "chinese-poetry（GitHub, MIT）の lunyu.json（簡体字）"
# 篇名 in Japanese, in the order of the data file
BOOKS = ["学而", "為政", "八佾", "里仁", "公冶長", "雍也", "述而", "泰伯", "子罕", "郷党",
         "先進", "顔淵", "子路", "憲問", "衛霊公", "季氏", "陽貨", "微子", "子張", "堯曰"]
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
    "原文は簡体字で渡すが、書き下し文・語句は日本の漢字（常用漢字）で書く。"
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


@lru_cache(maxsize=1)
def _bundled() -> tuple[dict[str, Any], ...]:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != len(BOOKS):
        raise ValueError(f"{DATA_FILE.name}: 20 篇の配列ではありません")
    out = []
    for i, (book, name) in enumerate(zip(data, BOOKS), start=1):
        texts = [t.strip() for t in book.get("paragraphs") or [] if isinstance(t, str) and t.strip()]
        out += [{"id": f"{i:02d}-{n:02d}", "book": i, "book_name": name, "no": n, "text": t}
                for n, t in enumerate(texts, start=1)]
    return tuple(out)


def chapters() -> list[dict[str, Any]]:
    return list(_bundled())


def _note_path(cid: str):
    if not re.fullmatch(r"\d{2}-\d{2}", cid or ""):
        raise ValueError("章の指定が不正です")
    return DIR / "notes" / f"{cid}.md"


def _today() -> date:
    return datetime.now(ZoneInfo(load_fleet().timezone)).date()


# ---------- reading progress ----------

USERS = ("owner", "guest")  # the dashboard logins; each keeps its own reading and stars


def _all_state() -> dict[str, dict[str, dict[str, str]]]:
    """progress.json = {"read": {id: date}, "fav": {id: date}, "users": {"guest": {"read": …, "fav": …}}}.
    The owner's record stays at the top level (the file from before per-user reading still works)."""
    data = _read_json("progress.json", {})
    data = data if isinstance(data, dict) else {}
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    out = {}
    for u in USERS:
        src = data if u == "owner" else (users.get(u) if isinstance(users.get(u), dict) else {})
        out[u] = {k: dict(src.get(k) or {}) for k in ("read", "fav")}
    return out


def _write_all(st: dict[str, dict[str, dict[str, str]]]) -> None:
    _write_json("progress.json", st["owner"] | {"users": {u: st[u] for u in USERS if u != "owner"}})


def _state(user: str = "owner") -> dict[str, dict[str, str]]:
    return _all_state()[_user(user)]


def _user(user: str) -> str:
    return user if user in USERS else "owner"


def _progress(user: str = "owner") -> dict[str, str]:
    return _state(user)["read"]


def _toggle(kind: str, cid: str, on: bool, today: date | None, user: str) -> dict[str, Any]:
    _require_enabled()
    if cid not in {c["id"] for c in chapters()}:
        raise ValueError("その章はありません")
    user = _user(user)
    with _lock:
        st = _all_state()
        if on:
            st[user][kind].setdefault(cid, (today or _today()).isoformat())
        else:
            st[user][kind].pop(cid, None)
        _write_all(st)
    return status(cid, user=user)


def mark_read(cid: str, read: bool = True, today: date | None = None, user: str = "owner") -> dict[str, Any]:
    return _toggle("read", cid, read, today, user)


def set_fav(cid: str, fav: bool = True, today: date | None = None, user: str = "owner") -> dict[str, Any]:
    """Star a chapter to come back to it (independent of reading)."""
    return _toggle("fav", cid, fav, today, user)


def _section(note: str | None, title: str) -> str:
    """The text under '## <title>' in a note (for the favourites list)."""
    if not note:
        return ""
    m = re.search(rf"^##\s*{re.escape(title)}\s*\n(.*?)(?=^##\s|\Z)", note, re.S | re.M)
    return m.group(1).strip() if m else ""


def _streak(done: dict[str, str], today: date) -> int:
    days = set(done.values())
    d = today if today.isoformat() in days else today - timedelta(days=1)  # today not read yet keeps yesterday's run
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def _chapter_view(c: dict[str, Any], done: dict[str, str], fav: dict[str, str]) -> dict[str, Any]:
    path = _note_path(c["id"])
    return c | {"read_on": done.get(c["id"]), "fav_on": fav.get(c["id"]),
                "note": path.read_text(encoding="utf-8") if path.exists() else None}


def status(cid: str = "", today: date | None = None, user: str = "owner",
           people: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The tab for one login (`user`): its own progress, streak and stars, plus who else read each chapter.
    `people` = the logins to show, [{role, name, color}] (the UI passes these; default: just the owner)."""
    cfg = config()
    from . import cronja

    schedule = str(cfg.get("schedule") or DEFAULT_SCHEDULE)
    base = {"enabled": bool(cfg.get("enabled")), "schedule": schedule, "schedule_ja": cronja.describe(schedule),
            "generation": _read_json("generation.json", {})}
    if not base["enabled"]:
        return base
    chs = chapters()
    user = _user(user)
    every = _all_state()
    st = every[user]
    done, fav = st["read"], st["fav"]
    people = [p for p in (people or [{"role": "owner", "name": "", "color": "blue"}]) if p.get("role") in USERS]
    if user not in {p["role"] for p in people}:
        people = people + [{"role": user, "name": "", "color": "gray"}]
    readers = [p | {"me": p["role"] == user, "read": len(every[p["role"]]["read"])} for p in people]
    who = lambda x: [p["role"] for p in people if x in every[p["role"]]["read"]]  # noqa: E731
    today = today or _today()
    ids = [c["id"] for c in chs]
    nxt = next((c for c in chs if c["id"] not in done), None)
    current = next((c for c in chs if c["id"] == cid), None) if cid else nxt
    current = current or chs[-1]
    i = ids.index(current["id"])
    books = []
    for no, name in enumerate(BOOKS, start=1):
        in_book = [c for c in chs if c["book"] == no]
        books.append({"no": no, "name": name, "total": len(in_book),
                      "chapters": [{"id": c["id"], "no": c["no"], "read": c["id"] in done, "fav": c["id"] in fav,
                                    "read_by": who(c["id"])}
                                   for c in in_book]})
    favorites = []
    for c in chs:  # in the book's order
        if c["id"] in fav:
            path = _note_path(c["id"])
            note = path.read_text(encoding="utf-8") if path.exists() else None
            favorites.append({k: c[k] for k in ("id", "book_name", "no", "text")}
                             | {"fav_on": fav[c["id"]], "translation": _section(note, "現代語訳"),
                                "lesson": _section(note, "教訓")})
    return base | {
        "source": SOURCE_LABEL,
        "total": len(chs), "read": sum(1 for x in ids if x in done), "streak": _streak(done, today),
        "read_today": sum(1 for v in done.values() if v == today.isoformat()),
        "next_id": nxt["id"] if nxt else None,
        "current": _chapter_view(current, done, fav)
        | {"read_by": [{"role": p["role"], "on": every[p["role"]]["read"][current["id"]]}
                       for p in people if current["id"] in every[p["role"]]["read"]]},
        "user": user, "readers": readers,
        "prev_id": ids[i - 1] if i > 0 else None, "following_id": ids[i + 1] if i + 1 < len(ids) else None,
        "notes_ready": sum(1 for x in ids if x not in done and _note_path(x).exists()),
        "books": books, "favorites": favorites,
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
    """The next n unread chapters for each reader, in the book's order (the owner's current one first)."""
    n = max(1, min(int(n or config().get("ahead") or 3), 20))
    every = _all_state()
    out: list[str] = []
    for u in USERS:
        if u != "owner" and not every[u]["read"]:
            continue  # someone who never started does not pull notes for chapter 1
        for x in [c["id"] for c in chapters() if c["id"] not in every[u]["read"]][:n]:
            if x not in out:
                out.append(x)
    return out


def run_generation(ids: list[str] | None = None, ask: Ask | None = None) -> dict[str, Any]:
    """Blocking entry point for the scheduler and the dashboard; one run at a time, result in generation.json."""
    if not enabled():
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
    ids = ([cid] if cid else []) + [x for x in upcoming() if x != cid]
    if cid:
        _note_path(cid)
    threading.Thread(target=run_generation, args=(ids,), name="rongo-gen", daemon=True).start()
    return {"started": ids}
