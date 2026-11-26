"""Manual ticketing: Kihyo drafts from pasted text (review mode), Nakama validates, the user approves.

Flow (all state lives in workspace/kihyo/manual/<job>/, never in the synced folder until approval):
  input.md      text the user pasted in the dashboard
  draft.json    written by Kihyo in review mode (same schema as the SKILL's draft)
  promoted.json written here after the user approved and the file was created in tickets/

Duplicate protection does not rely on the model alone. Before anything is promoted we check, in code:
  - schema / due / priority / assignee ids against roster.json
  - close candidates must exist in the latest planner_state (exact id)
  - titles against open cards, finished recurring cards and tickets promoted in the last 7 days
  - tickets/ must not hold an unprocessed promoted file (one create event per file, see SKILL)
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ticketing import Paths, TicketingError, create_promoted

PRIORITIES = {"high", "medium", "low"}
SUFFIX_RE = re.compile(r"\s*[🟢🟠🔵🟣✋]\s*\d{1,2}/\d{1,2}.*$")  # source suffix like " 🟢7/16朝会で起票"
RECENT_DAYS = 7


def norm(title: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(title or "")))


def core(title: Any) -> str:
    """Title without the daily-changing source suffix, normalised (the SKILL's 'core title')."""
    return norm(SUFFIX_RE.sub("", unicodedata.normalize("NFKC", str(title or ""))))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_state(paths: Paths) -> tuple[Path | None, list[dict]]:
    files = [p for sub in ("planner_state_am", "planner_state_pm") for p in (paths.state / sub).glob("planner_state_*.json")]
    if not files:
        return None, []
    newest = max(files, key=lambda p: p.stat().st_mtime)
    data = _read_json(newest)
    return newest, [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def roster_ids(paths: Paths) -> dict[str, str]:
    data = _read_json(paths.roster) or {}
    return {str(m.get("id")): str(m.get("displayName") or "") for m in data.get("members") or [] if isinstance(m, dict)}


def recent_promoted_titles(paths: Paths, days: int = RECENT_DAYS) -> dict[str, str]:
    """core title -> file name, for tickets promoted in the last `days` days (root + archive)."""
    since = datetime.now().timestamp() - days * 86400
    out: dict[str, str] = {}
    for f in list(paths.tickets.glob("tickets_*.json")) + list(paths.archive.glob("*.json")):
        if f.stat().st_mtime < since:
            continue
        data = _read_json(f)
        for t in (data or {}).get("tasks") or [] if isinstance(data, dict) else []:
            if isinstance(t, dict) and t.get("title"):
                out[core(t["title"])] = f.name
    return out


def validate(paths: Paths, draft: Any) -> dict[str, Any]:
    """Per-task / per-close findings. `errors` block promotion; `warnings` need an explicit tick."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return {"errors": ["draft.json が JSON オブジェクトではありません"], "tasks": [], "closes": []}
    for key in ("tasks", "close_candidates"):
        if not isinstance(draft.get(key), list):
            errors.append(f"{key} が配列ではありません")
    state_file, state = latest_state(paths)
    if state_file is None:
        errors.append("planner_state が見つかりません（重複チェックができないので昇格不可）")
    ids = {str(t.get("id")): t for t in state}
    open_core = {core(t.get("title")): t for t in state if (t.get("percentComplete") or 0) < 100}
    done_count: dict[str, int] = {}
    for t in state:
        if (t.get("percentComplete") or 0) >= 100:
            done_count[core(t.get("title"))] = done_count.get(core(t.get("title")), 0) + 1
    roster = roster_ids(paths)
    recent = recent_promoted_titles(paths)

    tasks, seen = [], set()
    for i, t in enumerate(draft.get("tasks") or []):
        errs, warns = [], []
        if not isinstance(t, dict) or not str(t.get("title") or "").strip():
            tasks.append({"index": i, "title": "", "errors": ["title がありません"], "warnings": []})
            continue
        c = core(t["title"])
        due = str(t.get("due") or "")
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            errs.append(f"due の形式が不正: {due}")
        if t.get("priority") and t["priority"] not in PRIORITIES:
            errs.append(f"priority が不正: {t['priority']}")
        for gid in [t.get("assignee_id")] + list(t.get("assignee_ids") or []):
            if gid and str(gid) not in roster:
                errs.append(f"assignee_id が roster にありません: {gid}")
        if not t.get("assignee_id"):
            warns.append("担当者が未割当（Planner に未割当で入ります）")
        if c in seen:
            errs.append("この draft の中で同じタスクが重複しています")
        seen.add(c)
        if c in open_core:
            warns.append(f"未完了の既存カードと同名: {open_core[c].get('title')}")
        if done_count.get(c, 0) >= 1:
            warns.append(f"完了済みに同名カードが {done_count[c]} 枚（繰り返し運用のカードの可能性）")
        if c in recent:
            warns.append(f"直近 {RECENT_DAYS} 日に起票済み（{recent[c]}）")
        tasks.append({"index": i, "title": str(t["title"]), "due": due, "priority": str(t.get("priority") or ""),
                      "assignee": roster.get(str(t.get("assignee_id") or ""), "未割当"),
                      "memo": str(t.get("memo") or "")[:300], "errors": errs, "warnings": warns})

    closes = []
    for i, c in enumerate(draft.get("close_candidates") or []):
        errs = []
        if not isinstance(c, dict):
            closes.append({"index": i, "title": "", "errors": ["不正な要素"], "warnings": []})
            continue
        pid = str(c.get("planner_task_id") or "")
        card = ids.get(pid)
        if not card:
            errs.append("planner_task_id が最新の planner_state にありません（手打ち・推測の ID は昇格禁止）")
        elif (card.get("percentComplete") or 0) >= 100:
            errs.append("対象カードは既に完了しています")
        if not str(c.get("note") or "").strip():
            errs.append("note が空です")
        closes.append({"index": i, "title": str(c.get("title") or (card or {}).get("title") or ""), "note": str(c.get("note") or ""),
                       "errors": errs, "warnings": []})
    return {"errors": errors, "tasks": tasks, "closes": closes,
            "state_file": f"{state_file.parent.name}/{state_file.name}" if state_file else None}


def landed_ratio(paths: Paths, promoted: Path) -> tuple[int, int]:
    """(landed, total) tasks of a promoted file found in a planner_state exported AFTER it (verify-via-state)."""
    data = _read_json(promoted) or {}
    titles = [core(t.get("title")) for t in data.get("tasks") or [] if isinstance(t, dict)]
    state_file, state = latest_state(paths)
    if not titles:
        return 0, 0
    if state_file is None or state_file.stat().st_mtime <= promoted.stat().st_mtime:
        return 0, len(titles)  # no export since promotion: cannot tell yet
    have = {core(t.get("title")) for t in state}
    return sum(1 for c in titles if c in have), len(titles)


def promote_manual(paths: Paths, draft: dict, task_idx: list[int], close_idx: list[int], job: str) -> dict[str, Any]:
    """Re-validate, archive a processed predecessor if needed, then create tickets/tickets_<today>.json."""
    report = validate(paths, draft)
    if report["errors"]:
        raise TicketingError("昇格できません: " + " / ".join(report["errors"]))
    chosen_t = [t for t in report["tasks"] if t["index"] in set(task_idx)]
    chosen_c = [c for c in report["closes"] if c["index"] in set(close_idx)]
    bad = [f"{x['title']}: {e}" for x in chosen_t + chosen_c for e in x["errors"]]
    if bad:
        raise TicketingError("エラーのある項目は昇格できません: " + " / ".join(bad))
    if not chosen_t and not chosen_c:
        raise TicketingError("昇格する項目が選ばれていません")

    today = date.today().isoformat()
    archived = None
    existing = paths.tickets / f"tickets_{today}.json"
    if existing.exists():
        landed, total = landed_ratio(paths, existing)
        if total == 0 or landed < total:
            raise TicketingError(
                f"tickets/{existing.name} がまだ PA に処理されていない可能性があります（Planner 着地 {landed}/{total}）。"
                "次の planner_state の書き出し後にもう一度試してください（上書きすると PA が動かないため）")
        prev_cycle = str((_read_json(existing) or {}).get("cycle") or "x")
        dest = paths.archive / f"tickets_{today}_{prev_cycle}_PROCESSED.json"
        if dest.exists():
            dest = paths.archive / f"tickets_{today}_{prev_cycle}_PROCESSED_{datetime.now():%H%M%S}.json"
        paths.archive.mkdir(parents=True, exist_ok=True)
        existing.replace(dest)
        archived = dest.name

    tasks = [draft["tasks"][t["index"]] for t in chosen_t]
    closes = [draft["close_candidates"][c["index"]] for c in chosen_c]
    final = {"generated_at": datetime.now().isoformat(timespec="seconds"), "cycle": "manual",
             "source_file": f"nakama-manual/{job}", "tasks": tasks, "close_candidates": closes}
    created = create_promoted(paths, today, final, tag=f"manual_{job}")
    return {"promoted": created.name, "archived": archived, "tasks": len(tasks), "closes": len(closes), "final": final}


def write_cycle_log(paths: Paths, job: str, result: dict, dropped_titles: list[str]) -> Path:
    """Step 7 equivalent for manual runs: logs/report_<date>_manual_<job>.md (never overwritten)."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    path = paths.logs / f"report_{date.today().isoformat()}_manual_{job}.md"
    if path.exists():
        return path
    lines = [f"# 起票レポート {date.today().isoformat()} manual ({job})", "",
             f"- 昇格: tickets/{result['promoted']}（PA 発火）" + (f" / 退避: archive/{result['archived']}" if result["archived"] else ""),
             f"- 新規 {result['tasks']} 件 / close・追記 {result['closes']} 件", "", "## 新規"]
    lines += [f"- {t.get('title')}（担当 {t.get('assignee_hint') or t.get('assignee_id') or '未割当'} / 期日 {t.get('due') or '-'}）"
              for t in result["final"]["tasks"]] or ["- なし"]
    lines += ["", "## close・追記"] + ([f"- {c.get('title')}: {c.get('note')}" for c in result["final"]["close_candidates"]] or ["- なし"])
    lines += ["", "## ユーザーが承認しなかった項目"] + ([f"- {t}" for t in dropped_titles] or ["- なし"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
