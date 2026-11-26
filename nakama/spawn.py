"""Start `python -m nakama run <agent>` as a child process without flashing a console on Windows.

Why: the background service runs under pythonw.exe (no console). When it starts the bundled
Claude CLI (a console program), Windows opens a *new, visible* console window for it.
Running the agent in python.exe with CREATE_NO_WINDOW gives it a hidden console instead,
and the CLI inherits that hidden console.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 0 on macOS / Linux


def console_python() -> str:
    """python.exe next to pythonw.exe (CREATE_NO_WINDOW is ignored for GUI-subsystem pythonw)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def spawn_nakama(args: list[str]) -> subprocess.Popen:
    """Run `python -m nakama <args>` in a hidden-console child (see the module docstring)."""
    return subprocess.Popen(
        [console_python(), "-m", "nakama", *args], cwd=ROOT, env={**os.environ, "NAKAMA_HOME": str(ROOT)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )


def spawn_agent(name: str, force: bool = False, extra_args: list[str] | None = None) -> subprocess.Popen:
    cmd = [console_python(), "-m", "nakama", "run", name] + (["--force"] if force else []) + (extra_args or [])
    return subprocess.Popen(
        cmd, cwd=ROOT, env={**os.environ, "NAKAMA_HOME": str(ROOT)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,  # the child logs to logs/nakama.log itself
    )
