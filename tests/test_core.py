"""Offline tests: no API calls. Run with `pytest`."""
from apscheduler.triggers.cron import CronTrigger

from nakama.config import ROOT, AgentSpec, load_fleet
from nakama.guard import check_tool_call
from nakama.prompt import build_system_prompt


def _spec(**kw) -> AgentSpec:
    return AgentSpec(name="henji", model="claude-sonnet-5-5", writes=["workspace/mail/drafts", "knowledge"], **kw)


def test_write_inside_scope_is_allowed():
    assert check_tool_call(_spec(), "Write", {"file_path": "workspace/mail/drafts/D-1.md"}) is None
    assert check_tool_call(_spec(), "Edit", {"file_path": str(ROOT / "knowledge" / "learnings.md")}) is None


def test_write_outside_scope_is_denied():
    assert check_tool_call(_spec(), "Write", {"file_path": "workspace/crm/leads/L-1.json"})
    assert check_tool_call(_spec(), "Write", {"file_path": "workspace/mail/drafts/../../../.env"})
    assert check_tool_call(_spec(), "Edit", {"file_path": "agents/henji/AGENT.md"})


def test_secrets_are_not_readable():
    assert check_tool_call(_spec(), "Read", {"file_path": ".env"})
    assert check_tool_call(_spec(), "Read", {"file_path": "config/mcp.json"})
    assert check_tool_call(_spec(), "Grep", {"pattern": "API_KEY"})  # no path -> repo root -> denied
    assert check_tool_call(_spec(), "Read", {"file_path": "knowledge/rulebook.md"}) is None


def test_example_fleet_loads_and_cron_parses():
    fleet = load_fleet()
    assert {"furiwake", "sabaki", "henji", "shoudan", "nokori", "coordinator"} <= set(fleet.agents)
    for spec in fleet.agents.values():
        for expr in spec.schedule:
            CronTrigger.from_crontab(expr, timezone=fleet.timezone)


def test_subscription_auth_drops_api_key(monkeypatch):
    from nakama.runner import resolve_auth

    fleet = load_fleet()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-dummy")
    fleet.auth = "auto"
    assert resolve_auth(fleet) == "subscription"
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ  # otherwise the CLI would bill the API


def test_api_key_auth_and_missing_credentials(monkeypatch):
    import pytest
    from nakama.config import ConfigError
    from nakama.runner import resolve_auth

    fleet = load_fleet()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-dummy")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")  # blank in .env == not set
    assert resolve_auth(fleet) == "api_key"
    fleet.auth = "subscription"
    with pytest.raises(ConfigError):
        resolve_auth(fleet)


def test_system_prompt_includes_common_and_role():
    prompt = build_system_prompt(load_fleet().agents["henji"])
    assert "Nakama 共通ルール" in prompt and "Henji" in prompt
