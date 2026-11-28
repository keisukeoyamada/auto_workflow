"""WBS spreadsheets (Excel) found in your folders: read, show in the dashboard, update with a change log.

Detection: files named *WBS*.xlsx|xlsm under the configured roots (vault root + project folders by default),
skipping backups/archives (bak, bkup, backup, archive, ~$). A sheet counts as a WBS when a header row has a
WBS-number column and a task column; other columns are matched by name (担当, 開始, 終了, 状態, 実績開始 ...).

Edits (状態 / 開始 / 終了 / 実績開始 / 実績完了) made in the dashboard are first staged in
workspace/wbs/pending.json (+ workspace/wbs/changes.md, a readable log) and shown at once. flush() — the
"Excel に転記" button or the scheduled sync (fleet.yaml wbs.sync_minutes, default 60) — then patches only the target
cell in the .xlsx XML, keeps a backup in logs/wbs-backup/ and appends a row to the 変更履歴 sheet
(日付 | WBS | セル | 修正前 | 修正後 | 理由) when the workbook has one. Files open in Excel are skipped.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import LOGS_DIR, load_fleet

SKIP_NAME = re.compile(r"(bak|bkup|backup|archive|~\$)", re.I)
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "_backup", "backup"}
BACKUP_DIR = LOGS_DIR / "wbs-backup"
COLS = {  # field -> header keywords (first match wins, normalised: no spaces / newlines)
    "no": ("WBS", "No", "番号"), "title": ("タスク", "作業", "項目"), "owner": ("担当",), "effort": ("工数",),
    "days": ("期間",), "pred": ("先行",), "start": ("開始",), "end": ("終了", "期限"), "deliverable": ("完了条件", "成果物"),
    "related": ("関連",), "status": ("状態", "ステータス", "進捗"), "actual_start": ("実績開始",), "actual_end": ("実績完了",),
}
EDITABLE = ("status", "start", "end", "actual_start", "actual_end")
DONE_WORDS = ("完了", "done", "済", "取り下げ", "取下げ", "中止")  # closed: no work left
WBS_NO = re.compile(r"[A-Za-z]{0,3}\d+(\.\d+)*")  # 1 / 1.2.3 / M2
_cache: dict[str, tuple[float, dict]] = {}


class WbsError(Exception):
    pass


def _n(v: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(v or "")))


def roots() -> list[Path]:
    from . import projects, vault

    cfg = getattr(load_fleet(), "wbs", None) or {}
    out = [Path(r) for r in cfg.get("roots") or []]
    if not out:
        if vault.root():
            out.append(vault.root())
        out += projects.read_roots()
    return [r for r in out if r.is_dir()]


def owner_aliases() -> list[str]:
    cfg = getattr(load_fleet(), "wbs", None) or {}
    names = list(cfg.get("owner_aliases") or ["自分"])
    me = (load_fleet().owner or {}).get("name")
    if me and me != "Your Name":
        names.append(me)
    return names


def file_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).lower().encode("utf-8")).hexdigest()[:12]


_discover_cache: dict[str, Any] = {}
DISCOVER_TTL = 600  # q118: walking the vault + project folders takes seconds; WBS files rarely appear or move


def discover(limit: int = 200, refresh: bool = False) -> list[Path]:
    """WBS-like Excel files under the roots (cached for DISCOVER_TTL seconds, keyed by the root list)."""
    import time

    key = repr([str(r) for r in roots()]) + str(limit)
    hit = _discover_cache.get(key)
    if not refresh and hit and time.time() - hit[0] < DISCOVER_TTL and all(p.exists() for p in hit[1]):
        return list(hit[1])
    found = _discover(limit)
    _discover_cache.clear()
    _discover_cache[key] = (time.time(), found)
    return list(found)


def _discover(limit: int) -> list[Path]:
    from . import vault

    _, locked = vault.claude_roots()  # hidden/locked vault folders stay out of the WBS list too
    found: list[Path] = []
    for root in roots():
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                           and not any(here / d == l or l in (here / d).parents for l in locked)]
            for fn in filenames:
                if fn.lower().endswith((".xlsx", ".xlsm")) and "wbs" in fn.lower() and not SKIP_NAME.search(fn):
                    found.append(here / fn)
                    if len(found) >= limit:
                        return found
    return sorted(set(found))


def _header(ws) -> tuple[int, dict[str, int]] | None:
    for r_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
        names = [_n(v) for v in row]
        cols: dict[str, int] = {}
        for field, keys in COLS.items():
            for c, name in enumerate(names):
                if name and c not in cols.values() and any(name.startswith(k) for k in keys):
                    # actual_* must not be taken by start/end
                    if field in ("start", "end") and name.startswith("実績"):
                        continue
                    cols[field] = c
                    break
        if "no" in cols and "title" in cols:
            return r_idx, cols
    return None


def _cell_date(v: Any) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def _load(path: Path, read_only: bool = True):
    import openpyxl

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        wb = openpyxl.load_workbook(path, read_only=read_only, data_only=True)
    lossy = [str(x.message) for x in w if "will be removed" in str(x.message) or "not supported" in str(x.message)]
    return wb, lossy


def _deadline(ws, header_row: int) -> str:
    """A '期限' label above the header with a date next to it (e.g. row 2: 期限 | 2027-01-12)."""
    for row in ws.iter_rows(min_row=1, max_row=max(1, header_row - 1), values_only=True):
        for i, v in enumerate(row[:-1]):
            if _n(v).startswith(("期限", "締切", "納期")):
                d = next((_cell_date(x) for x in row[i + 1:i + 4] if _cell_date(x)), "")
                if d:
                    return d
    return ""


def _float(v: str) -> float:
    try:
        return float(unicodedata.normalize("NFKC", v).strip() or 0)
    except ValueError:
        return 0.0


def _schedule(rows: list[dict[str, Any]], deadline: str) -> dict[str, Any]:
    """Progress numbers and bar positions for the chart. Missing end dates are estimated from
    start (or actual start) + 期間（営業日）, and flagged as estimates."""
    from datetime import date as _date

    from .bizdays import business_days_of_month, is_business_day

    def add_bd(start: _date, n: int) -> _date:
        d, left = start, max(1, n) - 1
        while left > 0:
            d = _date.fromordinal(d.toordinal() + 1)
            if is_business_day(d):
                left -= 1
        return d

    def next_bd(d: _date) -> _date:
        d = _date.fromordinal(d.toordinal() + 1)
        while not is_business_day(d):
            d = _date.fromordinal(d.toordinal() + 1)
        return d

    leaves = [r for r in rows if not r["parent"]]
    today = _date.today()
    ends: dict[str, str] = {}
    bars = []
    prev_end = ""
    for r in leaves:
        start = r["actual_start"] or r["start"]
        end = r["actual_end"] if r["done"] and r["actual_end"] else r["end"]
        estimated = False
        if not start:  # like the sheet's formulas: day after the predecessors (or the previous task) end
            preds = [p.strip() for p in re.split(r"[,、\s]+", r["pred"]) if p.strip() and p.strip() not in ("-", "－", "ー")]
            known = [ends[p] for p in preds if ends.get(p)]
            base = max(known) if known else (prev_end if not preds or not known else "")
            if base:
                start, estimated = next_bd(_date.fromisoformat(base)).isoformat(), True
                if not r["done"] and start < today.isoformat():  # unfinished work cannot start in the past
                    start = (today if is_business_day(today) else next_bd(today)).isoformat()
        if start and not end:
            try:
                end = add_bd(_date.fromisoformat(start), int(_float(r["days"]) or 1)).isoformat()
                estimated = True
            except ValueError:
                end = ""
        if end:
            ends[r["no"]] = end
            prev_end = max(prev_end, end)
        state = "done" if r["done"] else ("doing" if any(w in r["status"] for w in ("進行", "作業中", "対応中")) else
                                          ("hold" if "保留" in r["status"] else "todo"))
        bars.append({"row": r["row"], "no": r["no"], "group": r["no"].split(".")[0] if r["no"] else "", "title": r["title"],
                     "owner": r["owner"], "start": start, "end": end, "estimated": estimated, "state": state,
                     "late": bool(not r["done"] and not estimated and end and end < today.isoformat())})
    remaining = round(sum(_float(r["effort"]) for r in leaves if not r["done"]), 2)
    left_bd = 0
    if deadline:
        d = today
        while d.isoformat() <= deadline:
            left_bd += 1 if is_business_day(d) else 0
            d = _date.fromordinal(d.toordinal() + 1)
    groups = {r["no"]: r["title"] for r in rows if r["parent"] and "." not in r["no"]}
    counts = {k: sum(1 for b in bars if b["state"] == k) for k in ("done", "doing", "hold", "todo")}
    finish = max((b["end"] for b in bars if b["end"]), default="")
    return {"bars": bars, "groups": groups, "counts": counts, "total": len(leaves), "remaining_effort": remaining, "finish": finish,
            "deadline": deadline, "left_bd": left_bd, "slack": round(left_bd - remaining, 2) if deadline else None,
            "undated": sum(1 for b in bars if not b["start"]), "late": sum(1 for b in bars if b["late"])}


def parse(path: Path) -> dict[str, Any]:
    path = Path(path)
    key = str(path.resolve())
    mtime = path.stat().st_mtime
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        wb, _ = _load(path)
    except ImportError as exc:
        raise WbsError("openpyxl が必要です（pip install openpyxl）") from exc
    except Exception as exc:  # noqa: BLE001 - corrupt / locked / password-protected files
        raise WbsError(f"読めません: {exc}") from exc
    sheets, changelog = [], []
    for ws in wb.worksheets:
        if _n(ws.title) in ("変更履歴", "更新履歴"):
            for row in ws.iter_rows(min_row=2, max_row=2000, values_only=True):
                if any(v not in (None, "") for v in row[:6]):
                    changelog.append([_cell_date(row[0]) or str(row[0] or "")] + [str(v or "") for v in row[1:6]])
            continue
        head = _header(ws)
        if not head:
            continue
        hrow, cols = head
        rows, notes = [], []
        for r_idx, row in enumerate(ws.iter_rows(min_row=hrow + 1, max_row=hrow + 2000, values_only=True), start=hrow + 1):
            get = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else None
            no, title = str(get("no") or "").strip(), str(get("title") or "").strip()
            if not no and not title:
                continue
            if no and not WBS_NO.fullmatch(_n(no)):
                # not a WBS number: holiday lists (dates) are skipped, legends / forecast memos are kept as notes
                if not re.match(r"\d{4}-\d{2}-\d{2}", no) and len(no) > 12:
                    notes.append({"row": r_idx, "text": no + (f"\n{title}" if title else "")})
                continue
            item = {"row": r_idx, "no": no, "level": no.count(".") + 1 if no else 1, "title": title}
            for f in ("owner", "effort", "days", "pred", "deliverable", "related", "status"):
                item[f] = str(get(f) or "").strip()
            for f in ("start", "end", "actual_start", "actual_end"):
                item[f] = _cell_date(get(f))
            item["done"] = any(w in item["status"] for w in DONE_WORDS)
            rows.append(item)
        parents = {r["no"] for r in rows if any(x["no"].startswith(r["no"] + ".") for x in rows if r["no"])}
        for r in rows:
            r["parent"] = r["no"] in parents
        deadline = _deadline(ws, hrow)
        sheets.append({"name": ws.title, "header_row": hrow, "columns": cols, "rows": rows, "deadline": deadline,
                       "schedule": _schedule(rows, deadline), "notes": notes})
    wb.close()
    data = {"path": str(path), "id": file_id(path), "name": path.name, "mtime": mtime, "sheets": sheets,
            "changelog": changelog[-200:], "is_wbs": bool(sheets)}
    _cache[key] = (mtime, data)
    return data


def summary(data: dict[str, Any], today: date) -> dict[str, Any]:
    leaves = [r for s in data["sheets"] for r in s["rows"] if not r["parent"]]
    overdue = [r for r in leaves if not r["done"] and r["end"] and r["end"] < today.isoformat()]
    return {"total": len(leaves), "done": sum(r["done"] for r in leaves), "overdue": len(overdue),
            "ends": max((r["end"] for r in leaves if r["end"]), default="")}


def index(today: date) -> list[dict[str, Any]]:
    out = []
    for p in discover():
        try:
            d = view(p)
        except WbsError as exc:
            out.append({"id": file_id(p), "name": p.name, "path": str(p), "is_wbs": False, "error": str(exc)})
            continue
        out.append({"id": d["id"], "name": d["name"], "path": d["path"], "is_wbs": d["is_wbs"],
                    "sheets": [s["name"] for s in d["sheets"]], **(summary(d, today) if d["is_wbs"] else {})})
    return out


def by_id(fid: str) -> Path:
    for p in discover():
        if file_id(p) == fid:
            return p
    raise WbsError("WBS が見つかりません（場所が変わった可能性）")


def my_items(today: date) -> list[dict[str, Any]]:
    """Open leaf rows assigned to me (owner contains an alias), for the unified task list."""
    names = owner_aliases()
    out = []
    for f in index(today):
        if not f.get("is_wbs"):
            continue
        d = view(Path(f["path"]))
        for s in d["sheets"]:
            for r in s["rows"]:
                if r["parent"] or not any(n and n in r["owner"] for n in names):
                    continue
                out.append({"file": d["id"], "file_name": d["name"], "sheet": s["name"], **r})
    return out


# ---------- write-back: patch only the target cell (and one change-log row) inside the .xlsx ----------
# Saving with openpyxl would drop every formula's cached value (and images/charts), so instead we edit the
# sheet XML in place: one <c> element, one appended <row> in 変更履歴, and fullCalcOnLoad so Excel recalculates.


def _col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _sheet_paths(z) -> dict[str, str]:
    """sheet name -> zip member path of its XML."""
    wb_xml = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {}
    for m in re.finditer(r"<Relationship\b[^>]*>", rels):
        tag = m.group(0)
        rid = re.search(r'\bId="([^"]+)"', tag)
        tgt = re.search(r'\bTarget="([^"]+)"', tag)
        if rid and tgt:
            targets[rid.group(1)] = tgt.group(1)
    out = {}
    for m in re.finditer(r"<sheet\b[^>]*>", wb_xml):
        tag = m.group(0)
        name = re.search(r'\bname="([^"]+)"', tag)
        rid = re.search(r'\br:id="([^"]+)"', tag)
        if name and rid:
            t = targets.get(rid.group(1), "")
            n = name.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            out[n] = t.lstrip("/") if t.startswith("/") else "xl/" + t
    return out


def _find_cell(xml: str, ref: str):
    return re.search(r'<c\b[^>]*\br="' + ref + r'"[^>]*?(?:/>|>.*?</c>)', xml, re.S)


def _set_cell(xml: str, ref: str, new_cell: str, allow_formula: bool = False) -> tuple[str, str]:
    """Replace (or insert) cell `ref` in sheet XML. Returns (new_xml, old_cell_xml).
    allow_formula: replace a plain formula with the value (planned dates set by hand); shared-formula
    anchors are always refused because other cells borrow their formula text."""
    row_no = int(re.sub(r"[A-Z]+", "", ref))
    row_m = re.search(r'<row\b[^>]*\br="' + str(row_no) + r'"[^>]*?(?:/>|>(.*?)</row>)', xml, re.S)
    if not row_m:
        raise WbsError(f"行 {row_no} が見つかりません")
    row_xml = row_m.group(0)
    cell_m = _find_cell(row_xml, ref)
    if cell_m:
        old = cell_m.group(0)
        if "<f" in old:
            if not allow_formula:
                raise WbsError("このセルは数式です。ダッシュボードからは変更しません（Excel で直接編集してください）")
            if re.search(r'<f\b[^>]*\bt="shared"[^>]*\bref=', old):
                raise WbsError("このセルは共有数式の起点です。Excel で直接編集してください")
        new_row = row_xml[:cell_m.start()] + new_cell + row_xml[cell_m.end():]
    else:
        old = ""
        if row_xml.endswith("/>"):
            new_row = row_xml[:-2] + ">" + new_cell + "</row>"
        else:
            col = _col_index(re.sub(r"\d", "", ref))
            cells = list(re.finditer(r'<c\b[^>]*\br="([A-Z]+)\d+"', row_xml))
            after = next((c for c in cells if _col_index(c.group(1)) > col), None)
            pos = after.start() if after else row_xml.rfind("</row>")
            new_row = row_xml[:pos] + new_cell + row_xml[pos:]
    return xml[:row_m.start()] + new_row + xml[row_m.end():], old


def _style_attr(old_cell: str) -> str:
    m = re.search(r'\bs="(\d+)"', old_cell)
    return f' s="{m.group(1)}"' if m else ""


def _append_log_row(xml: str, values: list[str]) -> str:
    rows = [int(m.group(1)) for m in re.finditer(r'<row\b[^>]*\br="(\d+)"', xml)]
    n = (max(rows) if rows else 0) + 1
    cells = "".join(f'<c r="{_col_letter(i + 1)}{n}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(v)}</t></is></c>'
                    for i, v in enumerate(values))
    new_row = f'<row r="{n}">{cells}</row>'
    if "<sheetData/>" in xml:
        xml = xml.replace("<sheetData/>", f"<sheetData>{new_row}</sheetData>", 1)
    else:
        xml = xml.replace("</sheetData>", new_row + "</sheetData>", 1)
    return re.sub(r'(<dimension ref="[A-Z]+\d+:[A-Z]+)\d+(")', lambda m: f"{m.group(1)}{n}{m.group(2)}", xml, count=1)


def _full_calc_on_load(wb_xml: str) -> str:
    if "fullCalcOnLoad" in wb_xml or "<calcPr" not in wb_xml:
        return wb_xml  # no calcPr element: leave the workbook part untouched (schema order is strict)
    return re.sub(r"<calcPr\b", '<calcPr fullCalcOnLoad="1"', wb_xml, count=1)


def update(fid: str, sheet: str, row: int, field: str, value: str, reason: str, expected_no: str = "",
           overwrite_formula: bool = False) -> dict[str, Any]:
    """Write one value back to the Excel file, keeping everything else as it was."""
    import zipfile

    if field not in EDITABLE:
        raise WbsError(f"変更できる項目は {EDITABLE}")
    if not reason.strip():
        raise WbsError("理由を入力してください（変更履歴に残します）")
    path = by_id(fid)
    data = parse(path)
    sh = next((s for s in data["sheets"] if s["name"] == sheet), None)
    if sh is None or field not in sh["columns"]:
        raise WbsError("この WBS にはその列がありません")
    item = next((r for r in sh["rows"] if r["row"] == row), None)
    if item is None or row <= sh["header_row"]:
        raise WbsError("その行は WBS のデータ行ではありません")
    if expected_no and item["no"] != expected_no:
        raise WbsError(f"Excel の行がずれています（{row} 行目は今 {item['no'] or '空'}）。画面を再読み込みしてください")
    d = datetime.strptime(value, "%Y-%m-%d") if (field != "status" and value) else None
    ref = f"{_col_letter(sh['columns'][field] + 1)}{row}"
    with zipfile.ZipFile(path) as z:
        members = {i.filename: (i, z.read(i.filename)) for i in z.infolist()}
        sheets = _sheet_paths(z)
    if sheet not in sheets or sheets[sheet] not in members:
        raise WbsError("シートが見つかりません")
    xml = members[sheets[sheet]][1].decode("utf-8")
    old_m = _find_cell(xml, ref)
    style = _style_attr(old_m.group(0)) if old_m else ""
    if not value:
        new_cell = f'<c r="{ref}"{style}/>'
    elif field == "status":
        new_cell = f'<c r="{ref}"{style} t="inlineStr"><is><t>{_xml_escape(value)}</t></is></c>'
    else:
        new_cell = f'<c r="{ref}"{style}><v>{(d - datetime(1899, 12, 30)).days}</v></c>'  # Excel date serial
    xml, _old = _set_cell(xml, ref, new_cell, allow_formula=overwrite_formula and field in ("start", "end"))
    replaced = {sheets[sheet]: xml.encode("utf-8")}
    log_name = next((n for n in sheets if _n(n) in ("変更履歴", "更新履歴")), None)
    before = item.get(field, "")
    if log_name and sheets[log_name] in members:
        log_xml = members[sheets[log_name]][1].decode("utf-8")
        replaced[sheets[log_name]] = _append_log_row(
            log_xml, [date.today().isoformat(), item["no"], ref, str(before), value, reason.strip()[:500] + "（Nakama）"]
        ).encode("utf-8")
    replaced["xl/workbook.xml"] = _full_calc_on_load(members["xl/workbook.xml"][1].decode("utf-8")).encode("utf-8")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP_DIR / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S-%f}{path.suffix}")
    tmp = path.with_name(path.name + ".nakama.part")  # not *.xlsx: never picked up by discover()
    try:
        with zipfile.ZipFile(tmp, "w") as out:
            for name, (info, blob) in members.items():
                out.writestr(info, replaced.get(name, blob), compress_type=info.compress_type)
        with zipfile.ZipFile(tmp) as check:  # the result must still be a readable package
            if check.testzip() is not None:
                raise WbsError("書き込み結果の検証に失敗しました（元のファイルは変更していません）")
        os.replace(tmp, path)
    except PermissionError as exc:
        raise WbsError("Excel で開いているため保存できません。閉じてからもう一度どうぞ") from exc
    finally:
        tmp.unlink(missing_ok=True)
    _cache.pop(str(path.resolve()), None)
    return {"cell": ref, "before": str(before), "after": value, "logged": bool(log_name)}



# ---------- staged edits: GUI changes live in JSON (+ a readable MD log) until they are copied into Excel ----------
# Excel is often open (or synced) while you edit, so a change is first saved to workspace/wbs/pending.json and
# shown on screen at once. "Excel に転記" (or the scheduled sync) then writes each one with update() above.
import json as _json
import threading as _threading

from .config import WORKSPACE_DIR

STAGE_DIR = WORKSPACE_DIR / "wbs"
PENDING_FILE = STAGE_DIR / "pending.json"
CHANGES_MD = STAGE_DIR / "changes.md"
LABELS = {"status": "状態", "start": "開始", "end": "終了", "actual_start": "実績開始", "actual_end": "実績完了"}
_stage_lock = _threading.Lock()


def load_pending() -> list[dict[str, Any]]:
    try:
        data = _json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_pending(items: list[dict[str, Any]]) -> None:
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(_json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PENDING_FILE)


def _md_log(cells: list[Any]) -> None:
    """Append one row to workspace/wbs/changes.md (a table you can open in Obsidian)."""
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    new = not CHANGES_MD.exists()
    with CHANGES_MD.open("a", encoding="utf-8") as f:
        if new:
            f.write("# WBS の変更ログ（ダッシュボード）\n\n| 日時 | 操作 | ファイル | WBS | 項目 | 修正前 | 修正後 | 理由・結果 |\n"
                    "|---|---|---|---|---|---|---|---|\n")
        f.write("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |\n")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stage(fid: str, sheet: str, row: int, field: str, value: str, reason: str = "", expected_no: str = "") -> dict[str, Any]:
    """Save one edit to pending.json (validated like update()); a later edit of the same cell replaces it."""
    if field not in EDITABLE:
        raise WbsError(f"変更できる項目は {EDITABLE}")
    if field != "status" and value:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise WbsError("日付は YYYY-MM-DD で") from exc
    path = by_id(fid)
    data = parse(path)
    sh = next((s for s in data["sheets"] if s["name"] == sheet), None)
    if sh is None or field not in sh["columns"]:
        raise WbsError("この WBS にはその列がありません")
    item = next((r for r in sh["rows"] if r["row"] == row), None)
    if item is None or item["parent"]:
        raise WbsError("その行は WBS の作業行ではありません")
    if expected_no and item["no"] != expected_no:
        raise WbsError(f"Excel の行がずれています（{row} 行目は今 {item['no'] or '空'}）。画面を再読み込みしてください")
    reason = (reason or "").strip()[:500] or "ダッシュボードで更新"
    now = _now()
    with _stage_lock:
        items = [p for p in load_pending()
                 if not (p["file"] == fid and p["sheet"] == sheet and p["row"] == row and p["field"] == field)]
        if value == item.get(field, ""):  # back to what Excel already says: nothing left to copy
            _save_pending(items)
            return {"pending": len(items), "cancelled": True}
        items.append({"id": hashlib.sha1(f"{fid}|{sheet}|{row}|{field}|{now}".encode()).hexdigest()[:10], "file": fid,
                      "file_name": data["name"], "sheet": sheet, "row": row, "no": item["no"], "title": item["title"],
                      "field": field, "before": item.get(field, ""), "value": value, "reason": reason, "at": now,
                      "force": field in ("start", "end")})  # planned dates may replace a formula (set by hand)
        _save_pending(items)
    _md_log([now, "変更", data["name"], f"{item['no']} {item['title'][:30]}", LABELS[field], item.get(field, ""), value, reason])
    return {"pending": len(items)}


def discard(pid: str) -> dict[str, Any]:
    with _stage_lock:
        items = load_pending()
        hit = next((p for p in items if p["id"] == pid), None)
        if hit is None:
            raise WbsError("その未転記の変更はもうありません")
        items = [p for p in items if p["id"] != pid]
        _save_pending(items)
    _md_log([_now(), "取り消し", hit["file_name"], hit["no"], LABELS[hit["field"]], hit["before"], hit["value"], "転記せずに取り消し"])
    return {"pending": len(items)}


def excel_open(path: Path) -> bool:
    """Excel keeps a ~$ lock file next to a workbook while it is open (long names lose their first 2 chars)."""
    return any((path.parent / n).exists() for n in ("~$" + path.name, "~$" + path.name[2:]))


def flush(fid: str = "") -> dict[str, Any]:
    """Copy pending edits into Excel (all files, or one). Files open in Excel are skipped and stay pending."""
    written, skipped, errors, keep = 0, set(), [], []
    with _stage_lock:
        for p in load_pending():
            if fid and p["file"] != fid:
                keep.append(p)
                continue
            try:
                path = by_id(p["file"])
                if excel_open(path):
                    skipped.add(p["file_name"])
                    keep.append(p)
                    continue
                r = update(p["file"], p["sheet"], p["row"], p["field"], p["value"], p["reason"], expected_no=p["no"],
                           overwrite_formula=bool(p.get("force")))
            except (WbsError, OSError) as exc:
                errors.append({"no": p["no"], "file_name": p["file_name"], "error": str(exc)})
                keep.append({**p, "error": str(exc)})
                continue
            written += 1
            _md_log([_now(), "転記", p["file_name"], p["no"], LABELS[p["field"]], r["before"], p["value"], f"{r['cell']} に書き込み"])
        _save_pending(keep)
    return {"written": written, "skipped_open": sorted(skipped), "errors": errors, "pending": len(keep)}


def view(path: Path) -> dict[str, Any]:
    """parse() with pending edits applied, so the screen and the task list show them before they reach Excel."""
    import copy

    data = parse(path)
    mine = [p for p in load_pending() if p["file"] == data["id"]]
    if not mine:
        return {**data, "pending": []}
    data = copy.deepcopy(data)
    for sh in data["sheets"]:
        changed = False
        for p in (x for x in mine if x["sheet"] == sh["name"]):
            r = next((x for x in sh["rows"] if x["row"] == p["row"]), None)
            if r is not None:
                r[p["field"]] = p["value"]
                r.setdefault("pending", []).append(p["field"])
                r["done"] = any(w in r["status"] for w in DONE_WORDS)
                changed = True
        if changed:
            sh["schedule"] = _schedule(sh["rows"], sh["deadline"])
    data["pending"] = mine
    return data


def overview(today: date) -> dict[str, Any]:
    """All WBS files in one place: per-file progress, a merged list of what comes next, overdue, recent changes."""
    files, nxt, overdue, changes = [], [], [], []
    names = owner_aliases()
    for f in index(today):
        if not f.get("is_wbs"):
            continue
        d = view(Path(f["path"]))
        for sh in d["sheets"]:
            sc = sh["schedule"]
            files.append({"id": d["id"], "name": d["name"], "sheet": sh["name"], "total": sc["total"], **sc["counts"],
                          "remaining_effort": sc["remaining_effort"], "deadline": sc["deadline"], "finish": sc["finish"],
                          "left_bd": sc["left_bd"], "slack": sc["slack"], "late": sc["late"],
                          "bars": [{**b, "group": d["name"]} for b in sc["bars"]]})  # one chart lane per file
            bars = {b["no"]: b for b in sc["bars"]}
            for r in sh["rows"]:
                if r["parent"] or r["done"]:
                    continue
                b = bars.get(r["no"], {})
                item = {"file": d["id"], "file_name": d["name"], "sheet": sh["name"], "row": r["row"], "no": r["no"],
                        "title": r["title"], "owner": r["owner"], "status": r["status"],
                        "start": r["start"] or b.get("start", ""), "end": r["end"] or b.get("end", ""),
                        "estimated": not r["end"], "mine": any(n and n in r["owner"] for n in names)}
                (overdue if r["end"] and r["end"] < today.isoformat() else nxt).append(item)
        changes += [{"file_name": d["name"], "date": c[0], "no": c[1], "before": c[3], "after": c[4], "reason": c[5]}
                    for c in d["changelog"] if len(c) >= 6]
    nxt.sort(key=lambda r: (r["start"] or "9999", r["end"] or "9999"))
    changes.sort(key=lambda c: c["date"], reverse=True)
    total = lambda k: round(sum(f[k] or 0 for f in files), 2)  # noqa: E731
    return {"files": files, "next": nxt[:15], "overdue": overdue, "changes": changes[:15], "pending": load_pending(),
            "total": total("total"), "done": total("done"), "doing": total("doing"), "remaining_effort": total("remaining_effort"),
            "deadline": min((f["deadline"] for f in files if f["deadline"]), default=""),
            "over": [f["name"] for f in files if f["deadline"] and f["finish"] and f["finish"] > f["deadline"]],
            "sync_minutes": sync_minutes()}


def sync_minutes() -> int:
    """fleet.yaml wbs.sync_minutes: how often pending edits are copied into Excel (0 = only by the button)."""
    cfg = getattr(load_fleet(), "wbs", None) or {}
    try:
        return max(0, int(cfg.get("sync_minutes", 60)))
    except (TypeError, ValueError):
        return 60
