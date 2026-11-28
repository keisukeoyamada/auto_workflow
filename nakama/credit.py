"""Credit card statement split (dashboard tab 「クレジット精算」). Code only, no agent.

Monthly flow: download the card statement CSV -> upload it in the dashboard -> both people go
through the rows and pick who pays each one -> enter the bank account balance -> close the month.

Each row's amount is split three ways: member A, member B, and the shared household budget
("shared" = whatever A and B do not pay). Nothing is split by ratio; a row is one of
  shared (default) | a | b | half | custom (A and B amounts typed by hand).
Deposits into the debit account for the month:
  A = sum of A's parts, B = sum of B's parts, shared budget = sum of shared parts - account balance.

Everything personal stays out of git:
- names, colours, labels and the CSV column names live in config/fleet.yaml `credit:` (git-ignored)
- statements, uploaded CSVs and exports live in workspace/credit/ (git-ignored)
The feature is off unless fleet.yaml sets `credit.enabled: true`.
"""
from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import json
import re
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import WORKSPACE_DIR, load_fleet

DIR = WORKSPACE_DIR / "credit"
SPLITS = ("shared", "a", "b", "half", "custom")
COLORS = ("blue", "pink", "green", "orange", "purple", "gray")
MAX_UPLOAD_BYTES = 700 * 1024
# Header names tried for each column (first match wins). fleet.yaml credit.csv adds to these.
DEFAULT_COLUMNS: dict[str, list[str]] = {
    "date": ["ご利用日", "利用日", "ご利用年月日", "利用年月日", "日付", "取引日"],
    "merchant": ["ご利用内容", "利用内容", "ご利用店名", "利用店名", "ご利用先", "利用先", "内容", "摘要", "店名"],
    "amount": ["ご利用金額", "利用金額", "金額", "ご利用金額(円)", "利用金額(円)", "金額(円)", "請求金額"],
    "holder": ["ご利用者", "利用者", "カード会員", "カード会員名", "カード会員様名", "カード会員様氏名", "会員様名",
               "会員名", "会員氏名", "名義", "使用者", "誰"],
    "card": ["会員番号#", "会員番号", "カード番号", "カード番号下4桁", "カード下4桁"],  # optional: which card was used
}
# Rows that are not purchases (e.g. the previous statement's bank debit). fleet.yaml credit.exclude adds to these.
DEFAULT_EXCLUDE = ["前回分口座振替"]

_lock = threading.Lock()


# ---------- config ----------

def config() -> dict[str, Any]:
    cfg = getattr(load_fleet(), "credit", None) or {}
    return cfg if isinstance(cfg, dict) else {}


def enabled() -> bool:
    return bool(config().get("enabled"))


def _require_enabled() -> dict[str, Any]:
    cfg = config()
    if not cfg.get("enabled"):
        raise PermissionError("クレジット精算は無効です（fleet.yaml の credit.enabled を true に）")
    return cfg


