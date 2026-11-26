"""PreToolUse hook that sandboxes file access per agent.

- Reads/searches are limited to agents/, knowledge/, workspace/ (never .env or config/).
- Writes are limited to the agent's `writes` list in fleet.yaml.
This is the local stand-in for Managed Agents' scoped credential vaults.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AGENTS_DIR, KNOWLEDGE_DIR, ROOT, WORKSPACE_DIR, AgentSpec

READ_ROOTS = (AGENTS_DIR, KNOWLEDGE_DIR, WORKSPACE_DIR)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep"}


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _within(path: Path, roots: tuple[Path, ...] | list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def check_tool_call(spec: AgentSpec, tool: str, tool_input: dict[str, Any],
                    extra_read: tuple[Path, ...] = (), extra_write: tuple[Path, ...] = ()) -> str | None:
    """Return a denial reason, or None if the call is allowed. Pure function for testing.

    extra_read / extra_write: folders outside the repo granted by a helper (e.g. a synced ticket folder).
    """
    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not raw:
            return "file_path がありません"
        target = _resolve(raw)
        write_roots = [_resolve(w) for w in spec.writes] + [p.resolve() for p in extra_write]
        if not _within(target, write_roots):
            return f"{target} は書き込み可能パス外です: {spec.writes}"
        return None

    if tool in READ_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("path")
        if not raw:
            # Glob/Grep without path would search the repo root (incl. .env)
            return "path を agents/・knowledge/・workspace/ 配下で明示してください"
        if not _within(_resolve(raw), READ_ROOTS + tuple(p.resolve() for p in extra_read)):
            return f"{raw} は読み取り可能範囲外です（agents/, knowledge/, workspace/ と許可された外部フォルダのみ）"
    return None


def make_guard(spec: AgentSpec, extra_read: tuple[Path, ...] = (), extra_write: tuple[Path, ...] = ()):
    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check_tool_call(spec, input_data.get("tool_name", ""), input_data.get("tool_input") or {},
                                 extra_read, extra_write)
        return _deny(reason) if reason else {}

    return guard
