"""
Safe Chrome cleanup for Grok pydoll automation only.

Kills ONLY processes whose CommandLine shows tool markers:
  - --remote-debugging-port=... together with tool profile paths, OR
  - --user-data-dir under D:\\grok_tool\\grok_tool\\chrome_runs (or chrome_profile)
  - path contains chrome_runs / grok_tool\\chrome

NEVER kills normal user Chrome (AppData\\Local\\Google\\Chrome\\User Data without tool markers).
Uses taskkill /T to reap child renderers of matched roots.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from grokreg.core import winhide

log = logging.getLogger("grok-reg")

ROOT = Path(__file__).resolve().parents[2]

# Paths that identify OUR automation Chrome (case-insensitive match on cmdline)
_TOOL_PATH_MARKERS = (
    r"chrome_runs",
    r"grok_tool\\chrome",
    r"grok_tool/chrome",
    r"grok_tool\\grok_tool\\chrome",
    r"d:\\grok_tool\\grok_tool\\chrome",
    r"d:/grok_tool/grok_tool/chrome",
)

# remote-debugging alone is NOT enough (could be another tool) — require tool path
# OR debugging port in tool range with tool path
_DEBUG_PORT_RE = re.compile(r"remote-debugging-port\s*=\s*(\d+)", re.I)
_USER_DATA_RE = re.compile(
    r'user-data-dir(?:=|\s+)(?:"([^"]+)"|(\S+))',
    re.I,
)

# Tool typically uses 9333–9400; still require path marker for safety
_TOOL_PORT_MIN = 9300
_TOOL_PORT_MAX = 9500


def _tool_root_win() -> str:
    try:
        return str(ROOT).replace("/", "\\").lower()
    except Exception:
        return r"d:\grok_tool\grok_tool"


def is_tool_chrome_cmdline(cmd: str | None) -> bool:
    """True if this chrome.exe CommandLine belongs to our automation."""
    if not cmd:
        return False
    c = cmd.replace("/", "\\")
    cl = c.lower()

    # Hard exclude: normal Chrome user profile without any tool marker
    has_user_data = "\\google\\chrome\\user data" in cl or "/google/chrome/user data" in cmd.lower()
    has_tool_path = any(m in cl for m in _TOOL_PATH_MARKERS) or _tool_root_win() in cl

    if has_user_data and not has_tool_path:
        # Normal browser (even if somehow had debug port — still don't kill user data)
        if "chrome_runs" not in cl and "grok_tool" not in cl:
            return False

    # Explicit tool profile dirs
    if "chrome_runs" in cl:
        return True
    if "grok_tool" in cl and ("chrome_profile" in cl or "chrome_runs" in cl):
        return True

    # remote-debugging-port + path under our project
    m = _DEBUG_PORT_RE.search(c)
    if m:
        try:
            port = int(m.group(1))
        except ValueError:
            port = 0
        if has_tool_path:
            return True
        # user-data-dir under ROOT
        um = _USER_DATA_RE.search(c)
        if um:
            ud = (um.group(1) or um.group(2) or "").replace("/", "\\").lower()
            root = _tool_root_win()
            if root in ud or "chrome_runs" in ud:
                return True
        # Only in tool port range AND cmdline mentions grok_tool somewhere
        if _TOOL_PORT_MIN <= port <= _TOOL_PORT_MAX and "grok_tool" in cl:
            return True

    # user-data-dir points into project
    um = _USER_DATA_RE.search(c)
    if um:
        ud = (um.group(1) or um.group(2) or "").replace("/", "\\").lower()
        root = _tool_root_win()
        if "chrome_runs" in ud:
            return True
        if root in ud and ("chrome" in ud):
            return True

    return False


def list_chrome_processes() -> list[dict[str, Any]]:
    """Return list of {pid, cmd} for chrome.exe via PowerShell."""
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  ForEach-Object {
    $c = if ($_.CommandLine) { $_.CommandLine } else { '' }
    # avoid newlines breaking parse
    $c = $c -replace '[\r\n]+',' '
    "{0}`t{1}" -f $_.ProcessId, $c
  }
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=45,
            **winhide.kwargs(),
        )
    except Exception as e:
        log.warning("list_chrome_processes failed: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln or "\t" not in ln:
            continue
        pid_s, cmd = ln.split("\t", 1)
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        out.append({"pid": pid, "cmd": cmd})
    return out


def find_tool_chrome_pids() -> list[dict[str, Any]]:
    return [p for p in list_chrome_processes() if is_tool_chrome_cmdline(p.get("cmd"))]


def _taskkill_tree(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=20,
            **winhide.kwargs(),
        )
        # 0 = success; 128 = not found
        return r.returncode == 0
    except Exception:
        return False


def kill_tool_chrome(*, reason: str = "") -> dict[str, Any]:
    """
    Kill all tool automation Chrome process trees.
    Returns report: {killed: [pid...], remaining_tool: N, total_chrome: N, details: [...]}
    """
    before = find_tool_chrome_pids()
    # Prefer killing "browser" roots first (no --type=) then others
    roots = [p for p in before if "--type=" not in (p.get("cmd") or "")]
    children = [p for p in before if p not in roots]
    order = roots + children

    killed: list[int] = []
    details: list[str] = []
    seen: set[int] = set()
    for p in order:
        pid = int(p["pid"])
        if pid in seen:
            continue
        cmd = (p.get("cmd") or "")[:160]
        ok = _taskkill_tree(pid)
        seen.add(pid)
        if ok:
            killed.append(pid)
            details.append(f"killed pid={pid} {cmd}")
        else:
            details.append(f"skip/fail pid={pid} {cmd}")

    # Second pass: any survivors still matching
    survivors = find_tool_chrome_pids()
    for p in survivors:
        pid = int(p["pid"])
        if pid in seen:
            continue
        if _taskkill_tree(pid):
            killed.append(pid)
            seen.add(pid)
            details.append(f"killed-survivor pid={pid}")

    remaining = find_tool_chrome_pids()
    total = len(list_chrome_processes())
    report = {
        "reason": reason,
        "matched_before": len(before),
        "killed": killed,
        "killed_count": len(killed),
        "remaining_tool": len(remaining),
        "remaining_tool_pids": [p["pid"] for p in remaining],
        "total_chrome": total,
        "details": details,
    }
    if killed or before:
        log.info(
            "Chrome cleanup (%s): matched=%s killed=%s remaining_tool=%s total_chrome=%s",
            reason or "n/a",
            len(before),
            len(killed),
            len(remaining),
            total,
        )
    return report


def format_report(rep: dict[str, Any]) -> str:
    lines = [
        f"reason={rep.get('reason')}",
        f"matched_before={rep.get('matched_before')}",
        f"killed_count={rep.get('killed_count')} pids={rep.get('killed')}",
        f"remaining_tool={rep.get('remaining_tool')} pids={rep.get('remaining_tool_pids')}",
        f"total_chrome_now={rep.get('total_chrome')}",
    ]
    for d in (rep.get("details") or [])[:20]:
        lines.append(f"  {d}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = kill_tool_chrome(reason="cli")
    print(format_report(r))
    sys.exit(0 if r.get("remaining_tool", 0) == 0 else 1)
