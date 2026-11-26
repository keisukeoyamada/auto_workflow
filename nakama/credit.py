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
    "holder": ["ご利用者", "利用者", "カード会員", "カード会員名", "会員名", "名義", "使用者", "誰"],
}

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


def public_config() -> dict[str, Any]:
    """What the dashboard needs (no CSV match strings)."""
    cfg = config()
    return {
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


def parse_csv(text: str, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Statement rows: date, holder (raw), holder_key (a/b/''), merchant (as written), amount.

    Works with a holder column per row, or with section lines naming the card member
    (a line that matches a member and has no date/amount switches the current holder).
    """
    cfg = config() if cfg is None else cfg
    mems = members(cfg)
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
        out.append({"date": day, "holder": holder, "holder_key": _holder_key(holder, mems) or current[1],
                    "merchant": get("merchant"), "amount": amount})
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
    key = f"{r['date']}|{_norm(r['holder'])}|{_norm(r['merchant'])}|{r['amount']}|{n}"
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
    parsed = _with_ids(parse_csv(decode(raw), cfg))
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
            if prev:
                kept += 1
                rows.append(prev)
            else:
                rows.append(r | {"split": "shared", "a": 0, "b": 0, "checked": False, "memo": ""})
        st["rows"] = rows
        st["source"] = {"file": Path(filename).name[:120], "imported_at": now.isoformat(timespec="seconds")}
        raw_dir = DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"{month}-{now:%Y%m%d%H%M%S}.csv").write_bytes(raw)  # original kept for re-import
        _save(st)
    dropped = len(old) - kept
    return {"month": month, "rows": len(rows), "added": len(rows) - kept, "kept": kept, "dropped": dropped}


# ---------- calculation ----------

def parts(r: dict[str, Any]) -> tuple[int, int, int]:
    """(A, B, shared) for one row. Half splits truncate toward zero; the odd yen stays shared."""
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


def summarize(st: dict[str, Any]) -> dict[str, Any]:
    order = {"a": 0, "b": 1}
    rows = []
    for r in st["rows"]:
        a, b, s = parts(r)
        rows.append(r | {"a": a, "b": b, "shared": s})
    # A's rows, then B's, then anyone else; newest first within each (as in the hand-made sheet)
    rows.sort(key=lambda r: r["date"], reverse=True)
    rows.sort(key=lambda r: (order.get(r.get("holder_key", ""), 2), _norm(r["holder"]) if r.get("holder_key") not in order else ""))
    total = {k: sum(r[k] for r in rows) for k in ("amount", "a", "b", "shared")}
    balance = st.get("balance")
    billed = st.get("billed")
    unchecked = sum(1 for r in rows if not r.get("checked"))
    over = sum(1 for r in rows if r["shared"] < 0)
    problems = []
    if unchecked:
        problems.append(f"未確認が {unchecked} 件あります")
    if billed is not None and billed != total["amount"]:
        problems.append(f"明細の合計 {total['amount']:,} 円が請求額 {billed:,} 円と一致しません")
    if balance is None:
        problems.append("口座残高が未入力です")
    return {
        "month": st["month"], "closed": bool(st.get("closed")), "closed_at": st.get("closed_at"),
        "billed": billed, "balance": balance, "source": st.get("source"),
        "rows": rows, "total": total,
        "deposit": {"a": total["a"], "b": total["b"],
                    "shared": None if balance is None else total["shared"] - int(balance)},
        "unchecked": unchecked, "negative_shared": over,
        "can_close": not problems, "problems": problems,
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
        out.append({k: s[k] for k in ("month", "closed", "billed", "total", "deposit", "unchecked")})
    return {"config": public_config(), "months": out}


def month_view(month: str) -> dict[str, Any]:
    _require_enabled()
    return summarize(_load(month)) | {"config": public_config()}


def _editable(month: str) -> dict[str, Any]:
    st = _load(month)
    if st.get("closed"):
        raise ValueError(f"{month} は確定済みです（変更するには確定を取り消してください）")
    return st


def update_rows(month: str, ids: list[str], change: dict[str, Any]) -> dict[str, Any]:
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
        _save(st)
    return month_view(month)


def set_meta(month: str, balance: Any = ..., billed: Any = ...) -> dict[str, Any]:
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
    return month_view(month)


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
        w.writerow([r["date"].replace("-", "/"), r["holder"], r["merchant"], "確認済" if r.get("checked") else "",
                    r["amount"], r["a"], r["b"], r["shared"], r.get("memo", "")])
    t, d = s["total"], s["deposit"]
    w.writerow([])
    w.writerow(["合計", "", "", "", t["amount"], t["a"], t["b"], t["shared"], ""])
    w.writerow([f"{cfg['account_label']}の残高", "", "", "", "" if s["balance"] is None else s["balance"]])
    w.writerow([f"{cfg['shared_label']}からの入金額", "", "", "", "" if d["shared"] is None else d["shared"]])
    return {"filename": f"credit-{month}.csv", "csv": "﻿" + buf.getvalue()}


def close(month: str, closed: bool, now: datetime | None = None) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        st = _load(month)
        if closed:
            s = summarize(st)
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
    return month_view(month)


def delete_month(month: str) -> dict[str, Any]:
    _require_enabled()
    with _lock:
        st = _load(month)
        if st.get("closed"):
            raise ValueError(f"{month} は確定済みです（削除するには確定を取り消してください）")
        _path(month).unlink()
    return {"deleted": month}
