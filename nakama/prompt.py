"""Assemble each agent's system prompt and per-run user prompt."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from .bizdays import prompt_section
from .config import AGENTS_DIR, LOCAL_CONNECTOR_PATHS, AgentSpec, Fleet, load_team
from .versioning import version_path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def personal_layer(spec: AgentSpec) -> str:
    """LOCAL.md (live) or a pinned snapshot from versions/."""
    if spec.pin_version is not None:
        pinned = version_path(spec.name, spec.pin_version)
        if not pinned.exists():
            raise FileNotFoundError(f"pin_version {spec.pin_version} not found: {pinned}")
        return _read(pinned)
    return _read(AGENTS_DIR / spec.name / "LOCAL.md")


def build_system_prompt(spec: AgentSpec) -> str:
    """Static part only (shared rules + role + personal tuning) so it stays cache-friendly."""
    parts = [
        _read(AGENTS_DIR / "_shared" / "COMMON.md"),
        _read(AGENTS_DIR / spec.name / "AGENT.md"),
    ]
    for raw in spec.instructions_files:  # e.g. an existing SKILL.md that stays the single source of truth
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{spec.name}: instructions_files entry not found: {path}")
        parts.append(f"# 手順書（{path.name}。読み込み専用の正本）\n\n" + _read(path))
    local = personal_layer(spec)
    if local:
        parts.append("# オーナーによる個別チューニング（AGENT.md より優先）\n\n" + local)
    return "\n\n---\n\n".join(p for p in parts if p)


def _connector_lines(spec: AgentSpec, fleet: Fleet) -> list[str]:
    lines = []
    for name in spec.connectors:
        target = fleet.connectors.get(name, "local")
        if target.startswith("mcp:"):
            lines.append(f"- {name}: MCP サーバー `{target[4:]}` のツールを使う")
        else:
            paths = ", ".join(f"`{p}`" if Path(p).suffix else f"`{p}/`" for p in LOCAL_CONNECTOR_PATHS.get(name, []))
            lines.append(f"- {name}: ローカルファイル {paths}")
    return lines or ["- （外部データソースなし）"]


def build_user_prompt(
    spec: AgentSpec, fleet: Fleet, now: datetime, changed_files: list[str], extra: str | None = None
) -> str:
    """Dynamic runtime context: date, connectors, team, write scope, changed files."""
    team = load_team()
    today = now.date().isoformat()
    ooo = [o["member"] for o in team.get("out_of_office") or [] if today in (o.get("dates") or [])]
    owner = {k: v for k, v in fleet.owner.items() if k != "slack_user_id"} | {
        "slack_user_id": fleet.owner.get("slack_user_id") or "(未設定)"
    }

    sections = [
        f"# ランタイムコンテキスト\n- 現在時刻: {now.isoformat(timespec='minutes')}（{fleet.timezone}）\n- エージェント: {spec.name}",
        "## オーナー\n```yaml\n" + yaml.safe_dump(owner, allow_unicode=True, sort_keys=False) + "```",
        "## データソース（コネクタ）\n" + "\n".join(_connector_lines(spec, fleet)),
        prompt_section(now.date()),
        "## 書き込み可能パス（これ以外への書き込みは拒否される）\n"
        + "\n".join(f"- `{w}`" for w in spec.writes),
    ]
    if spec.name == "furiwake":
        members = yaml.safe_dump(team.get("members") or [], allow_unicode=True, sort_keys=False)
        sections.append(f"## チーム\n```yaml\n{members}```\n- 本日の不在者: {ooo or 'なし'}")
    if "ticketing" in spec.helpers and fleet.ticketing.get("base"):
        from .helpers.ticketing import Paths

        paths = Paths.from_config(fleet.ticketing)
        base = paths.base
        weekday = "月火水木金土日"[now.weekday()]
        sections.append(
            "## 起票パイプライン\n"
            f"- BASE: `{base}`\n- TICKETS_DIR: `{paths.tickets}`\n- STATE_DIR: `{paths.state}`\n"
            f"- STANDUP_DIR: `{paths.minutes}`\n- roster: `{paths.roster}`\n"
            f"- 今日: {today}（{weekday}）/ 時刻から見たサイクル: {'am' if now.hour < 15 else 'pm'}\n"
            f"- ツールのパスは BASE からの相対パスで渡せる（例: `planner_state/planner_state_am/planner_state_{today}.json`）"
        )
    if changed_files:
        sections.append("## 前回の成功実行以降に変更されたファイル\n" + "\n".join(f"- `{f}`" for f in changed_files))
    if extra:
        sections.append("## 追加の指示（手動実行時）\n" + extra)

    sections.append("上記のコンテキストで、あなたの役割の作業を 1 巡実行し、最後に実行レポートを返してください。")
    return "\n\n".join(sections)