def members(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Exactly two members (A, B) with label, colour and the names that identify them in the CSV."""
    cfg = config() if cfg is None else cfg
    raw = [m for m in (cfg.get("members") or []) if isinstance(m, dict)][:2]
    out = []
    for i, key in enumerate(("a", "b")):
        m = raw[i] if i < len(raw) else {}
        color = str(m.get("color") or ("blue", "pink")[i])
        out.append({
            "key": key,
            "label": str(m.get("label") or f"メンバー{key.upper()}"),
            "color": color if color in COLORS else "gray",
            "match": [_norm(x) for x in (m.get("match") or []) if str(x).strip()],
        })
    return out


NOTE_KEYS = ("shared",)  # memos the dashboard shows on a label (shared = the joint account): workspace/credit/notes.json


def notes() -> dict[str, dict[str, str]]:
    try:
        data = json.loads((DIR / "notes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in NOTE_KEYS and isinstance(v, dict)} if isinstance(data, dict) else {}


def set_note(key: str, text: str, who: dict[str, Any] | None = None) -> dict[str, Any]:
    """Save the memo for a label (anyone who can open the tab may edit it; empty text removes it)."""
    _require_enabled()
    if key not in NOTE_KEYS:
        raise ValueError("そのメモはありません")
    with _lock:
        data = notes()
        text = str(text or "").strip()[:2000]
        if text:
            data[key] = {"text": text, "by": str((who or {}).get("name") or ""),
                         "at": datetime.now().isoformat(timespec="minutes")}
        else:
            data.pop(key, None)
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = DIR / "notes.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(DIR / "notes.json")
    return public_config()


def public_config() -> dict[str, Any]:
    """What the dashboard needs (no CSV match strings)."""
    cfg = config()
    return {
        "notes": notes(),
        "enabled": bool(cfg.get("enabled")),
        "members": [{k: m[k] for k in ("key", "label", "color")} for m in members(cfg)],
        "shared_label": str(cfg.get("shared_label") or "共同"),
        "account_label": str(cfg.get("account_label") or "引き落とし口座"),
    }


# ---------- parsing ----------

def _norm(s: Any) -> str:
    """NFKC (full-width -> half-width), collapse spaces, casefold: for matching only, never for display."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(s or ""))).strip().casefold()


def _header_key(s: str) -> str:
    return re.sub(r"[\s()（）]", "", unicodedata.normalize("NFKC", s or ""))


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判別できません（UTF-8 か Shift_JIS の CSV を使ってください）")


def parse_date(s: str) -> str | None:
    s = unicodedata.normalize("NFKC", s or "").strip()
    m = re.fullmatch(r"(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?(?:\s.*)?", s) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_amount(s: str) -> int | None:
    s = unicodedata.normalize("NFKC", s or "").strip().replace(",", "").replace("¥", "").replace("\\", "").replace("円", "")
    s = s.replace("△", "-").replace("▲", "-").replace(" ", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return int(round(float(s)))


def _find_columns(row: list[str], cfg: dict[str, Any]) -> dict[str, int] | None:
    extra = cfg.get("csv") if isinstance(cfg.get("csv"), dict) else {}
    cells = [_header_key(c) for c in row]
    found: dict[str, int] = {}
    for col, names in DEFAULT_COLUMNS.items():
        wanted = [_header_key(str(n)) for n in (extra.get(col) or [])] + [_header_key(n) for n in names]
        for name in wanted:
            if name in cells:
                found[col] = cells.index(name)
                break
    return found if {"date", "merchant", "amount"} <= set(found) else None


def _holder_key(name: str, mems: list[dict[str, Any]]) -> str:
    n = _norm(name)
    if not n:
        return ""
    for m in mems:
        if any(x and (x in n or n in x) for x in m["match"]):
            return m["key"]
    return ""


def parse_csv(text: str, cfg: dict[str, Any] | None = None, excluded: list | None = None) -> list[dict[str, Any]]:
    """Statement rows: date, holder (raw), holder_key (a/b/''), card (last digits or ''), merchant (as written), amount.

    Works with a holder column per row, or with section lines naming the card member
    (a line that matches a member and has no date/amount switches the current holder).
    Rows whose merchant matches an exclude pattern (bank debit of the last bill etc.) go to `excluded`.
    """
    cfg = config() if cfg is None else cfg
    mems = members(cfg)
    skip = [_norm(x) for x in [*DEFAULT_EXCLUDE, *(cfg.get("exclude") or [])] if str(x).strip()]
    rows = list(csv.reader(io.StringIO(text)))
    cols = None
    current = ("", "")
    out: list[dict[str, Any]] = []
    for row in rows:
        row = [c.strip() for c in row]
        if not any(row):
            continue
        if cols is None:
            cols = _find_columns(row, cfg)
            if cols is None:
                key = next((_holder_key(c, mems) for c in row if _holder_key(c, mems)), "")
                if key:
                    current = (next(c for c in row if _holder_key(c, mems)), key)
            continue
        get = lambda col: row[cols[col]] if col in cols and cols[col] < len(row) else ""  # noqa: E731
        day, amount = parse_date(get("date")), parse_amount(get("amount"))
        if day is None or amount is None:
            hit = [c for c in row if _holder_key(c, mems)]
            if hit:
                current = (hit[0], _holder_key(hit[0], mems))
            continue  # totals, notes, blank lines
        holder = get("holder") or current[0]
        row_out = {"date": day, "holder": holder, "holder_key": _holder_key(holder, mems) or current[1],
                   "card": re.sub(r"\D", "", unicodedata.normalize("NFKC", get("card")))[-5:],
                   "merchant": get("merchant"), "amount": amount}
        if any(x in _norm(row_out["merchant"]) for x in skip):
            if excluded is not None:
                excluded.append(row_out)
            continue
        out.append(row_out)
    if cols is None:
        first = next((r for r in rows if any(c.strip() for c in r)), [])
        raise ValueError("CSV の見出し行が見つかりません（日付・内容・金額の列が必要）。"
                         f"先頭行: {', '.join(first[:8])}。列名が違う場合は fleet.yaml の credit.csv に追加してください")
    if not out:
        raise ValueError("明細の行がありません")
    return out


def detect_month(rows: list[dict[str, Any]], closing_day: int | None) -> str:
    """Statement month key YYYYMM: the month of the latest use, moved forward past the closing day."""
    last = max(date.fromisoformat(r["date"]) for r in rows)
    y, m = last.year, last.month
    if closing_day and last.day > int(closing_day):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}{m:02d}"


# ---------- storage ----------

def _month_ok(month: str) -> str:
    if not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", month or ""):
        raise ValueError("月は YYYYMM で指定してください")
    return month


def _path(month: str) -> Path:
    return DIR / "statements" / f"{_month_ok(month)}.json"


def _load(month: str) -> dict[str, Any]:
    path = _path(month)
    if not path.exists():
        raise FileNotFoundError(month)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(st: dict[str, Any]) -> None:
    path = _path(st["month"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _row_id(r: dict[str, Any], n: int) -> str:
    # holder is not part of the id: a later fix to member matching must not orphan what was entered
    key = f"{r['date']}|{_norm(r['merchant'])}|{r['amount']}|{n}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _with_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        base = _row_id(r, 0)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(r | {"id": _row_id(r, n)})
    return out


def import_csv(filename: str, data_b64: str, month: str = "", now: datetime | None = None) -> dict[str, Any]:
    cfg = _require_enabled()
    if filename.lower().endswith(".pdf"):
        raise ValueError("PDF は読み込めません。カード会社のサイトから CSV をダウンロードしてください")
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except ValueError as exc:
        raise ValueError("ファイルを読み込めませんでした") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("ファイルが大きすぎます")
    if filename.lower().endswith((".xlsx", ".xlsm")):
        return import_workbook(filename, raw, cfg, now)
    excluded: list[dict[str, Any]] = []
    parsed = _with_ids(parse_csv(decode(raw), cfg, excluded))
    month = _month_ok(month) if month else detect_month(parsed, cfg.get("closing_day"))
    now = now or datetime.now()
    with _lock:
        try:
            st = _load(month)
        except FileNotFoundError:
            st = {"month": month, "billed": None, "balance": None, "closed": False, "rows": []}
        if st.get("closed"):
            raise ValueError(f"{month} は確定済みです（取り込み直すには確定を取り消してください）")
        old = {r["id"]: r for r in st["rows"]}
        rows, kept = [], 0
        for r in parsed:
            prev = old.get(r["id"])
            if prev:  # keep what was entered; refresh what comes from the file
                kept += 1
                rows.append(prev | {k: r[k] for k in ("id", "date", "holder", "holder_key", "card", "merchant", "amount")})
            else:
                rows.append(r | {"split": "shared", "a": 0, "b": 0, "checked": False, "memo": ""})
        if len(rows) != kept or len(old) != kept:  # rows came or went: earlier approvals no longer hold
            st["approvals"] = {}
        st["rows"] = rows
        st["source"] = {"file": Path(filename).name[:120], "imported_at": now.isoformat(timespec="seconds")}
        raw_dir = DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{month}-{now:%Y%m%d%H%M%S}.csv").write_bytes(raw)  # original kept for re-import
        _save(st)
    dropped = len(old) - kept
    return {"month": month, "rows": len(rows), "added": len(rows) - kept, "kept": kept, "dropped": dropped,
            "excluded": [f"{x['date']} {x['merchant']} {x['amount']:,}" for x in excluded]}


# ---------- past months from a hand-made workbook ----------

def _arith(expr: str) -> float:
    """Evaluate + - * / and parentheses over numbers only (no names, no calls)."""
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(n.left), ev(n.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}.get(type(n.op)) if not isinstance(n.op, ast.Div) else a / b
        raise ValueError("unsupported formula")
    return ev(ast.parse(expr, mode="eval"))


def _num(v: Any) -> float:
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    n = parse_amount(str(v))
    if n is None:
        raise ValueError(f"not a number: {v}")
    return n


def _cell(ws: Any, v: Any, depth: int = 0) -> Any:
    """A cell's value; simple formulas (=E5, =E5/2, =$E$8/2, =E5-(F5+G5)) are computed here because
    workbooks saved by some apps carry no cached results. Functions, ranges and other sheets give None."""
    if not (isinstance(v, str) and v.startswith("=")):
        return v
    expr = v[1:].replace("$", "")
    if depth > 8 or re.search(r"[A-Za-z]{3,}|[!:]", expr):
        return None
    try:
        expr = re.sub(r"[A-Z]{1,2}[0-9]+", lambda m: repr(_num(_cell(ws, ws[m.group(0)].value, depth + 1))), expr)
        return _arith(expr)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError):
        return None


def _sheet_rows(ws: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """One month sheet laid out as 日付 / 誰 / 内容 / 状態 / 金額 / <A>負担額 / <B>負担額 / ... / memo."""
    mems = members(cfg)
    grid = [list(r) for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 2000), max_col=16)]
    head_i = next((i for i, r in enumerate(grid[:6])
                   if {"日付", "内容", "金額"} <= {str(c.value).strip() for c in r if c.value is not None}), None)
    if head_i is None:
        return None
    head = [str(c.value).strip() if c.value is not None else "" for c in grid[head_i]]
    col = {name: head.index(name) for name in ("日付", "誰", "内容", "状態", "金額", "memo") if name in head}
    burden = [i for i, h in enumerate(head) if h.endswith("負担額") and "から" not in h]
    if len(burden) < 2:
        return None
    a_col, b_col = burden[0], burden[1]
    if head[b_col].startswith(mems[0]["label"]) or head[a_col].startswith(mems[1]["label"]):
        a_col, b_col = b_col, a_col
    billed = balance = None
    for r in grid[:head_i]:
        for i, c in enumerate(r):
            text = str(c.value or "")
            m = re.search(r"ご?請求金額\D*?([\d,]+)", unicodedata.normalize("NFKC", text))
            if m:
                billed = int(m.group(1).replace(",", ""))
            if "残" in text and i > 0 and isinstance(r[i - 1].value, (int, float)):
                balance = int(r[i - 1].value)
    rows = []
    for r in grid[head_i + 1:]:
        val = lambda i: _cell(ws, r[i].value) if i is not None and i < len(r) else None  # noqa: E731
        d = val(col.get("日付"))
        day = d.date().isoformat() if isinstance(d, datetime) else d.isoformat() if isinstance(d, date) else parse_date(str(d or ""))
        try:
            amount = int(round(_num(val(col.get("金額")))))
            a, b = int(round(_num(val(a_col)))), int(round(_num(val(b_col))))
        except ValueError:
            continue
        if day is None or val(col.get("金額")) in (None, ""):
            continue
        if a == 0 and b == 0:
            split = "shared"
        elif a == amount and b == 0:
            split = "a"
        elif b == amount and a == 0:
            split = "b"
        elif abs(a - amount / 2) < 1 and abs(b - amount / 2) < 1:
            split = "half"
        else:
            split = "custom"
        holder = str(val(col.get("誰")) or "")
        rows.append({"date": day, "holder": holder, "holder_key": _holder_key(holder, mems), "card": "",
                     "merchant": str(val(col.get("内容")) or ""), "amount": amount, "split": split,
                     "a": a if split == "custom" else 0, "b": b if split == "custom" else 0,
                     "checked": True, "memo": str(val(col.get("memo")) or "")[:500]})
    return {"rows": rows, "billed": billed, "balance": balance} if rows else None


def import_workbook(filename: str, raw: bytes, cfg: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Past months from a workbook with one sheet per month named YYYYMM. Imported months are closed
    (read-only history); a month that already exists here is never overwritten."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - listed in pyproject
        raise ValueError("Excel の読み込みには openpyxl が必要です（イメージを作り直してください）") from exc
    import warnings

    now = now or datetime.now()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=False)
        except Exception as exc:  # noqa: BLE001 - any broken file is the user's input error
            raise ValueError("Excel ファイルを開けませんでした") from exc
    imported, skipped = [], []
    with _lock:
        for ws in wb.worksheets:
            month = unicodedata.normalize("NFKC", ws.title).strip()
            if not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", month):
                skipped.append({"sheet": ws.title, "reason": "シート名が YYYYMM ではない"})
                continue
            if _path(month).exists():
                skipped.append({"sheet": ws.title, "reason": "この月はもうある"})
                continue
            data = _sheet_rows(ws, cfg)
            if data is None:
                skipped.append({"sheet": ws.title, "reason": "列の並びが違う（日付・内容・金額・2 人分の負担額が必要）"})
                continue
            _save({"month": month, "billed": data["billed"], "balance": data["balance"], "closed": True,
                   "closed_at": now.isoformat(timespec="seconds"), "rows": _with_ids(data["rows"]),
                   "source": {"file": f"{Path(filename).name[:100]} / {ws.title}", "imported_at": now.isoformat(timespec="seconds")},
                   "history": True})
            imported.append({"month": month, "rows": len(data["rows"])})
        raw_dir = DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"workbook-{now:%Y%m%d%H%M%S}{Path(filename).suffix.lower()}").write_bytes(raw)
    if not imported:
        raise ValueError("取り込める月がありませんでした: " + " / ".join(f"{x['sheet']}（{x['reason']}）" for x in skipped))
    return {"month": max(x["month"] for x in imported), "workbook": {"imported": imported, "skipped": skipped}}


