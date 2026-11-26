"""Run one agent once via the Claude Agent SDK."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    HookMatcher,
    ProcessError,
    ResultMessage,
    TextBlock,
    query,
)

from .config import AGENTS_DIR, LOGS_DIR, ROOT, WORKSPACE_DIR, AgentSpec, ConfigError, Fleet, load_fleet, mcp_servers_for
from .guard import make_guard
from .notify import post_to_slack
from .prompt import build_system_prompt, build_user_prompt
from .state import AgentBusy, agent_lock, changed_files, last_success, mark_success, record_cost, spend_on
from .versioning import snapshot_all

log = logging.getLogger(__name__)

BASE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]
WEB_TOOLS = ["WebSearch", "WebFetch"]
SUBAGENT_TOOLS = ["Agent", "Task"]  # "Task" is the older name of the subagent tool


def _tool_lists(spec: AgentSpec, mcp_names: list[str]) -> tuple[list[str], list[str]]:
    """(available built-in tools, pre-approved tools). Anything else is denied by dontAsk mode."""
    builtin = BASE_TOOLS + (WEB_TOOLS if spec.web else []) + (SUBAGENT_TOOLS[:1] if spec.advisor else [])
    allowed = list(builtin) + (SUBAGENT_TOOLS if spec.advisor else []) + [f"mcp__{n}" for n in mcp_names]
    return builtin, sorted(set(allowed))


def build_options(spec: AgentSpec, fleet: Fleet) -> ClaudeAgentOptions:
    mcp = mcp_servers_for(spec, fleet)
    tools, allowed = _tool_lists(spec, list(mcp))

    agents = None
    if spec.advisor:
        advisor_prompt = (AGENTS_DIR / spec.name / "ADVISOR.md").read_text(encoding="utf-8")
        agents = {
            "advisor": AgentDefinition(
                description="難しい判断に直面したときに相談する上位モデルのアドバイザー。PLAN / FIX / STOP を返す。",
                prompt=advisor_prompt,
                tools=["Read", "Glob", "Grep"],
                model=spec.advisor.get("model", "claude-opus-5-5"),
                effort=spec.advisor.get("effort", "high"),
            )
        }

    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(spec),
        model=spec.model,
        effort=spec.effort,
        tools=tools,
        allowed_tools=allowed,
        disallowed_tools=spec.disallowed_tools,
        permission_mode="dontAsk",      # deny anything not pre-approved; no interactive prompts
        cwd=str(ROOT),
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        mcp_servers=mcp,
        strict_mcp_config=True,         # ignore any MCP config on the host
        setting_sources=[],             # ignore ~/.claude and project settings -> reproducible runs
        agents=agents,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_guard(spec)])]},
        stderr=lambda line: log.debug("[cli] %s", line),
    )


def resolve_auth(fleet: Fleet) -> str:
    """Decide which credential the bundled CLI uses and make it unambiguous.

    CLI precedence puts ANTHROPIC_API_KEY above CLAUDE_CODE_OAUTH_TOKEN, so for subscription mode
    the API key must be removed from the environment or every run would be billed to the API.
    """
    for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if not os.environ.get(var, "").strip():
            os.environ.pop(var, None)  # blank values from .env count as "not set"
    has_key = "ANTHROPIC_API_KEY" in os.environ
    has_token = "CLAUDE_CODE_OAUTH_TOKEN" in os.environ

    mode = fleet.auth
    if mode == "auto":
        mode = "subscription" if has_token else "api_key"
    if mode == "subscription":
        if not has_token:
            raise ConfigError("auth: subscription needs CLAUDE_CODE_OAUTH_TOKEN in .env (run `claude setup-token`)")
        os.environ.pop("ANTHROPIC_API_KEY", None)
    elif not has_key:
        raise ConfigError("ANTHROPIC_API_KEY is not set (or set CLAUDE_CODE_OAUTH_TOKEN for subscription auth)")
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        log.warning("ANTHROPIC_AUTH_TOKEN is set and takes precedence over %s", mode)
    return mode


def _to_jsonable(message: object) -> dict:
    data = dataclasses.asdict(message) if dataclasses.is_dataclass(message) else {"repr": repr(message)}
    return {"type": type(message).__name__, **data}


def _action_items() -> set[str]:
    return {p.name for p in (WORKSPACE_DIR / "action_items").glob("*.md")}


async def run_agent(name: str, *, force: bool = False, dry_run: bool = False, extra: str | None = None) -> int:
    """Run one agent once. Returns a process-style exit code (0 = ok or skipped)."""
    fleet = load_fleet()
    if name not in fleet.agents:
        raise ConfigError(f"Unknown agent '{name}'. Defined: {', '.join(fleet.agents)}")
    spec = fleet.agents[name]
    tz = ZoneInfo(fleet.timezone)
    now = datetime.now(tz)

    # ---- Pre-flight checks: skip cheaply before spending any tokens ----
    if not spec.enabled and not force:
        log.info("%s is disabled; skipping", name)
        return 0
    budget = float(fleet.limits.get("daily_budget_usd") or 0)
    spent = sum(spend_on(now.date().isoformat(), tz).values())
    if budget and spent >= budget and not force:
        msg = f"[nakama] 日次予算 ${budget:.2f} に到達（${spent:.2f}）。{name} をスキップしました。"
        log.warning(msg)
        post_to_slack(fleet.notify.get("slack_webhook_env"), msg)
        return 0
    changed = changed_files(spec.trigger_paths, last_success(name))
    if spec.trigger_paths and not changed and not force:
        log.info("%s: no changes in %s since last run; skipping", name, spec.trigger_paths)
        return 0

    # Capture manual LOCAL.md edits as versions before anything runs.
    snapshot_all("manual edit detected before run")
    options = build_options(spec, fleet)
    prompt = build_user_prompt(spec, fleet, now, changed, extra)

    if dry_run:
        print(f"===== SYSTEM PROMPT ({name}) =====\n{options.system_prompt}\n")
        print(f"===== USER PROMPT =====\n{prompt}\n")
        print(f"===== OPTIONS =====\nmodel={options.model} effort={options.effort} tools={options.tools}")
        print(f"allowed={options.allowed_tools} mcp={list(options.mcp_servers)} max_turns={options.max_turns}")
        print(f"max_budget_usd={options.max_budget_usd} subagents={list(options.agents or {})}")
        try:
            print(f"auth={resolve_auth(fleet)}")
        except ConfigError as exc:
            print(f"auth=NOT READY ({exc})")
        return 0

    auth = resolve_auth(fleet)  # raises ConfigError before any tokens are spent

    trace_dir = LOGS_DIR / "runs" / now.date().isoformat()
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{name}-{now.strftime('%H%M%S')}.jsonl"
    items_before = _action_items()
    result: ResultMessage | None = None
    final_text = ""

    try:
        with agent_lock(name), trace_path.open("w", encoding="utf-8") as trace:
            log.info("%s: start (model=%s, effort=%s, auth=%s)", name, spec.model, spec.effort, auth)
            async for message in query(prompt=prompt, options=options):
                trace.write(json.dumps(_to_jsonable(message), ensure_ascii=False, default=str) + "\n")
                if isinstance(message, AssistantMessage):
                    texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                    if texts:
                        final_text = "\n".join(texts)
                elif isinstance(message, ResultMessage):
                    result = message
    except AgentBusy:
        log.warning("%s is already running; skipping", name)
        return 0
    except CLINotFoundError:
        log.error("Claude Code CLI not found. Reinstall claude-agent-sdk (it bundles the CLI).")
        return 2
    except ProcessError as exc:
        # The CLI exits non-zero after an error result (e.g. invalid API key); keep that result if we got it.
        log.error("%s: CLI process exited with %s: %s", name, exc.exit_code, final_text or exc.stderr)
        if result is None:
            return 1
    except ClaudeSDKError as exc:
        log.error("%s: SDK error: %s", name, exc)
        return 1
    finally:
        if name == "coordinator":
            saved = snapshot_all(f"coordinator run {now.isoformat(timespec='minutes')}")
            if saved:
                log.info("new instruction versions: %s", saved)

    # ---- Bookkeeping & notifications ----
    if result is None:
        log.error("%s: no result message received (trace: %s)", name, trace_path)
        return 1
    record_cost(name, now, result.total_cost_usd or 0.0, result.num_turns, result.is_error)
    report = (result.result or final_text or "(no report)").strip()
    # On a subscription the dollar figure is only the CLI's estimate; nothing is billed per run.
    cost_label = f"${result.total_cost_usd or 0:.3f}" + (" est." if auth == "subscription" else "")
    summary = f"{report}\n_({name} / {result.num_turns} turns / {cost_label} / {result.subtype})_"
    (WORKSPACE_DIR / "reports").mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "reports" / f"{now.strftime('%Y-%m-%d-%H%M')}-{name}.md").write_text(summary, encoding="utf-8")

    webhook = fleet.notify.get("slack_webhook_env")
    post_to_slack(webhook, summary)
    mention = fleet.notify.get("action_item_mention")
    for item in sorted(_action_items() - items_before):
        tag = f"<@{mention}> " if mention else ""
        post_to_slack(webhook, f"[action-needed] {tag}`workspace/action_items/{item}` ({name})")

    if result.is_error:
        log.error("%s finished with error: %s %s", name, result.subtype, result.errors or "")
        return 1
    # Use the END time so files the agent itself edited don't re-trigger the next run.
    mark_success(name, datetime.now(tz))
    log.info("%s: done in %d turns, $%.3f", name, result.num_turns, result.total_cost_usd or 0)
    return 0
