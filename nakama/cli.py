"""Command line entry point: `python -m nakama <command>`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import (
    AGENTS_DIR, CONFIG_DIR, KNOWLEDGE_DIR, LOGS_DIR, ROOT, SAMPLES_DIR, TEMPLATES_DIR, WORKSPACE_DIR,
    ConfigError, load_fleet,
)


def _copy_if_missing(src, dst) -> bool:
    if dst.exists() or not src.exists():  # src may be absent inside the container (.env.example)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def cmd_init(args: argparse.Namespace) -> int:
    created = []
    for example in CONFIG_DIR.glob("*.example.*"):
        target = example.with_name(example.name.replace(".example", ""))
        if _copy_if_missing(example, target):
            created.append(target)
    for tmpl in (TEMPLATES_DIR / "knowledge").glob("*.md"):
        if _copy_if_missing(tmpl, KNOWLEDGE_DIR / tmpl.name):
            created.append(KNOWLEDGE_DIR / tmpl.name)
    env_example = ROOT / ".env.example"
    if _copy_if_missing(env_example, ROOT / ".env"):
        created.append(ROOT / ".env")
    for agent_dir in AGENTS_DIR.iterdir():
        local = agent_dir / "LOCAL.md"
        if agent_dir.is_dir() and not agent_dir.name.startswith("_") and not local.exists():
            local.write_text("<!-- 個人用チューニング。Coordinator もここを編集する（git管理外） -->\n", encoding="utf-8")
            created.append(local)

    if args.demo:
        today = datetime.now(ZoneInfo(load_fleet().timezone)).date().isoformat()
        for src in SAMPLES_DIR.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(SAMPLES_DIR)
            if rel.parts[0] == "calendar":  # meetings land in today's folder
                dst = WORKSPACE_DIR / "calendar" / today / rel.name
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(src.read_text(encoding="utf-8").replace("{{DATE}}", today), encoding="utf-8")
                    created.append(dst)
            elif _copy_if_missing(src, WORKSPACE_DIR / rel):
                created.append(WORKSPACE_DIR / rel)

    for path in created:
        print(f"created {path.relative_to(ROOT).as_posix()}")
    print("init done." + ("" if created else " (nothing to do)"))
    if (ROOT / ".env").exists():
        print("next: set ANTHROPIC_API_KEY (API billing) or CLAUDE_CODE_OAUTH_TOKEN (subscription) in .env")
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    fleet = load_fleet()
    print(f"config: {fleet.source.relative_to(ROOT).as_posix()}  timezone: {fleet.timezone}")
    print(f"{'agent':<12} {'on':<3} {'model':<18} {'effort':<7} schedule")
    for spec in fleet.agents.values():
        on = "yes" if spec.enabled else "no"
        print(f"{spec.name:<12} {on:<3} {spec.model:<18} {spec.effort or '-':<7} {', '.join(spec.schedule)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    names = list(load_fleet().agents) if args.agent == "all" else [args.agent]
    code = 0
    for name in names:
        code = max(code, asyncio.run(run_agent_lazy(name, args)))
    return code


async def run_agent_lazy(name: str, args: argparse.Namespace) -> int:
    from .runner import run_agent  # import here so `init`/`list` work before SDK install

    return await run_agent(name, force=args.force, dry_run=args.dry_run, extra=args.extra,
                           review=getattr(args, "review", False))


def cmd_serve(args: argparse.Namespace) -> int:
    from .scheduler import serve

    if args.ui:
        from .ui import serve_ui

        try:
            serve_ui(args.host, args.port, background=True, open_browser=args.open)  # thread next to the scheduler
        except OSError as exc:  # e.g. port already in use: keep the scheduler running without the UI
            logging.getLogger(__name__).error("UI could not start on port %d: %s", args.port, exc)
    serve()
    return 0


def cmd_devbox(args: argparse.Namespace) -> int:
    from .devbox import serve_forever

    serve_forever()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import serve_ui

    try:
        serve_ui(args.host, args.port, open_browser=args.open)
    except OSError as exc:
        print(f"port {args.port} is in use ({exc}). Another Nakama UI may be running "
              f"(e.g. the scheduled task) - open http://localhost:{args.port}/ or use --port.", file=sys.stderr)
        return 1
    return 0


def _add_ui_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 inside Docker)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open the dashboard in the default browser")


def cmd_run_all(args: argparse.Namespace) -> int:
    """Run the named agents one after another with the normal checks (no --force)."""
    code = 0
    for name in args.agents:
        ns = argparse.Namespace(force=False, dry_run=False, extra=None, review=False)
        code = max(code, asyncio.run(run_agent_lazy(name, ns)))
    return code


def cmd_chat_turn(args: argparse.Namespace) -> int:
    from .chat import run_turn

    return run_turn(args.chat_id)


def cmd_request(args: argparse.Namespace) -> int:
    """Drop a change request for the coordinator (local stand-in for the Slack channel)."""
    now = datetime.now(ZoneInfo(load_fleet().timezone))
    path = WORKSPACE_DIR / "requests" / f"{now.strftime('%Y%m%d-%H%M%S')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: open\ncreated: {now.isoformat(timespec='minutes')}\n---\n{args.text}\n", encoding="utf-8")
    print(f"created {path.relative_to(ROOT).as_posix()} (coordinator picks it up on its next run)")
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    from .state import spend_on

    fleet = load_fleet()
    tz = ZoneInfo(fleet.timezone)
    day = args.date or datetime.now(tz).date().isoformat()
    totals = spend_on(day, tz)
    for agent, usd in sorted(totals.items()):
        print(f"{agent:<12} ${usd:.3f}")
    print(f"{'total':<12} ${sum(totals.values()):.3f}  (budget ${fleet.limits.get('daily_budget_usd', 0)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nakama", description="Nakama: a self-hosted fleet of Claude agents")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create config/knowledge from templates")
    p.add_argument("--demo", action="store_true", help="also copy sample data into workspace/")
    p.set_defaults(func=cmd_init)

    sub.add_parser("list", help="show agents and schedules").set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="run one agent (or 'all') once now")
    p.add_argument("agent")
    p.add_argument("--force", action="store_true", help="ignore enabled/trigger/budget checks")
    p.add_argument("--dry-run", action="store_true", help="print prompts and options; no API call")
    p.add_argument("--extra", help="one-off extra instruction for this run")
    p.add_argument("--review", action="store_true",
                   help="review mode: no promote/archive tools, no writes outside workspace/")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="start the scheduler (foreground)")
    p.add_argument("--ui", action="store_true", help="also serve the web dashboard")
    _add_ui_args(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ui", help="serve only the web dashboard (http://localhost:8765)")
    _add_ui_args(p)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("run-all", help="(internal) run several agents in sequence with normal checks")
    p.add_argument("agents", nargs="+")
    p.set_defaults(func=cmd_run_all)

    p = sub.add_parser("chat-turn", help="(internal) answer the pending message of a dashboard chat")
    p.add_argument("chat_id")
    p.set_defaults(func=cmd_chat_turn)

    p = sub.add_parser("request", help="queue a change request for the coordinator")
    p.add_argument("text")
    p.set_defaults(func=cmd_request)

    p = sub.add_parser("devbox", help="(devbox container) implement 着手OK requests and open draft PRs")
    p.set_defaults(func=cmd_devbox)

    p = sub.add_parser("costs", help="show spend for a day")
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.set_defaults(func=cmd_costs)

    args = parser.parse_args(argv)
    # Windows consoles default to cp932 etc.; prompts are Japanese, so force UTF-8 output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOGS_DIR / "nakama.log", encoding="utf-8")],
    )
    _load_dotenv()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


def _load_dotenv() -> None:
    """Load .env when running natively (docker compose injects env itself)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)