# ---------- what past months did with the same shop ----------

def _shop_keys(merchant: str) -> tuple[str, str]:
    """(first two words, first word) of the normalised shop name: statements append places and
    truncate differently from month to month, so the lookup falls back to the first word."""
    words = _norm(merchant).split(" ")
    return " ".join(words[:2]), words[0]


def _history(before: str) -> dict[str, list[dict[str, Any]]]:
    idx: dict[str, list[dict[str, Any]]] = {}
    folder = DIR / "statements"
    for p in sorted(folder.glob("*.json"), reverse=True) if folder.exists() else []:
        if p.stem >= before:
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in sorted(st.get("rows") or [], key=lambda r: r.get("date", ""), reverse=True):
            entry = {"month": st.get("month", p.stem), "split": r.get("split", "shared")}
            k2, k1 = _shop_keys(r.get("merchant", ""))
            idx.setdefault("2:" + k2, []).append(entry)
            idx.setdefault("1:" + k1, []).append(entry)
    return idx


def _hint(r: dict[str, Any], idx: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    k2, k1 = _shop_keys(r["merchant"])
    past = idx.get("2:" + k2) or idx.get("1:" + k1)
    if not past:
        return None
    recent = past[:6]
    last = recent[0]
    return {"split": last["split"], "month": last["month"],
            "same": sum(1 for x in recent if x["split"] == last["split"]), "count": len(recent)}


# ---------- calculation ----------

def parts(r: dict[str, Any]) -> tuple[int, int, int]:
    """(A, B, shared) for one row. Half splits truncate toward zero; the odd yen stays shared.
    An excluded row counts for nobody (it is still part of the bill: see summarize)."""
    if r.get("excluded"):
        return 0, 0, 0
    amount, split = int(r["amount"]), r.get("split", "shared")
    if split == "a":
        a, b = amount, 0
    elif split == "b":
        a, b = 0, amount
    elif split == "half":
        a = b = int(amount / 2)
    elif split == "custom":
        a, b = int(r.get("a") or 0), int(r.get("b") or 0)
    else:
        a = b = 0
    return a, b, amount - a - b


def summarize(st: dict[str, Any], required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`required` = who must approve before the month can be closed: [{"role", "name", "color"}, ...]."""
    order = {"a": 0, "b": 1}
    rows = []
    for r in st["rows"]:
        a, b, s = parts(r)
        rows.append(r | {"a": a, "b": b, "shared": s})
    # A's rows, then B's, then anyone else; newest first within each (as in the hand-made sheet)
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: (order.get(r.get("holder_key", ""), 2), _norm(r["holder"]) if r.get("holder_key") not in order else ""))
    used = [r for r in rows if not r.get("excluded")]
    total = {k: sum(r[k] for r in used) for k in ("amount", "a", "b", "shared")}
    excluded = {"count": len(rows) - len(used), "amount": sum(r["amount"] for r in rows if r.get("excluded"))}
    statement_total = total["amount"] + excluded["amount"]  # what the card company bills: every row
    balance = st.get("balance")
    billed = st.get("billed")
    unchecked = sum(1 for r in used if not r.get("checked"))
    over = sum(1 for r in rows if r["shared"] < 0)
    problems = []
    if unchecked:
        problems.append(f"未確認が {unchecked} 件あります")
    if billed is not None and billed != statement_total:
        problems.append(f"明細の合計 {statement_total:,} 円が請求額 {billed:,} 円と一致しません")
    if balance is None:
        problems.append("口座残高が未入力です")
    approvals = dict(st.get("approvals") or {})
    required = list(required or [])
    waiting = [u["name"] for u in required if u["role"] not in approvals]
    if waiting:
        problems.append("承認待ち: " + "・".join(waiting))
    return {
        "month": st["month"], "closed": bool(st.get("closed")), "closed_at": st.get("closed_at"),
        "billed": billed, "balance": balance, "source": st.get("source"),
        "rows": rows, "total": total, "excluded": excluded, "statement_total": statement_total,
        "deposit": {"a": total["a"], "b": total["b"],
                    "shared": None if balance is None else total["shared"] - int(balance)},
        "unchecked": unchecked, "negative_shared": over,
        "can_close": not problems, "problems": problems,
        "approvals": approvals, "approvers": required,
    }


# ---------- API ----------

def months() -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    folder = DIR / "statements"
    if not enabled() or not folder.exists():
        return {"config": public_config(), "months": out}
    for p in sorted(folder.glob("*.json"), reverse=True):
        try:
            s = summarize(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            continue
        out.append({k: s[k] for k in ("month", "closed", "billed", "balance", "total", "deposit", "unchecked",
                                       "statement_total", "excluded", "approvals")})
    return {"config": public_config(), "months": out}


def month_view(month: str, required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _require_enabled()
    view = summarize(_load(month), required)
    idx = _history(view["month"])
    for r in view["rows"]:
        r["hint"] = _hint(r, idx)
    return view | {"config": public_config()}


def _editable(month: str) -> dict[str, Any]:
    st = _load(month)
    if st.get("closed"):
        raise ValueError(f"{month} は確定済みです（変更するには確定を取り消してください）")
    return st


# Changes that alter who pays what: they withdraw earlier approvals (nobody approves what they have not seen).
_MONEY_KEYS = ("split", "a", "b", "excluded")


def update_rows(month: str, ids: list[str], change: dict[str, Any],
                required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Change split / custom amounts / checked / memo on one or more rows."""
    _require_enabled()
    ids = [str(i) for i in ids][:1000]
    if not ids:
        raise ValueError("行が指定されていません")
    with _lock:
        st = _editable(month)
        by_id = {r["id"]: r for r in st["rows"]}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError("見つからない行があります（再読み込みしてください）")
        for i in ids:
            r = by_id[i]
            if "split" in change:
                split = str(change["split"])
                if split not in SPLITS:
                    raise ValueError(f"振り分けが不正です: {split}")
                if split == "custom":  # start the hand-typed amounts from what the row shows now
                    r["a"], r["b"], _ = parts(r)
                r["split"] = split
            if ("a" in change or "b" in change) and r.get("split") != "custom":
                r["a"], r["b"], _ = parts(r)  # typing one side keeps the other as the row showed it
            for k in ("a", "b"):
                if k in change:
                    v = parse_amount(str(change[k])) if str(change[k]).strip() else 0
                    if v is None:
                        raise ValueError("金額は数字で入力してください")
                    r[k] = v
                    r["split"] = "custom"
            if "checked" in change:
                r["checked"] = bool(change["checked"])
            if "memo" in change:
                r["memo"] = str(change["memo"])[:500]
            if "excluded" in change:
                r["excluded"] = bool(change["excluded"])
        if any(k in change for k in _MONEY_KEYS):
            st["approvals"] = {}
        _save(st)
    return month_view(month, required)


def set_meta(month: str, balance: Any = ..., billed: Any = ...,
             required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _require_enabled()

    def num(v: Any) -> int | None:
        if v is None or str(v).strip() == "":
            return None
        n = parse_amount(str(v))
        if n is None:
            raise ValueError("金額は数字で入力してください")
        return n

    with _lock:
        st = _editable(month)
        if balance is not ...:
            st["balance"] = num(balance)
        if billed is not ...:
            st["billed"] = num(billed)
        _save(st)
    return month_view(month, required)


def approve(month: str, who: dict[str, Any], approved: bool = True, now: datetime | None = None,
            required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Record (or withdraw) one person's approval of the month: who = {"role", "name"}; keeps the time."""
    _require_enabled()
    role = str(who.get("role") or "")
    if not role:
        raise ValueError("誰の承認か分かりません")
    with _lock:
        st = _editable(month)
        approvals = dict(st.get("approvals") or {})
        if approved:
            approvals[role] = {"name": str(who.get("name") or role)[:40],
                               "at": (now or datetime.now()).isoformat(timespec="seconds")}
        else:
            approvals.pop(role, None)
        st["approvals"] = approvals
        _save(st)
    return month_view(month, required)


def export_csv(month: str) -> dict[str, Any]:
    """The month as a CSV laid out like the hand-made sheet (UTF-8 with BOM so Excel opens it)."""
    _require_enabled()
    s = summarize(_load(month))
    cfg = public_config()
    ma, mb = cfg["members"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["日付", "誰", "内容", "状態", "金額", f"{ma['label']}負担額", f"{mb['label']}負担額",
                f"{cfg['shared_label']}から負担額", "memo"])
    for r in s["rows"]:
        w.writerow([r["date"].replace("-", "/"), r["holder"], r["merchant"], "除外" if r.get("excluded") else "確認済" if r.get("checked") else "",
                    r["amount"], r["a"], r["b"], r["shared"], r.get("memo", "")])
    t, d = s["total"], s["deposit"]
    w.writerow([])
    w.writerow(["合計", "", "", "", t["amount"], t["a"], t["b"], t["shared"], ""])
    if s["excluded"]["count"]:
        w.writerow([f"除外した行（{s['excluded']['count']} 件・入金額に含めない）", "", "", "", s["excluded"]["amount"]])
    w.writerow([f"{cfg['account_label']}の残高", "", "", "", "" if s["balance"] is None else s["balance"]])
    w.writerow([f"{cfg['shared_label']}からの入金額", "", "", "", "" if d["shared"] is None else d["shared"]])
    return {"filename": f"credit-{month}.csv", "csv": "﻿" + buf.getvalue()}


def close(month: str, closed: bool, now: datetime | None = None,
          required: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        st = _load(month)
        if closed:
            s = summarize(st, required)
            if not s["can_close"]:
                raise ValueError("確定できません: " + " / ".join(s["problems"]))
            st["closed"], st["closed_at"] = True, (now or datetime.now()).isoformat(timespec="seconds")
        else:
            st["closed"], st["closed_at"] = False, None
        _save(st)
    if closed:  # keep a plain copy next to the data, readable even without Nakama
        out = DIR / "exports"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{month}.csv").write_text(export_csv(month)["csv"], encoding="utf-8")
    return month_view(month, required)


def delete_month(month: str) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        st = _load(month)
        if st.get("closed"):
            raise ValueError(f"{month} は確定済みです（削除するには確定を取り消してください）")
        _path(month).unlink()
    return {"deleted": month}
