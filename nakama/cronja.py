"""Read cron expressions out loud in Japanese ("平日 7:30", "毎日 8-22 時の 15 分ごと") for the dashboard."""
from __future__ import annotations

DOW = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}

HELP = """\
cron は「分 時 日 月 曜日」の 5 つの欄で時刻を表します（左から）。
- `30 7 * * mon-fri` … 平日の 7:30
- `5 9-18 * * mon-fri` … 平日の 9〜18 時の毎時 5 分
- `*/15 8-22 * * *` … 毎日 8〜22 時の 15 分ごと
- `0 12,19 * * *` … 毎日 12:00 と 19:00
`*` は「すべて」、`a-b` は範囲、`a,b` は列挙、`*/n` は n ごと。曜日は mon〜sun（数字は使わない）。
変更は 設定 > fleet.yaml の schedule で（保存から 1 分以内に反映）。"""


def _days(dow: str) -> str:
    if dow == "*":
        return "毎日"
    if dow == "mon-fri":
        return "平日"
    if dow in ("sat,sun", "sat-sun"):
        return "土日"
    parts = []
    for p in dow.split(","):
        a, _, b = p.partition("-")
        parts.append(f"{DOW.get(a, a)}〜{DOW.get(b, b)}" if b else DOW.get(a, a))
    return "・".join(parts) + "曜"


def describe(expr: str) -> str:
    try:
        minute, hour, dom, month, dow = expr.split()
    except ValueError:
        return expr
    when = _days(dow) if dom == "*" and month == "*" else f"{month if month != '*' else '毎'}月 {dom}日"
    if minute.startswith("*/"):
        rng = "" if hour == "*" else (f"{hour.replace('-', '〜')}時の" if "-" in hour else f"{hour}時台の")
        return f"{when} {rng}{minute[2:]} 分ごと"
    if "-" in hour and "," not in hour:
        a, b = hour.split("-")
        return f"{when} {a}〜{b} 時の毎時 {int(minute):02d} 分"
    if hour == "*":
        return f"{when} 毎時 {int(minute):02d} 分"
    times = [f"{int(h)}:{int(minute):02d}" for h in hour.split(",")]
    return f"{when} {' / '.join(times)}"
