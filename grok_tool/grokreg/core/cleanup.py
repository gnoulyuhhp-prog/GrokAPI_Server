"""Auto-split from main.py — modular package."""
from __future__ import annotations

import argparse
import asyncio
import email as email_lib
import imaplib
import json
import logging
import os
import random
import re
import string
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests

from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

import grokreg.browser.anti_flag as af
from grokreg.mail.tmail_wibu import TmailWibuProvider
import grokreg.mail.temp_mail_router as tmr
import grokreg.browser.chrome_cleanup as chrome_clean
import grokreg.core.style_log as slog
from grokreg.core.stop_control import (
    StopRequested,
    clear_stop,
    interruptible_sleep,
    is_stop_requested,
    raise_if_stop,
    request_stop,
    sleep_interruptible,
    start_esc_listener,
    stop_reason,
)

from grokreg.core.runtime import (
    ROOT,
    DATA_DIR,
    CONFIG_PATH,
    log,
    MS_CLIENT_IDS,
    FIRST_NAMES,
    LAST_NAMES,
    RATE_LIMIT_PATH,
)


def kill_old_runs(*, also_chrome: bool = True, keep_pid: int | None = None) -> None:
    """
    Stop leftover tool processes so each new run does not pile up RAM.
    Safe: only kills python with main.py/probe_after_otp entrypoints,
    and Chrome identified as tool automation (chrome_runs / debug-port+grok_tool).
    NEVER kills normal user Chrome (User Data profile).
    Never kills keep_pid (current process).
    """
    keep_pid = keep_pid if keep_pid is not None else os.getpid()
    try:
        import subprocess

        # $keep is injected — do not kill current python
        ps = f"""
$keep = {int(keep_pid)}
$n = 0
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -match 'python' -and $_.CommandLine -and (
    $_.CommandLine -match '[\\\\/ ]main\\.py([\"'\\s]|$)' -or
    $_.CommandLine -match '[\\\\/ ]probe_after_otp\\.py([\"'\\s]|$)'
  ) -and $_.ProcessId -ne $keep -and $_.ProcessId -ne $PID
}} | ForEach-Object {{
  try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }} catch {{}}
}}
Write-Output $n
"""
        candidates = [
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "powershell.exe",
            "powershell",
        ]
        exe = next(
            (c for c in candidates if Path(c).exists() or c in ("powershell", "powershell.exe")),
            None,
        )
        if exe:
            from grokreg.core import winhide

            r = subprocess.run(
                [exe, "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=30,
                **winhide.kwargs(),
            )
            n = (r.stdout or "").strip().splitlines()
            killed = n[-1] if n else "?"
            if killed not in ("0", "?", ""):
                try:
                    log.info("Cleaned old main.py runs (killed≈%s)", killed)
                except Exception:
                    print(f"[cleanup] python killed≈{killed}")
        if also_chrome:
            rep = chrome_clean.kill_tool_chrome(reason="kill_old_runs")
            if rep.get("killed_count"):
                log.info(
                    "Tool Chrome cleaned: killed=%s remaining_tool=%s",
                    rep.get("killed_count"),
                    rep.get("remaining_tool"),
                )
    except Exception as e:
        try:
            log.debug("kill_old_runs: %s", e)
        except Exception:
            pass


