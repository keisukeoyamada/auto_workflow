"""Offline tests for the credit statement split (no API calls, no real data)."""
import base64
from datetime import datetime
from types import SimpleNamespace

import pytest

from nakama import credit

CFG = {
    "enabled": True,
    "members": [
        {"label": "A", "color": "blue", "match": ["TARO YAMADA"]},
        {"label": "B", "color": "pink", "match": ["HANAKO YAMADA"]},
    ],
    "shared_label": "共同",
    "closing_day": 19,
}

# Per-row holder column, full-width merchant names, amounts with commas, a totals line to skip.
CSV_ROWS = (
    "ご利用日,ご利用者,ご利用内容,金額\r\n"
    "2026/10/18,TARO YAMADA,ＪＲ　えきねっと,\"25,160\"\r\n"
    "2026/10/04,TARO YAMADA,サイクル　利用料金,495\r\n"
    "2026/10/04,TARO YAMADA,サイクル　利用料金,495\r\n"
    "2026/10/03,HANAKO YAMADA,スーパー　食品,840\r\n"
    "2026/09/24,HANAKO YAMADA,返品,-1001\r\n"
    ",,合計,\"25,989\"\r\n"
)
# Section lines naming the member instead of a holder column.
CSV_SECTIONS = (
    "ご利用日,ご利用店名,ご利用金額\r\n"
    "カード会員: TARO YAMADA,,\r\n"
    "2026/10/01,STORE ONE,1000\r\n"
    "カード会員: HANAKO YAMADA,,\r\n"
    "2026/10/02,STORE TWO,2001\r\n"
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(credit, "DIR", tmp_path / "credit")
    monkeypatch.setattr(credit, "load_fleet", lambda: SimpleNamespace(credit=CFG))
    return CFG


def _b64(text: str, enc: str = "utf-8") -> str:
    return base64.b64encode(text.encode(enc)).decode()


def test_parse_holder_column_and_skip_totals(cfg):
    rows = credit.parse_csv(CSV_ROWS, cfg)
    assert len(rows) == 5
    assert rows[0] == {"date": "2026-10-18", "holder": "TARO YAMADA", "holder_key": "a", "card": "",
                       "merchant": "ＪＲ　えきねっと", "amount": 25160}
    assert rows[4]["holder_key"] == "b" and rows[4]["amount"] == -1001


def test_parse_section_lines_and_shift_jis(cfg):
    rows = credit.parse_csv(credit.decode(CSV_SECTIONS.encode("cp932")), cfg)
    assert [(r["holder_key"], r["amount"]) for r in rows] == [("a", 1000), ("b", 2001)]


def test_parse_rejects_unknown_layout(cfg):
    with pytest.raises(ValueError, match="見出し"):
        credit.parse_csv("foo,bar\n1,2\n", cfg)


def test_detect_month_uses_closing_day():
    rows = [{"date": "2026-10-19"}, {"date": "2026-09-30"}]
    assert credit.detect_month(rows, 19) == "202610"
    assert credit.detect_month([{"date": "2026-12-20"}], 19) == "202701"


def test_parts_per_split():
    row = {"amount": 1001}
    assert credit.parts(row | {"split": "shared"}) == (0, 0, 1001)
    assert credit.parts(row | {"split": "a"}) == (1001, 0, 0)
    assert credit.parts(row | {"split": "half"}) == (500, 500, 1)  # odd yen stays shared
    assert credit.parts({"amount": -1001, "split": "half"}) == (-500, -500, -1)
    assert credit.parts({"amount": 48400, "split": "custom", "a": 25000, "b": 25000}) == (25000, 25000, -1600)


def test_import_split_close_flow(cfg):
    now = datetime(2026, 10, 20, 9, 0)
    r = credit.import_csv("statement.csv", _b64(CSV_ROWS), now=now)
    assert r == {"month": "202610", "rows": 5, "added": 5, "kept": 0, "dropped": 0, "excluded": []}
    view = credit.month_view("202610")
    ids = {(x["merchant"], x["date"]): x["id"] for x in view["rows"]}
    assert len({x["id"] for x in view["rows"]}) == 5  # identical rows get distinct ids
    assert [x["holder_key"] for x in view["rows"]] == ["a", "a", "a", "b", "b"]

    jr = ids[("ＪＲ　えきねっと", "2026-10-18")]
    view = credit.update_rows("202610", [jr], {"split": "half"})
    view = credit.update_rows("202610", [ids[("スーパー　食品", "2026-10-03")]], {"split": "b", "memo": "自分用"})
    view = credit.update_rows("202610", [jr], {"a": "13000"})  # typing an amount switches to custom
    row = next(x for x in view["rows"] if x["id"] == jr)
    assert (row["split"], row["a"], row["b"], row["shared"]) == ("custom", 13000, 12580, -420)
    assert view["total"] == {"amount": 25989, "a": 13000, "b": 13420, "shared": -431}
    assert not view["can_close"]

    view = credit.set_meta("202610", balance="1,000", billed="25989")
    assert view["deposit"] == {"a": 13000, "b": 13420, "shared": -1431}
    with pytest.raises(ValueError, match="確定できません"):
        credit.close("202610", True)
    credit.update_rows("202610", [x["id"] for x in view["rows"]], {"checked": True})
    view = credit.close("202610", True, now=now)
    assert view["closed"]
    assert (credit.DIR / "exports" / "202610.csv").read_text(encoding="utf-8").startswith("﻿日付,誰,内容")
    with pytest.raises(ValueError, match="確定済み"):
        credit.update_rows("202610", [jr], {"split": "a"})

    # Re-importing after reopening keeps what was entered.
    credit.close("202610", False)
    r = credit.import_csv("statement.csv", _b64(CSV_ROWS, "cp932"), now=now)
    assert (r["added"], r["kept"]) == (0, 5)
    assert next(x for x in credit.month_view("202610")["rows"] if x["id"] == jr)["a"] == 13000


def test_disabled_and_pdf_are_refused(cfg, monkeypatch):
    with pytest.raises(ValueError, match="PDF"):
        credit.import_csv("statement.pdf", _b64("x"))
    monkeypatch.setattr(credit, "load_fleet", lambda: SimpleNamespace(credit={"enabled": False}))
    with pytest.raises(PermissionError):
        credit.import_csv("statement.csv", _b64(CSV_ROWS))
    assert credit.months()["months"] == []


def test_export_matches_sheet_layout(cfg):
    credit.import_csv("s.csv", _b64(CSV_ROWS), now=datetime(2026, 10, 20))
    out = credit.export_csv("202610")
    lines = out["csv"].lstrip("﻿").splitlines()
    assert lines[0] == "日付,誰,内容,状態,金額,A負担額,B負担額,共同から負担額,memo"
    assert lines[1].startswith("2026/10/18,TARO YAMADA,ＪＲ　えきねっと,,25160,0,0,25160")


# Layout of a card company's activity CSV: member + card number columns, and the last bill's debit row.
CSV_ACTIVITY = (
    "ご利用日,データ処理日,ご利用内容,カード会員様名,会員番号 #,金額,海外通貨利用金額,換算レート\r\n"
    "2026/11/19,2026/11/20,スーパー　食品,TARO YAMADA,-11111,\"2,615\",,\r\n"
    "2026/11/18,2026/11/20,スーパー　食品,HANAKO YAMADA,-22222,\"1,633\",,\r\n"
    "2026/11/11,2026/11/11,ポイント充当,TARO YAMADA,-11111,-534,,\r\n"
    "2026/11/10,2026/11/10,前回分口座振替金額,TARO YAMADA,-11111,\"-507,113\",,\r\n"
)


def test_activity_layout_card_and_excluded_debit(cfg):
    excluded = []
    rows = credit.parse_csv(CSV_ACTIVITY, cfg, excluded)
    assert [(r["holder_key"], r["card"], r["amount"]) for r in rows] == [("a", "11111", 2615), ("b", "22222", 1633),
                                                                          ("a", "11111", -534)]
    assert [x["amount"] for x in excluded] == [-507113]
    r = credit.import_csv("activity.csv", _b64(CSV_ACTIVITY, "cp932"), now=datetime(2026, 11, 27))
    assert (r["month"], r["rows"], len(r["excluded"])) == ("202611", 3, 1)
    assert credit.month_view("202611")["total"]["amount"] == 2615 + 1633 - 534


def test_reimport_keeps_input_when_member_matching_changes(cfg, monkeypatch):
    unmatched = CFG | {"members": [{"label": "A", "match": ["NOBODY"]}, {"label": "B", "match": ["NOBODY2"]}]}
    monkeypatch.setattr(credit, "load_fleet", lambda: SimpleNamespace(credit=unmatched))
    credit.import_csv("a.csv", _b64(CSV_ACTIVITY), now=datetime(2026, 11, 27))
    first = next(x for x in credit.month_view("202611")["rows"] if x["amount"] == 2615)  # TARO's row
    assert first["holder_key"] == ""
    credit.update_rows("202611", [first["id"]], {"split": "b", "memo": "note"})
    monkeypatch.setattr(credit, "load_fleet", lambda: SimpleNamespace(credit=CFG))
    r = credit.import_csv("a.csv", _b64(CSV_ACTIVITY), now=datetime(2026, 11, 27, 1))
    assert (r["added"], r["kept"]) == (0, 3)
    row = next(x for x in credit.month_view("202611")["rows"] if x["id"] == first["id"])
    assert (row["holder_key"], row["split"], row["memo"]) == ("a", "b", "note")


def _workbook() -> bytes:
    """Same shape as a hand-made sheet: totals row, header, formulas without cached values."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "202609"
    ws.append(["ご請求金額 ￥5,040 お支払い日 2026/10/13", None, None, "合計>>>>", "=SUM(E3:E99)", None, None, None,
               "=H1-J1", 1000, "<<口座の残り"])
    ws.append(["日付", "誰", "内容", "状態", "金額", "A負担額", "B負担額", "共同から負担額", "memo"])
    ws.append([datetime(2026, 9, 1), "TARO YAMADA", "スーパー 食品", None, 1000, None, None, "=E3-(F3+G3)"])
    ws.append([datetime(2026, 9, 2), "TARO YAMADA", "JR えきねっと 東京都 渋谷区", None, 2000, "=E4/2", "=$E$4/2", "=E4-(F4+G4)", "割り勘"])
    ws.append([datetime(2026, 9, 3), "HANAKO YAMADA", "服 店", None, 1540, None, "=E5", "=E5-(F5+G5)"])
    ws.append([datetime(2026, 9, 4), "TARO YAMADA", "百貨店", None, 500, 300, 300, "=E6-(F6+G6)"])
    other = wb.create_sheet("memo")
    other.append(["日付", "用途区分", "内容"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_workbook_imports_closed_history_with_formulas(cfg):
    r = credit.import_csv("book.xlsx", base64.b64encode(_workbook()).decode(), now=datetime(2026, 11, 27))
    assert r["month"] == "202609"
    assert r["workbook"]["imported"] == [{"month": "202609", "rows": 4}]
    assert [x["sheet"] for x in r["workbook"]["skipped"]] == ["memo"]
    v = credit.month_view("202609")
    assert v["closed"] and v["billed"] == 5040 and v["balance"] == 1000
    split = {x["merchant"]: (x["split"], x["a"], x["b"]) for x in v["rows"]}
    assert split == {"スーパー 食品": ("shared", 0, 0), "JR えきねっと 東京都 渋谷区": ("half", 1000, 1000),
                     "服 店": ("b", 0, 1540), "百貨店": ("custom", 300, 300)}
    assert v["statement_total"] == 5040 and v["can_close"]
    # importing again never overwrites a month that exists
    with pytest.raises(ValueError, match="取り込める月がありません"):
        credit.import_csv("book.xlsx", base64.b64encode(_workbook()).decode())


def test_hints_from_past_months(cfg):
    credit.import_csv("book.xlsx", base64.b64encode(_workbook()).decode(), now=datetime(2026, 11, 27))
    credit.import_csv("statement.csv", _b64(CSV_ROWS), now=datetime(2026, 11, 27))  # 202610
    rows = {x["merchant"]: x["hint"] for x in credit.month_view("202610")["rows"]}
    assert rows["ＪＲ　えきねっと"]["split"] == "half"  # same shop, different trailing place name
    assert rows["スーパー　食品"] == {"split": "shared", "month": "202609", "same": 1, "count": 1}
    assert rows["返品"] is None
    assert all(x["hint"] is None for x in credit.month_view("202609")["rows"])  # only earlier months count


def test_exclude_row_from_split_but_not_from_bill(cfg):
    credit.import_csv("activity.csv", _b64(CSV_ACTIVITY), now=datetime(2026, 11, 27))
    v = credit.set_meta("202611", billed="3714", balance="0")
    point = next(x for x in v["rows"] if x["amount"] == -534)
    v = credit.update_rows("202611", [point["id"]], {"excluded": True})
    assert v["total"]["amount"] == 2615 + 1633 and v["excluded"] == {"count": 1, "amount": -534}
    assert v["statement_total"] == 3714 and not any("請求額" in p for p in v["problems"])
    assert v["unchecked"] == 2  # an excluded row needs no check
    assert "除外" in credit.export_csv("202611")["csv"]


def test_months_list_has_figures_for_each_month(cfg):
    credit.import_csv("book.xlsx", base64.b64encode(_workbook()).decode(), now=datetime(2026, 11, 27))
    credit.import_csv("activity.csv", _b64(CSV_ACTIVITY), now=datetime(2026, 11, 27))
    credit.set_meta("202611", balance="500")
    ms = {m["month"]: m for m in credit.months()["months"]}
    assert list(ms) == ["202611", "202609"]  # newest first
    assert ms["202609"]["closed"] and ms["202609"]["billed"] == ms["202609"]["statement_total"] == 5040
    assert ms["202611"]["balance"] == 500 and ms["202611"]["deposit"]["shared"] == 3714 - 500
    assert ms["202611"]["excluded"] == {"count": 0, "amount": 0}
