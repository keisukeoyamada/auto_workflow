"""Numbered version history for each agent's personal layer (LOCAL.md).

Mirrors Managed Agents' "every instruction change is a new version":
whenever LOCAL.md differs from the latest snapshot, a new vNNN.md is saved.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from .config import AGENTS_DIR


def _versions_dir(agent: str) -> Path:
    return AGENTS_DIR / agent / "versions"


def version_path(agent: str, number: int) -> Path:
    return _versions_dir(agent) / f"v{number:03d}.md"


def list_versions(agent: str) -> list[int]:
    vdir = _versions_dir(agent)
    if not vdir.exists():
        return []
    return sorted(int(p.stem[1:]) for p in vdir.glob("v[0-9][0-9][0-9].md"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(agent: str, reason: str) -> int | None:
    """Save LOCAL.md as a new version if it changed. Returns the new version number."""
    local = AGENTS_DIR / agent / "LOCAL.md"
    if not local.exists():
        return None
    content = local.read_text(encoding="utf-8")
    versions = list_versions(agent)
    if versions and _digest(version_path(agent, versions[-1]).read_text(encoding="utf-8")) == _digest(content):
        return None  # unchanged

    number = (versions[-1] + 1) if versions else 1
    _versions_dir(agent).mkdir(parents=True, exist_ok=True)
    version_path(agent, number).write_text(content, encoding="utf-8")
    with (_versions_dir(agent) / "CHANGELOG.md").open("a", encoding="utf-8") as log:
        log.write(f"- v{number:03d} {datetime.now().isoformat(timespec='seconds')} {reason}\n")
    return number


def snapshot_all(reason: str) -> dict[str, int]:
    """Snapshot every agent that has a LOCAL.md; return {agent: new_version}."""
    saved = {}
    for agent_dir in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")):
        number = snapshot(agent_dir.name, reason)
        if number is not None:
            saved[agent_dir.name] = number
    return saved
