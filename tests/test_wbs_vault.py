"""Tests for WBS detection/write-back, vault visibility/password, project folders and the chat read scope."""
from datetime import date, datetime

import pytest

openpyxl = pytest.importorskip("openpyxl")

from nakama import chat, projects, vault, wbs  # noqa: E402


def _make_wbs(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WBS"
    ws.append(["WBS: sample"])
    ws.append([])
    ws.append(["WBS", "タスク", "担当", "開始", "終了", "状態", "実績開始", "実績完了"])
    ws.append(["1", "準備"])
    ws.append(["1.1", "調査", "自分", datetime(2026, 11, 2), datetime(2026, 11, 5), "進行中"])
    ws.append(["1.2", "方針", "他の人", None, datetime(2026, 11, 9), "完了"])
    log = wb.create_sheet("変更履歴")
    log.append(["日付", "WBS", "セル", "修正前", "修正後", "理由"])
    wb.save(path)


def test_wbs_detect_parse_and_write_back(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    f = tmp_path / "proj" / "sample_WBS.xlsx"
    _make_wbs(f)
    _make_wbs(tmp_path / "proj" / "sample_WBS_bkup.xlsx")             # backups are skipped
    monkeypatch.setattr(wbs, "roots", lambda: [tmp_path])
    monkeypatch.setattr(wbs, "owner_aliases", lambda: ["自分"])
    monkeypatch.setattr(wbs, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(vault, "claude_roots", lambda: ([], []))
    assert [p.name for p in wbs.discover()] == ["sample_WBS.xlsx"]
    d = wbs.parse(f)
    rows = d["sheets"][0]["rows"]
    assert [r["no"] for r in rows] == ["1", "1.1", "1.2"] and rows[0]["parent"] and rows[2]["done"]
    assert rows[1]["end"] == "2026-11-05"
    mine = wbs.my_items(date(2026, 11, 26))
    assert [m["no"] for m in mine] == ["1.1"]
    with pytest.raises(wbs.WbsError):
        wbs.update(d["id"], "WBS", rows[1]["row"], "status", "完了", "")     # a reason is required
    r = wbs.update(d["id"], "WBS", rows[1]["row"], "status", "完了", "テスト完了")
    assert r["logged"] and list((tmp_path / "bk").glob("*.xlsx"))
    wb = openpyxl.load_workbook(f)
    assert wb["WBS"].cell(row=rows[1]["row"], column=6).value == "完了"
    assert wb["変更履歴"].cell(row=2, column=2).value == "1.1" and "テスト完了" in wb["変更履歴"].cell(row=2, column=6).value



def test_wbs_staged_edits_then_flush(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    f = tmp_path / "proj" / "x_WBS.xlsx"
    _make_wbs(f)
    monkeypatch.setattr(wbs, "roots", lambda: [tmp_path / "proj"])  # backups live outside the searched folders
    monkeypatch.setattr(wbs, "owner_aliases", lambda: ["自分"])
    monkeypatch.setattr(wbs, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(wbs, "STAGE_DIR", tmp_path / "st")
    monkeypatch.setattr(wbs, "PENDING_FILE", tmp_path / "st" / "pending.json")
    monkeypatch.setattr(wbs, "CHANGES_MD", tmp_path / "st" / "changes.md")
    d = wbs.parse(f)
    row = d["sheets"][0]["rows"][1]["row"]
    with pytest.raises(wbs.WbsError):
        wbs.stage(d["id"], "WBS", d["sheets"][0]["rows"][0]["row"], "status", "完了")   # parent rows are not edited
    wbs.stage(d["id"], "WBS", row, "status", "保留")
    assert wbs.stage(d["id"], "WBS", row, "status", "完了")["pending"] == 1                # same cell: replaced, not added
    assert wbs.stage(d["id"], "WBS", row, "actual_end", "2026-11-20")["pending"] == 2
    v = wbs.view(f)
    r = v["sheets"][0]["rows"][1]
    assert r["status"] == "完了" and r["done"] and set(r["pending"]) == {"status", "actual_end"}
    assert wbs.parse(f)["sheets"][0]["rows"][1]["status"] == "進行中"                   # Excel untouched so far
    lock = f.parent / ("~$" + f.name)
    lock.write_text("x")                                                                  # open in Excel: wait
    assert wbs.flush()["skipped_open"] == [f.name] and len(wbs.load_pending()) == 2
    lock.unlink()
    out = wbs.flush()
    assert out["written"] == 2 and out["pending"] == 0 and not out["errors"]
    wb = openpyxl.load_workbook(f)
    assert wb["WBS"].cell(row=row, column=6).value == "完了"
    md = (tmp_path / "st" / "changes.md").read_text(encoding="utf-8")
    assert md.count("| 変更 |") == 3 and md.count("| 転記 |") == 2
    ov = wbs.overview(date(2026, 11, 26))
    assert ov["total"] == 2 and ov["done"] == 2 and ov["pending"] == []
    wbs.stage(d["id"], "WBS", row, "status", "保留")
    pid = wbs.load_pending()[0]["id"]
    assert wbs.discard(pid)["pending"] == 0



def test_wbs_skips_holiday_and_note_rows_and_planned_dates_override_formulas(tmp_path, monkeypatch):
    f = tmp_path / "y_WBS.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "WBS"
    ws.append(["WBS", "タスク", "担当", "期間", "開始", "終了", "状態"])
    ws.append(["1", "作業", "自分", 2, datetime(2026, 11, 2), "=E2+D2-1", "未着手"])
    ws.append(["M1", "◆ 完了"])
    ws.append(["黄色＝入力（工数・期間・固定開始日）の凡例です"])
    ws.append([datetime(2026, 11, 3), "文化の日"])
    wb.save(f)
    monkeypatch.setattr(wbs, "roots", lambda: [tmp_path])
    monkeypatch.setattr(wbs, "BACKUP_DIR", tmp_path.parent / (tmp_path.name + "-bk"))
    sh = wbs.parse(f)["sheets"][0]
    assert [r["no"] for r in sh["rows"]] == ["1", "M1"] and len(sh["notes"]) == 1
    fid = wbs.file_id(f)
    with pytest.raises(wbs.WbsError):   # formula cells are protected by default
        wbs.update(fid, "WBS", 2, "end", "2026-11-10", "手で固定")
    wbs.update(fid, "WBS", 2, "end", "2026-11-10", "手で固定", overwrite_formula=True)
    assert openpyxl.load_workbook(f)["WBS"]["F2"].value is not None
    assert openpyxl.load_workbook(f)["WBS"]["F2"].data_type != "f"
    xml = '<row r="3"><c r="F3"><f t="shared" ref="F3:F9" si="0">E3+1</f></c></row>'
    with pytest.raises(wbs.WbsError):   # shared-formula anchors stay untouched
        wbs._set_cell(xml, "F3", '<c r="F3"><v>1</v></c>', allow_formula=True)


def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "FILE", tmp_path / "vault.json")
    monkeypatch.setattr(vault, "BACKUP_DIR", tmp_path / "bk")
    root = tmp_path / "v"
    for d in ("Pub", "Secret/Sub", "Hidden", "Pub/Private", ".obsidian"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for f in ("Pub/a.md", "Secret/a.md", "Secret/Sub/b.md", "Hidden/a.md", "Pub/Private/p.md", ".obsidian/app.json"):
        (root / f).write_text("x", encoding="utf-8")
    vault.configure(root_path=str(root))
    return root


def test_vault_lock_hide_and_password(tmp_path, monkeypatch):
    root = _vault(tmp_path, monkeypatch)
    with pytest.raises(vault.VaultError):
        vault.configure(folder="Secret", folder_mode="lock")               # password first
    vault.set_password("correct horse")
    vault.configure(folder="Secret", folder_mode="lock")
    vault.configure(folder="Hidden", folder_mode="hide")
    vault.configure(folder="Pub", claude=True)
    vault.configure(folder="Pub/Private", claude=False)
    assert [e["name"] for e in vault.listing("")["entries"]] == ["Pub", "Secret"]   # hidden + .obsidian not listed
    assert vault.listing("Secret")["locked"]
    with pytest.raises(vault.VaultError):
        vault.read("Secret/a.md")
    token = vault.unlock("correct horse")
    assert vault.read("Secret/a.md", token)["text"] == "x"
    allow, deny = vault.claude_roots()
    assert (root / "Pub").resolve() in allow and (root / "Secret").resolve() in deny


@pytest.mark.parametrize("trick", ["Pub/../Secret/a.md", "Secret" + chr(92) + "a.md", "secret/a.md", "SECRET/Sub/b.md"])
def test_vault_path_tricks_do_not_bypass_lock(tmp_path, monkeypatch, trick):
    _vault(tmp_path, monkeypatch)
    vault.set_password("correct horse")
    vault.configure(folder="Secret", folder_mode="lock")
    with pytest.raises(vault.VaultError):
        vault.read(trick)
    with pytest.raises(vault.VaultError):
        vault.write(trick, "pwned", 0)


def test_vault_lock_is_inherited_and_needs_unlock_to_change(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    vault.set_password("correct horse")
    vault.configure(folder="Secret", folder_mode="lock")
    with pytest.raises(vault.VaultError):
        vault.configure(folder="Secret", folder_mode="show")                  # no unlock -> refused
    with pytest.raises(vault.VaultError):
        vault.configure(folder="Secret/Sub", folder_mode="show")              # child of a lock is locked too
    with pytest.raises(vault.VaultError):
        vault.configure(root_path=str(tmp_path))                              # moving the root shifts every lock
    assert vault.mode("Secret/Sub") == "lock"
    token = vault.unlock("correct horse")
    vault.configure(folder="Secret", folder_mode="show", token=token)        # allowed once unlocked
    assert vault.mode("Secret/Sub") == "show"


def test_vault_never_writes_settings_or_new_files(tmp_path, monkeypatch):
    root = _vault(tmp_path, monkeypatch)
    with pytest.raises(vault.VaultError):
        vault.write(".obsidian/app.json", "{}", 0)
    with pytest.raises(vault.VaultError):
        vault.write("Pub/new.md", "x", 0)
    p = root / "Pub" / "crlf.md"
    BOM, NL = bytes([0xEF, 0xBB, 0xBF]), bytes([13, 10])
    p.write_bytes(BOM + b"a" + NL + b"b" + NL)
    f = vault.read("Pub/crlf.md")
    vault.write("Pub/crlf.md", f["text"] + "c" + chr(10), f["mtime"])
    assert p.read_bytes() == BOM + b"a" + NL + b"b" + NL + b"c" + NL  # BOM and CRLF kept
    sj = root / "Pub" / "sj.txt"
    sj.write_bytes("テスト".encode("cp932"))
    assert vault.read("Pub/sj.txt")["editable"] is False
    with pytest.raises(vault.VaultError):
        vault.write("Pub/sj.txt", "x", sj.stat().st_mtime)


def test_chat_reads_projects_but_never_locked(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "locked").mkdir(parents=True)
    monkeypatch.setattr(chat, "_extra_roots", lambda: ([proj.resolve()], [(proj / "locked").resolve()]))
    monkeypatch.setattr(vault, "root", lambda: None)
    assert chat.check_read(str(proj / "spec.xlsx"), "mcp__files__read_excel") is None
    assert chat.check_read(str(proj / "locked" / "x.md"), "Read")            # vault-locked folder
    assert chat.check_read(str(proj), "Grep")                                # a search would reach the locked folder
    assert chat.check_read(str(tmp_path / "elsewhere.md"), "Read")          # outside every allowed root
    (proj / "sub").mkdir()
    (proj / "sub" / ".env").write_text("SECRET=1", encoding="utf-8")
    assert chat.check_read(str(proj / "sub"), "Grep")                        # a search would read .env


def test_chat_cannot_read_logs_or_vault_config():
    from nakama.config import ROOT

    for rel in ("logs/vault-backup/x.md", "logs/nakama.log", "config/vault.json", ".git/config", ".claude/settings.json"):
        assert chat.check_read(str(ROOT / rel), "Read"), rel
    assert chat.check_read(str(ROOT / "nakama" / "ui.py"), "Read") is None


def test_chat_respects_claude_off_subfolder(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    vault.configure(folder="Pub", claude=True)
    vault.configure(folder="Pub/Private", claude=False)
    monkeypatch.setattr(chat, "_extra_roots", lambda: vault.claude_roots())
    root = vault.root()
    assert chat.check_read(str(root / "Pub" / "a.md"), "Read") is None
    assert chat.check_read(str(root / "Pub" / "Private" / "p.md"), "Read")   # explicit "Claude: no" wins
    assert chat.check_read(str(root / "Pub"), "Grep")                        # search would reach it


def test_projects_require_existing_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "FILE", tmp_path / "p.json")
    (tmp_path / "docs").mkdir()
    projects.upsert("A", [str(tmp_path / "docs")], "note")
    assert projects.load()[0]["folders"] == [(tmp_path / "docs").resolve().as_posix()]
    with pytest.raises(ValueError):
        projects.upsert("B", [str(tmp_path / "missing")])
    assert "根拠" in projects.prompt_block()
