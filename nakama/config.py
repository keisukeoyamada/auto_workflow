"""Load fleet / team / MCP configuration and resolve project paths."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Project root: NAKAMA_HOME env var (container) or the repo root next to this package.
ROOT = Path(os.environ.get("NAKAMA_HOME", Path(__file__).resolve().parent.parent)).resolve()
CONFIG_DIR = ROOT / "config"
AGENTS_DIR = ROOT / "agents"
KNOWLEDGE_DIR = ROOT / "knowledge"
WORKSPACE_DIR = ROOT / "workspace"
LOGS_DIR = ROOT / "logs"
TEMPLATES_DIR = ROOT / "templates"
SAMPLES_DIR = ROOT / "samples"

# Local data layout for each connector when it is `local`.
LOCAL_CONNECTOR_PATHS: dict[str, list[str]] = {
    "crm": ["workspace/crm/leads", "workspace/crm/opportunities"],
    "mail": ["workspace/mail/inbox", "workspace/mail/sent", "workspace/mail/drafts"],
    "calendar": ["workspace/calendar"],
    "chat": ["workspace/requests", "workspace/reports", "workspace/briefs"],
    "attendance": ["workspace/attendance/sources", "workspace/attendance/mine.md"],
}


class ConfigError(Exception):
    """Raised when configuration files are missing or invalid."""


@dataclass
class AgentSpec:
    name: str
    model: str
    schedule: list[str] = field(default_factory=list)
    enabled: bool = True
    effort: str | None = None
    max_turns: int = 50
    max_budget_usd: float | None = None
    connectors: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    trigger_paths: list[str] = field(default_factory=list)
    advisor: dict[str, Any] | None = None
    pin_version: int | None = None
    disallowed_tools: list[str] = field(default_factory=list)
    web: bool = False
    # Do not run on holidays listed in config/holidays.json (weekday agents; cron cannot know holidays).
    skip_holidays: bool = False
    # Extra built-in helper tool bundles (e.g. ["ticketing"]; see nakama/helpers/).
    helpers: list[str] = field(default_factory=list)
    # Files appended to the system prompt on every run (e.g. an existing SKILL.md kept elsewhere).
    instructions_files: list[str] = field(default_factory=list)


AUTH_MODES = ("auto", "api_key", "subscription")


@dataclass
class Fleet:
    timezone: str
    owner: dict[str, Any]
    limits: dict[str, Any]
    notify: dict[str, Any]
    connectors: dict[str, str]
    agents: dict[str, AgentSpec]
    source: Path
    auth: str = "auto"  # auto | api_key | subscription (see runner.resolve_auth)
    ticketing: dict[str, Any] = field(default_factory=dict)  # settings for the "ticketing" helper
    cost_plans: list[dict[str, Any]] = field(default_factory=list)  # license scenarios for the cost tab
    todo_note: dict[str, Any] = field(default_factory=dict)  # hand-written ToDo note synced both ways
    overtime: dict[str, Any] = field(default_factory=dict)  # overtime log / 18:00 reminder settings
    credit: dict[str, Any] = field(default_factory=dict)  # credit card statement split tab (off by default)
    rongo: dict[str, Any] = field(default_factory=dict)  # the Analects, one chapter a day (off by default)


def _first_existing(*paths: Path) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def load_fleet(path: Path | None = None) -> Fleet:
    """Load config/fleet.yaml, falling back to the committed example (or an explicit file to validate)."""
    path = path or _first_existing(CONFIG_DIR / "fleet.yaml", CONFIG_DIR / "fleet.example.yaml")
    if path is None:
        raise ConfigError("config/fleet.yaml not found. Run `python -m nakama init`.")
    raw = _load_yaml(path)

    agents: dict[str, AgentSpec] = {}
    for name, spec in (raw.get("agents") or {}).items():
        if "model" not in spec:
            raise ConfigError(f"agents.{name}.model is required ({path})")
        unknown = set(spec) - set(AgentSpec.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"agents.{name}: unknown keys {sorted(unknown)} ({path})")
        agents[name] = AgentSpec(name=name, **spec)
        if not (AGENTS_DIR / name / "AGENT.md").exists():
            raise ConfigError(f"agents/{name}/AGENT.md is missing")

    auth = str(raw.get("auth", "auto"))
    if auth not in AUTH_MODES:
        raise ConfigError(f"auth must be one of {AUTH_MODES}, got '{auth}' ({path})")

    return Fleet(
        auth=auth,
        ticketing=raw.get("ticketing") or {},
        cost_plans=raw.get("cost_plans") or [],
        todo_note=raw.get("todo_note") or {},
        overtime=raw.get("overtime") or {},
        credit=raw.get("credit") or {},
        rongo=raw.get("rongo") or {},
        timezone=raw.get("timezone", "UTC"),
        owner=raw.get("owner") or {},
        limits=raw.get("limits") or {},
        notify=raw.get("notify") or {},
        connectors=raw.get("connectors") or {},
        agents=agents,
        source=path,
    )


def load_team() -> dict[str, Any]:
    path = _first_existing(CONFIG_DIR / "team.yaml", CONFIG_DIR / "team.example.yaml")
    return _load_yaml(path) if path else {"members": []}


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} with environment values (missing -> empty string)."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def load_mcp_servers() -> dict[str, dict[str, Any]]:
    """Return MCP server configs from config/mcp.json (empty if absent)."""
    path = CONFIG_DIR / "mcp.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    return _expand_env(raw.get("mcpServers") or {})


def mcp_servers_for(spec: AgentSpec, fleet: Fleet) -> dict[str, dict[str, Any]]:
    """Pick only the MCP servers this agent's connectors need (least privilege)."""
    needed = {
        fleet.connectors[c].split(":", 1)[1]
        for c in spec.connectors
        if fleet.connectors.get(c, "local").startswith("mcp:")
    }
    if not needed:
        return {}
    available = load_mcp_servers()
    missing = needed - set(available)
    if missing:
        raise ConfigError(f"{spec.name}: MCP servers {sorted(missing)} not defined in config/mcp.json")
    return {name: available[name] for name in needed}
