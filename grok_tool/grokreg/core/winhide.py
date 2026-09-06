"""Hide Windows console flashes for python.exe / powershell / taskkill children."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_CREATE_NO_WINDOW = 0x08000000
_ROOT = Path(__file__).resolve().parents[2]


def creationflags(extra: int = 0) -> int:
    if os.name != "nt":
        return extra
    return extra | getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)


def startupinfo() -> Any | None:
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def kwargs(*, new_group: bool = False, extra_flags: int = 0) -> dict[str, Any]:
    """Merge into subprocess.Popen/run: ``**winhide.kwargs()``."""
    if os.name != "nt":
        return {}
    flags = creationflags(extra_flags)
    if new_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": flags, "startupinfo": startupinfo()}


def hidden_python(root: Path | None = None) -> Path:
    """Interpreter that does not allocate a Windows console (pythonw)."""
    base = Path(root) if root is not None else _ROOT
    if os.name == "nt":
        for name in ("pythonw.exe", "python.exe"):
            cand = base / "venv" / "Scripts" / name
            if cand.is_file():
                return cand
        exe = Path(sys.executable)
        sibling = exe.with_name("pythonw.exe")
        if sibling.is_file():
            return sibling
        return exe
    nix = base / "venv" / "bin" / "python"
    return nix if nix.is_file() else Path(sys.executable)


def rewrite_python_cmd(cmd: list[str]) -> list[str]:
    """Swap ``python.exe`` → ``pythonw.exe`` when the sibling exists."""
    if os.name != "nt" or not cmd:
        return cmd
    exe = str(cmd[0])
    low = exe.replace("/", "\\").lower()
    if not low.endswith("python.exe"):
        return cmd
    sibling = Path(exe).with_name("pythonw.exe")
    if sibling.is_file():
        return [str(sibling), *cmd[1:]]
    return cmd
