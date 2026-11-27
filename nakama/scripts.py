"""Run registered ops scripts from the dashboard and turn their output into a ready-to-send message.

Only scripts listed in config/fleet.yaml `scripts:` can run (fixed command, no arguments from the UI):

  scripts:
    - id: cost-forecast                      # [a-z0-9-]
      name: AWS 月末コスト見込み
      cwd: C:/path/to/get_aws_cost_forecast
      command: ["{python}", "get_forecast_aws_cost.py"]   # {python} = Nakama's python
      timeout: 180
      routine: r016                          # show the run button on this routine (optional)
      extract_after: 報告メール貼り付け用      # keep only the output after this line (optional)
      plan_values: {SIT: 3000, PRD: 17000}   # cost_forecast: fill 計画値 and pick 内着地/超過 (optional)
      template: |                            # {output} {name} {month} {today}
        〇〇さん
        ...{output}...
      checklist: ["金額のオーダーが前月と大きく乖離していない", ...]

Each run is kept in workspace/scripts/<id>/<timestamp>.json (stdout, stderr, exit code, message).
Nothing is sent anywhere: the owner copies the message and sends it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import WORKSPACE_DIR, load_fleet

RUNS_DIR = WORKSPACE_DIR / "scripts"
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_running: dict[str, str] = {}  # script id -> run id
_lock = threading.Lock()


class ScriptError(Exception):
    pass


def registry() -> list[dict[str, Any]]:
    out = []
    for s in getattr(load_fleet(), "scripts", None) or []:
        if isinstance(s, dict) and ID_RE.fullmatch(str(s.get("id", ""))) and s.get("command"):
            out.append(s)
    return out


def get(sid: str) -> dict[str, Any]:
    s = next((x for x in registry() if x["id"] == sid), None)
    if s is None:
        raise ScriptError("登録されていないスクリプトです（fleet.yaml の scripts）")
    return s


def _runs(sid: str) -> list[Path]:
    d = RUNS_DIR / sid
    return sorted(d.glob("*.json"), reverse=True) if d.exists() else []


def listing() -> list[dict[str, Any]]:
    rows = []
    for s in registry():
        last = _runs(s["id"])[:1]
        rows.append({"id": s["id"], "name": s.get("name") or s["id"], "routine": s.get("routine") or "",
                     "cwd": str(s.get("cwd") or ""), "running": s["id"] in _running,
                     "last": _with_message(s, json.loads(last[0].read_text(encoding="utf-8"))) if last else None,
                     "checklist": list(s.get("checklist") or []),
                     "envs": envs(s), "plans": load_plans(s["id"]), "month": _month()})
    return rows


# ---------- monthly plan values (q116): asked before the run, used by the message ----------
ENV_RE = re.compile(r"・(\w+)\(計画値")


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


def envs(spec: dict[str, Any]) -> list[str]:
    """Environment names: plan_values keys, else the ones the last output printed (・SIT(計画値...)."""
    names = list((spec.get("plan_values") or {}).keys())
    last = _runs(spec["id"])[:1]
    if last:
        try:
            out = json.loads(last[0].read_text(encoding="utf-8")).get("stdout", "")
        except (OSError, ValueError):
            out = ""
        names += [n for n in ENV_RE.findall(out) if n not in names]
    return names


def load_plans(sid: str, month: str = "") -> dict[str, Any]:
    try:
        data = json.loads((RUNS_DIR / sid / "plans.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data.get(month or _month()) or {})


def save_plans(sid: str, values: dict[str, Any], month: str = "") -> dict[str, Any]:
    get(sid)
    month = month or _month()
    clean = {}
    for k, v in (values or {}).items():
        if not re.fullmatch(r"\w{1,20}", str(k)):
            raise ScriptError(f"環境名が不正です: {k}")
        v = str(v).replace(",", "").replace("$", "").strip()
        if v:
            try:
                clean[str(k)] = float(v)
            except ValueError as exc:
                raise ScriptError(f"{k} の計画値は数字で入れてください") from exc
    path = RUNS_DIR / sid / "plans.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[month] = clean
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": sid, "month": month, "plans": clean}


def _cost_forecast(text: str, plans: dict[str, Any]) -> str:
    """Fill 計画値$XXXXX and keep only the right one of 計画値内着地/超過見込み, per environment block."""
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = re.search(r"・(\w+)\(計画値\$([^)]*)\)", block)
        amount = re.search(r"月末見込み:\$([\d,]+(?:\.\d+)?)", block)
        if m and m.group(1) in plans:
            plan = float(str(plans[m.group(1)]).replace(",", ""))
            block = block.replace(f"計画値${m.group(2)}", f"計画値${plan:,.0f}")
            if amount:
                over = float(amount.group(1).replace(",", "")) > plan
                block = block.replace("計画値内着地/超過見込み", "超過見込み" if over else "計画値内着地見込み")
        out.append(block)
    return "\n\n".join(out)


def build_message(spec: dict[str, Any], stdout: str) -> str:
    text = stdout
    marker = str(spec.get("extract_after") or "")
    if marker and marker in text:
        text = text.split(marker, 1)[1]
        text = re.sub(r"^[=\-\s]*\n", "", text.lstrip("\n"))  # the ===== rule under the marker
    plans = {**dict(spec.get("plan_values") or {}), **load_plans(str(spec.get("id", "")))}  # this month's entry wins
    if plans:
        text = _cost_forecast(text, plans)
    tpl = str(spec.get("template") or "{output}")
    fleet = load_fleet()
    name = str((fleet.owner or {}).get("name") or "")
    now = datetime.now()
    return tpl.replace("{output}", text.strip()).replace("{name}", name if name and name != "Your Name" else "<自分の名前>") \
        .replace("{month}", f"{now.month}月").replace("{today}", now.strftime("%Y-%m-%d"))


def _command(spec: dict[str, Any]) -> list[str]:
    cmd = [str(c).replace("{python}", sys.executable) for c in spec["command"]]
    if not cmd:
        raise ScriptError("command が空です")
    return cmd


def run(sid: str) -> dict[str, Any]:
    """Start the script in the background; poll result(sid) for the outcome."""
    spec = get(sid)
    cwd = Path(str(spec.get("cwd") or "."))
    if not cwd.is_dir():
        raise ScriptError(f"作業フォルダがありません: {cwd}")
    with _lock:
        if sid in _running:
            raise ScriptError("実行中です。終わるまでお待ちください")
        rid = datetime.now().strftime("%Y%m%d-%H%M%S")
        _running[sid] = rid
    threading.Thread(target=_work, args=(spec, cwd, rid), daemon=True).start()
    return {"id": sid, "run": rid}


def _work(spec: dict[str, Any], cwd: Path, rid: str) -> None:
    sid = spec["id"]
    rec: dict[str, Any] = {"run": rid, "started": datetime.now().isoformat(timespec="seconds"), "cwd": str(cwd)}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}  # Japanese output from python scripts
    try:
        p = subprocess.run(_command(spec), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(spec.get("timeout") or 180), env=env, creationflags=NO_WINDOW)
        rec.update(exit=p.returncode, stdout=p.stdout[-20000:], stderr=p.stderr[-8000:])
        rec["message"] = build_message(spec, p.stdout) if p.stdout.strip() else ""
        rec["failed"] = p.returncode != 0 or "取得失敗" in rec["message"] or "[ERROR]" in p.stderr
    except subprocess.TimeoutExpired:
        rec.update(exit=-1, stdout="", stderr="タイムアウトしました", message="", failed=True)
    except (OSError, ScriptError) as exc:
        rec.update(exit=-1, stdout="", stderr=str(exc), message="", failed=True)
    rec["finished"] = datetime.now().isoformat(timespec="seconds")
    d = RUNS_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    with _lock:
        _running.pop(sid, None)


def result(sid: str) -> dict[str, Any]:
    get(sid)
    if sid in _running:
        return {"id": sid, "running": True, "run": _running[sid]}
    last = _runs(sid)[:1]
    return {"id": sid, "running": False,
            "last": _with_message(get(sid), json.loads(last[0].read_text(encoding="utf-8"))) if last else None}


def _with_message(spec: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the message from the saved stdout so plan values entered after the run still apply."""
    if rec.get("stdout", "").strip() and rec.get("exit") == 0:
        rec["message"] = build_message(spec, rec["stdout"])
    return rec
