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


from grokreg.core.config import load_config
from grokreg.browser.jsutil import _exec_js, _unwrap_js_result  # noqa: F401

def steal_focus_allowed(config: dict[str, Any] | None = None) -> bool:
    """False = never pop Chrome in front of user's work."""
    cfg = config or {}
    if cfg.get("chrome_steal_focus") is False:
        return False
    if cfg.get("headless"):
        return False
    mode = str(cfg.get("chrome_window_mode") or "").strip().lower()
    if mode in ("offscreen", "minimized", "background", "hidden"):
        return False
    if cfg.get("chrome_background"):
        return False
    return True


async def maybe_bring_to_front(tab: Any, config: dict[str, Any] | None = None) -> None:
    if not steal_focus_allowed(config):
        return
    try:
        if hasattr(tab, "bring_to_front"):
            await tab.bring_to_front()
    except Exception:
        pass


def _safe_add_arg(options: ChromiumOptions, arg: str) -> None:
    try:
        options.add_argument(arg)
    except Exception:
        # pydoll raises if argument already present (defaults include --no-first-run, etc.)
        pass


def build_chrome_options(
    config: dict[str, Any],
    *,
    fingerprint: dict[str, Any] | None = None,
    profile_dir: Path | None = None,
) -> ChromiumOptions:
    """
    Build Chrome options with per-run fingerprint + isolated profile.
    Default (anti-flag): fresh clean user-data-dir every account.
    """
    options = ChromiumOptions()
    fp = fingerprint or af.pick_fingerprint(config)
    config["_fingerprint"] = fp

    # Fingerprint CLI args + profile prefs (age, locale, webrtc prefs…)
    af.apply_chrome_fingerprint_args(options, fp, _safe_add_arg)
    antiflag_cfg = (
        config.get("antiflag") if isinstance(config.get("antiflag"), dict) else {}
    )
    if antiflag_cfg.get("browser_preferences", True):
        af.apply_browser_preferences(options, fp)

    # Isolated profile every account (fresh_profile_per_account=true)
    fresh = bool(config.get("fresh_profile_per_account", True))
    reuse = bool(config.get("reuse_chrome_profile", False)) and not fresh

    if profile_dir is not None:
        profile = Path(profile_dir)
    elif fresh:
        profile = af.fresh_profile_dir(ROOT / "chrome_runs")
    else:
        profile = Path(str(config.get("chrome_user_data_dir") or "chrome_profile"))
        if not profile.is_absolute():
            profile = ROOT / profile
    profile.mkdir(parents=True, exist_ok=True)
    config["_profile_dir"] = str(profile)
    profile_arg = af.to_windows_path(profile)
    _safe_add_arg(options, f"--user-data-dir={profile_arg}")

    # Proxy / headless
    proxy = (config.get("proxy") or "").strip()
    if proxy:
        _safe_add_arg(options, f"--proxy-server={proxy}")
        log.info("Proxy: %s", proxy)

    if config.get("headless"):
        _safe_add_arg(options, "--headless=new")
        # pydoll may also set --headless; _safe_add_arg swallows dupes
        _safe_add_arg(options, "--headless")

    # pydoll: enables --force-webrtc-ip-handling-policy=disable_non_proxied_udp
    # (also set via apply_chrome_fingerprint_args; setter is idempotent)
    try:
        options.webrtc_leak_protection = True
    except Exception as e:
        log.debug("webrtc_leak_protection: %s", e)
        _safe_add_arg(
            options, "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
        )

    # Window modes (Lygaz-style recommended):
    #   lygaz / visible / normal — start ON-SCREEN for CF, pull back after CF/done
    #   minimized / offscreen / background — park off-screen from start (harder CF)
    win_mode = str(
        config.get("chrome_window_mode")
        or ("lygaz" if not config.get("chrome_background", False) else "minimized")
    ).lower()
    if not config.get("headless"):
        if win_mode in ("minimized", "background", "offscreen"):
            pos = str(config.get("chrome_window_position") or "-1600,40")
            _safe_add_arg(options, f"--window-position={pos}")
            log.info(
                "Chrome UI mode=%s position=%s (parked from start)",
                win_mode,
                pos,
            )
        elif win_mode in ("lygaz", "visible", "normal"):
            # Start on primary screen so Cloudflare Turnstile can render/solve
            log.info(
                "Chrome UI mode=%s (VISIBLE for CF → kéo về after CF/done)",
                win_mode,
            )

    log.info(
        "Chrome profile (%s): %s | UA=...%s | %sx%s | tz=%s | webrtc_protect=on",
        "FRESH" if fresh or not reuse else "reuse",
        profile_arg,
        str(fp.get("user_agent", "") or "")[-28:],
        fp.get("width"),
        fp.get("height"),
        fp.get("timezone"),
    )
    return options


def chrome_debug_port(config: dict[str, Any]) -> int:
    return int(config.get("chrome_debug_port") or 9333)


def probe_cdp_ws(port: int) -> Optional[str]:
    """If Chrome already listening on CDP port, return browser WebSocket URL."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        if r.status_code != 200:
            return None
        data = r.json()
        ws = data.get("webSocketDebuggerUrl")
        if ws:
            log.info("Found live Chrome CDP on :%s", port)
            return str(ws)
    except Exception:
        pass
    return None


def list_cdp_pages(port: int) -> list[dict[str, Any]]:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _parse_window_pos(config: dict[str, Any]) -> tuple[int, int]:
    raw = str(config.get("chrome_window_position") or "-1600,40")
    try:
        parts = raw.replace(" ", "").split(",")
        return int(parts[0]), int(parts[1] if len(parts) > 1 else 40)
    except Exception:
        return -1600, 40


def minimize_automation_chrome(config: dict[str, Any] | None = None) -> None:
    """
    Minimize Chrome windows used by this tool WITHOUT activating them.
    So reg popups don't cover the user's work tabs.
    Targets: chrome with remote-debugging-port + (grok_tool|chrome_runs).
    """
    config = config or {}
    win_mode = str(
        config.get("chrome_window_mode")
        or ("minimized" if config.get("chrome_background", True) else "normal")
    ).lower()
    # lygaz / normal stay visible during CF — only minimize when mode asks
    if config.get("headless") or win_mode in ("normal", "lygaz", "visible"):
        return
    # SW_SHOWMINNOACTIVE = 7 — minimize without stealing focus
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class GrokWin {
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
"@
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
  $_.CommandLine -and (
    $_.CommandLine -match 'remote-debugging-port' -and
    ($_.CommandLine -match 'grok_tool|chrome_runs|chrome_profile')
  )
} | ForEach-Object {
  $proc = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
  if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {
    [GrokWin]::ShowWindowAsync($proc.MainWindowHandle, 7) | Out-Null
  }
}
"""
    try:
        from grokreg.core import winhide

        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=8,
            **winhide.kwargs(),
        )
        log.info("Chrome automation windows minimized (no focus steal)")
    except Exception as e:
        log.debug("minimize chrome skip: %s", e)


def pull_back_automation_chrome(
    config: dict[str, Any] | None = None, *, reason: str = ""
) -> None:
    """
    Lygaz style: after CF / after reg done → kéo cửa sổ tool Chrome về off-screen
    rồi minimize (không focus). Giảm CF flag từ nhiều cửa sổ chồng trên màn hình.

    Targets: chrome with remote-debugging-port + (grok_tool|chrome_runs|chrome_profile).
    """
    config = config or {}
    if config.get("headless"):
        return
    x, y = _parse_window_pos(config)
    # SWP_NOSIZE=0x0001 SWP_NOZORDER=0x0004 SWP_NOACTIVATE=0x0010
    # SW_SHOWMINNOACTIVE = 7
    ps = f"""
$ErrorActionPreference='SilentlyContinue'
$x = {int(x)}; $y = {int(y)}
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class GrokWinPull {{
  [DllImport("user32.dll")] public static extern bool SetWindowPos(
    IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}}
"@
$flags = [uint32]0x0015  # NOSIZE|NOZORDER|NOACTIVATE
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {{
  $_.CommandLine -and (
    $_.CommandLine -match 'remote-debugging-port' -and
    ($_.CommandLine -match 'grok_tool|chrome_runs|chrome_profile')
  )
}} | ForEach-Object {{
  $proc = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
  if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {{
    $h = $proc.MainWindowHandle
    [GrokWinPull]::SetWindowPos($h, [IntPtr]::Zero, $x, $y, 0, 0, $flags) | Out-Null
    [GrokWinPull]::ShowWindowAsync($h, 7) | Out-Null
  }}
}}
"""
    try:
        from grokreg.core import winhide

        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=10,
            **winhide.kwargs(),
        )
        log.info(
            "Chrome kéo về off-screen (%s,%s)%s",
            x,
            y,
            f" [{reason}]" if reason else "",
        )
    except Exception as e:
        log.debug("pull_back chrome skip: %s", e)


@dataclass
class BrowserHandle:
    browser: Any
    tab: Any
    attached: bool  # connected to already-running Chrome
    port: int
    keep_open: bool


async def _cf_page_state(tab: Any) -> str:
    """
    Return: clear | challenge | failed | unknown
    'failed' = Cloudflare error page (verification can fail / troubleshoot).
    """
    try:
        st = await _exec_js(
            tab,
            """
            (() => {
              const t = (document.body && document.body.innerText || '').toLowerCase();
              const title = (document.title || '').toLowerCase();
              const href = (location.href || '').toLowerCase();
              // Hard CF failure page (user-reported)
              if (t.includes('this verification can fail') ||
                  t.includes('not solely due to bot activity') ||
                  t.includes('submit feedback to cloudflare') ||
                  t.includes('troubleshooting documentation') ||
                  (t.includes('troubleshoot') && t.includes('cloudflare'))) {
                return 'failed';
              }
              if (title.includes('just a moment') || title.includes('attention required') ||
                  title.includes('failed') && href.includes('cloudflare')) {
                if (t.includes('verify') || t.includes('checking')) return 'challenge';
              }
              if (t.includes('verify you are human') || t.includes('checking your browser') ||
                  t.includes('confirm you are human') || t.includes('needs to review the security')) {
                return 'challenge';
              }
              // interstitial / managed Turnstile widgets
              const widget = document.querySelector(
                '#challenge-running, #cf-challenge-running, .cf-turnstile, [data-sitekey], iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
              );
              const tokEl = document.querySelector(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
              );
              const tok = tokEl ? (tokEl.value || '').trim() : '';
              // Widget visible with empty token = still need human check
              if (widget) {
                if (!tok || tok.length < 20) return 'challenge';
              }
              // empty token field alone while challenge text present
              if (t.includes('verify you are human') || t.includes('confirm you are human')) {
                if (!tok || tok.length < 20) return 'challenge';
              }
              // success signals for xAI signup (only if not stuck on turnstile)
              if (t.includes('sign up') || t.includes('create your account') ||
                  document.querySelector('input[type=email], input[name=email]')) {
                return 'clear';
              }
              return 'unknown';
            })()
            """,
        )
        return str(st or "unknown")
    except Exception:
        return "unknown"


async def _cf_still_blocking(tab: Any) -> bool:
    """True if Cloudflare / Turnstile challenge still visible or failed."""
    st = await _cf_page_state(tab)
    return st in ("challenge", "failed")


async def _turnstile_widget_info(tab: Any) -> dict[str, Any]:
    """Locate Turnstile/CF checkbox widget + token state (page-level)."""
    raw = await _exec_js(
        tab,
        """
        (() => {
          const tok = document.querySelector(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
          );
          const token = tok ? (tok.value || '').trim() : '';
          // Prefer explicit turnstile host / iframe
          const cands = [];
          document.querySelectorAll(
            'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"], .cf-turnstile, [data-sitekey], #cf-challenge-running, #challenge-stage'
          ).forEach((el, i) => {
            const r = el.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) return;
            cands.push({
              i, tag: el.tagName, id: el.id || '',
              src: (el.src || el.getAttribute('src') || '').slice(0, 120),
              cls: (el.className || '').toString().slice(0, 60),
              x: r.left, y: r.top, w: r.width, h: r.height,
              // checkbox is usually left side of the widget
              cx: r.left + Math.min(28, r.width * 0.15),
              cy: r.top + r.height / 2
            });
          });
          // text labels "Verify you are human"
          let labelRect = null;
          for (const n of document.querySelectorAll('label, span, div')) {
            const t = (n.innerText || n.textContent || '').trim().toLowerCase();
            if (!t || t.length > 40) continue;
            if (t.includes('verify you are human') || t.includes('i am human')
                || t.includes('xác nhận bạn là người') || t.includes('la nguoi')) {
              const r = n.getBoundingClientRect();
              if (r.width > 4 && r.height > 4) {
                labelRect = {x:r.left, y:r.top, w:r.width, h:r.height,
                  cx: r.left + 14, cy: r.top + r.height/2, text: t.slice(0,40)};
                break;
              }
            }
          }
          const body = ((document.body && document.body.innerText) || '').toLowerCase();
          return {
            tokenLen: token.length,
            tokenReady: token.length > 20,
            widgets: cands.slice(0, 8),
            label: labelRect,
            challengeText: body.includes('verify you are human')
              || body.includes('checking your browser')
              || body.includes('confirm you are human')
              || body.includes('just a moment')
          };
        })()
        """,
    )
    return raw if isinstance(raw, dict) else {}


async def _cdp_click_xy(tab: Any, x: float, y: float) -> bool:
    """Click page coordinates via pydoll mouse or CDP Input."""
    try:
        if hasattr(tab, "mouse") and hasattr(tab.mouse, "click"):
            try:
                await tab.mouse.click(int(x), int(y), humanize=True)
            except TypeError:
                await tab.mouse.click(int(x), int(y))
            return True
    except Exception as e:
        log.debug("mouse.click: %s", e)
    try:
        from pydoll.commands.input_commands import InputCommands
        from pydoll.protocol.input.types import MouseButton, MouseEventType

        await tab._execute_command(
            InputCommands.dispatch_mouse_event(
                type=MouseEventType.MOUSE_MOVED, x=x, y=y
            )
        )
        await asyncio.sleep(0.05)
        await tab._execute_command(
            InputCommands.dispatch_mouse_event(
                type=MouseEventType.MOUSE_PRESSED,
                x=x,
                y=y,
                button=MouseButton.LEFT,
                click_count=1,
            )
        )
        await asyncio.sleep(0.05)
        await tab._execute_command(
            InputCommands.dispatch_mouse_event(
                type=MouseEventType.MOUSE_RELEASED,
                x=x,
                y=y,
                button=MouseButton.LEFT,
                click_count=1,
            )
        )
        return True
    except Exception as e:
        log.debug("CDP mouse: %s", e)
        return False


async def click_turnstile_checkbox_robust(
    tab: Any,
    *,
    wait_sec: float = 40.0,
    reason: str = "",
    config: dict[str, Any] | None = None,
) -> bool:
    """
    Actually tick Cloudflare Turnstile 'Verify you are human'.

    Strategy order (grok-register-web style first):
      0) External Camoufox/YesCaptcha solver → inject token
      1) pydoll _bypass_cloudflare (shadow root)
      2) coordinate click on iframe/widget
      3) label click
      4) wait for cf-turnstile-response token
    """
    wait_sec = max(15.0, float(wait_sec))
    log.info(
        "CF/Turnstile robust click start wait=%.0fs%s",
        wait_sec,
        f" [{reason}]" if reason else "",
    )
    await maybe_bring_to_front(tab, config)

    # --- Strategy 0: external solver (Camoufox from zip / YesCaptcha) ---
    cfg = config or {}
    try:
        # Prefer global last config if caller didn't pass
        if not cfg and hasattr(click_turnstile_checkbox_robust, "_cfg"):
            cfg = getattr(click_turnstile_checkbox_robust, "_cfg") or {}
    except Exception:
        pass
    try:
        from grokreg.captcha.turnstile_solver_client import solve_and_inject_turnstile

        info0 = await _turnstile_widget_info(tab)
        if not info0.get("tokenReady"):
            ok_ext = await solve_and_inject_turnstile(
                tab, cfg, reason=reason or "robust"
            )
            if ok_ext:
                info0 = await _turnstile_widget_info(tab)
                if info0.get("tokenReady") or ok_ext:
                    log.info("Turnstile ready via EXTERNAL solver%s", f" [{reason}]" if reason else "")
                    return True
    except Exception as e:
        log.warning("external turnstile solver: %s", e)

    # Enable pydoll auto-solve (callback on load — may miss if already loaded)
    try:
        if hasattr(tab, "enable_auto_solve_cloudflare_captcha"):
            await tab.enable_auto_solve_cloudflare_captcha(
                time_to_wait_captcha=min(20.0, wait_sec)
            )
    except Exception as e:
        log.debug("enable_auto_solve: %s", e)

    deadline = time.time() + wait_sec
    pydoll_tries = 0
    coord_tries = 0
    external_retry = 0

    while time.time() < deadline:
        info = await _turnstile_widget_info(tab)
        if info.get("tokenReady"):
            log.info("Turnstile token already ready (len=%s)", info.get("tokenLen"))
            return True

        st = await _cf_page_state(tab)
        if st == "failed":
            log.warning("CF FAILURE page — stop clicking")
            return False

        # Retry external once mid-loop if still stuck
        if external_retry < 1 and (time.time() + wait_sec / 2) < deadline:
            external_retry += 1
            try:
                from grokreg.captcha.turnstile_solver_client import solve_and_inject_turnstile

                if await solve_and_inject_turnstile(tab, cfg, reason=f"{reason}:retry"):
                    if (await _turnstile_widget_info(tab)).get("tokenReady"):
                        return True
            except Exception:
                pass

        # Strategy 1: pydoll shadow traversal (works when widget is classic Turnstile)
        if pydoll_tries < 4:
            pydoll_tries += 1
            log.info("CF try pydoll _bypass_cloudflare #%s", pydoll_tries)
            try:
                if hasattr(tab, "_bypass_cloudflare"):
                    await tab._bypass_cloudflare({}, time_to_wait_captcha=8.0)
            except Exception as e:
                log.warning("pydoll CF bypass err: %s", e)
            await asyncio.sleep(1.5)
            info = await _turnstile_widget_info(tab)
            if info.get("tokenReady"):
                log.info("Turnstile ready after pydoll bypass")
                return True

        # Strategy 2: coordinate click on widget (checkbox left side)
        widgets = info.get("widgets") or []
        if widgets and coord_tries < 6:
            w = widgets[0]
            cx, cy = float(w.get("cx") or 0), float(w.get("cy") or 0)
            if cx > 0 and cy > 0:
                coord_tries += 1
                log.info(
                    "CF coord click #%s on widget %sx%s @ (%.0f,%.0f) src=%s",
                    coord_tries,
                    w.get("w"),
                    w.get("h"),
                    cx,
                    cy,
                    str(w.get("src") or "")[:60],
                )
                # slight jitter
                jx = cx + random.uniform(-3, 3)
                jy = cy + random.uniform(-3, 3)
                ok = await _cdp_click_xy(tab, jx, jy)
                if not ok:
                    # JS click at element center as last resort (may not pierce iframe)
                    await _exec_js(
                        tab,
                        f"""
                        (() => {{
                          const el = document.elementFromPoint({int(jx)}, {int(jy)});
                          if (el) {{ el.click(); return el.tagName; }}
                          return null;
                        }})()
                        """,
                    )
                await asyncio.sleep(random.uniform(2.0, 4.0))
                info = await _turnstile_widget_info(tab)
                if info.get("tokenReady"):
                    log.info("Turnstile ready after coord click")
                    return True

        # Strategy 3: label "Verify you are human"
        lab = info.get("label")
        if isinstance(lab, dict) and lab.get("cx"):
            log.info("CF click label %r", lab.get("text"))
            await _cdp_click_xy(tab, float(lab["cx"]), float(lab["cy"]))
            await asyncio.sleep(2.0)
            info = await _turnstile_widget_info(tab)
            if info.get("tokenReady"):
                return True

        # If no widget and not challenge text — maybe already clear
        if (
            not widgets
            and not info.get("challengeText")
            and st == "clear"
            and coord_tries >= 1
        ):
            log.info("CF looks clear (no widget, state=clear)")
            return True
        if not widgets and not info.get("challengeText") and st == "clear" and pydoll_tries >= 2:
            # wait a bit for late widget inject
            await asyncio.sleep(1.0)
            info2 = await _turnstile_widget_info(tab)
            if not info2.get("widgets") and not info2.get("challengeText"):
                log.info("CF clear — no turnstile widget on page")
                return True

        await asyncio.sleep(1.0)

    info = await _turnstile_widget_info(tab)
    st = await _cf_page_state(tab)
    log.warning(
        "CF/Turnstile robust click TIMEOUT tokenReady=%s state=%s widgets=%s",
        info.get("tokenReady"),
        st,
        len(info.get("widgets") or []),
    )
    return bool(info.get("tokenReady") or st == "clear")


async def force_click_cloudflare_checkbox(tab: Any, wait_sec: float = 35.0) -> bool:
    """
    Soft CF solve — prefer robust multi-strategy click (pydoll alone often fails
    with 'The specified element was not found' on xAI Turnstile).
    """
    return await click_turnstile_checkbox_robust(
        tab, wait_sec=max(20.0, float(wait_sec)), reason="force_click"
    )


async def _post_cf_session_harden(tab: Any, config: dict[str, Any]) -> None:
    """
    After CF is clear (never before — stealth breaks Turnstile):
      - full navigator/WebGL/Audio spoof when stealth_inject or patch_* on
      - else minimal hide-webdriver
      - CDP timezone only when fingerprint says OS≠IP
    Do NOT clear cookies/storage here — would drop CF clearance / break xAI client state.

    post_cf_stealth_mode:
      - "full"    → spoof hw/webgl/audio (default if patch_* on)
      - "minimal" → only hide webdriver (safer for xAI API; heavy spoof can trigger error_generic)
    """
    antiflag = config.get("antiflag") if isinstance(config.get("antiflag"), dict) else {}
    fp = config.get("_fingerprint") if isinstance(config.get("_fingerprint"), dict) else {}
    mode = str(
        config.get("post_cf_stealth_mode")
        or antiflag.get("post_cf_stealth_mode")
        or ""
    ).lower().strip()

    want_stealth = bool(antiflag.get("stealth_inject", False)) or bool(
        antiflag.get("hide_webdriver", True)
    ) or bool(
        antiflag.get("patch_hardware")
        or antiflag.get("patch_webgl")
        or antiflag.get("patch_audio")
    )
    if want_stealth and fp:
        try:
            fp_use = dict(fp)
            full = bool(
                antiflag.get("stealth_inject", False)
                or antiflag.get("patch_hardware")
                or antiflag.get("patch_webgl")
                or antiflag.get("patch_audio")
                or antiflag.get("patch_canvas")
            )
            # Force minimal when configured (reduces server-side fingerprint rejects)
            if mode in ("minimal", "light", "safe"):
                full = False
                log.info("Post-CF stealth mode=minimal (no webgl/audio/hw spoof)")
            if full:
                fp_use["stealth_full"] = True
                fp_use["patch_hardware"] = bool(
                    antiflag.get("patch_hardware", True) or full
                )
                fp_use["patch_webgl"] = bool(antiflag.get("patch_webgl", True) or full)
                fp_use["patch_audio"] = bool(antiflag.get("patch_audio", True) or full)
                fp_use["patch_canvas"] = bool(antiflag.get("patch_canvas", False))
                # ensure mem for deviceMemory spoof
                if fp_use.get("device_memory") is None:
                    fp_use["device_memory"] = 8
            else:
                # minimal path only
                fp_use["mode"] = "minimal"
                fp_use["stealth_full"] = False
                fp_use["patch_canvas"] = False
                fp_use["patch_webgl"] = False
                fp_use["patch_audio"] = False
                fp_use["patch_hardware"] = False
            await af.inject_stealth(tab, fp_use, _exec_js)
            log.info(
                "Post-CF harden: full=%s hw=%s webgl=%s audio=%s wd=%s tz_cdp=%s "
                "cores=%s mem=%s renderer=%s",
                full,
                fp_use.get("patch_hardware"),
                fp_use.get("patch_webgl"),
                fp_use.get("patch_audio"),
                fp_use.get("hide_webdriver", True),
                fp_use.get("timezone_cdp_override"),
                fp_use.get("hardware_concurrency"),
                fp_use.get("device_memory"),
                str(fp_use.get("webgl_renderer") or "")[:48],
            )
        except Exception as e:
            log.debug("post-cf stealth: %s", e)

    # IMPORTANT: do NOT pull Chrome off-screen here.
    # Off-screen right after CF makes email input "not visible" → CDP click/type fails
    # (falls back to React-only fill → xAI often error_generic).
    # Pull-back is deferred until after email submit reaches OTP (see register_one).
    pull_after_cf = config.get("chrome_pull_back_after_cf")
    if pull_after_cf is None:
        # default FALSE — only pull if user explicitly enables
        pull_after_cf = False
    if pull_after_cf:
        try:
            pull_back_automation_chrome(config, reason="after_cf")
        except Exception as e:
            log.debug("pull_back after CF: %s", e)
    else:
        log.info("Keep Chrome on-screen after CF (email/OTP interact needs visible input)")


async def navigate_signup_with_cf(
    tab: Any, config: dict[str, Any], url: str = "https://accounts.x.ai/sign-up"
) -> None:
    """
    CF-stable navigation:
      - real Chrome UA (no spoof)
      - long wait, max 2 soft clicks
      - on CF failure page: wait + ONE reload only
      - never spam force-click
    """
    max_try = int(config.get("cf_max_retries") or 2) + 1
    wait_captcha = float(config.get("cf_wait_sec") or 40)
    last_err: Exception | None = None

    for attempt in range(1, max_try + 1):
        log.info("Opening %s (CF soft attempt %s/%s)...", url, attempt, max_try)
        navigated = False
        try:
            async with tab.expect_and_bypass_cloudflare_captcha(
                time_to_wait_captcha=min(45.0, wait_captcha)
            ):
                await tab.go_to(url)
                navigated = True
        except TypeError:
            try:
                async with tab.expect_and_bypass_cloudflare_captcha():
                    await tab.go_to(url)
                    navigated = True
            except Exception as e:
                last_err = e
                log.warning("CF context: %s", e)
        except Exception as e:
            last_err = e
            log.warning("CF helper: %s — plain goto", e)

        if not navigated:
            try:
                await tab.go_to(url)
            except Exception as e:
                last_err = e
                log.warning("goto failed: %s", e)

        # Let page + Turnstile settle BEFORE any extra click
        await asyncio.sleep(random.uniform(3.0, 5.0))

        st = await _cf_page_state(tab)
        # Diagnostic dump helps when widgets=0 / unknown
        try:
            diag = await _exec_js(
                tab,
                """
                (() => {
                  const t = (document.body && document.body.innerText || '').slice(0, 220);
                  const inputs = document.querySelectorAll('input').length;
                  const ifr = [...document.querySelectorAll('iframe')].map(f =>
                    (f.src||'').slice(0,80)).slice(0,4);
                  return {
                    title: document.title || '',
                    href: location.href || '',
                    bodyLen: (document.body && document.body.innerText || '').length,
                    inputs, iframes: ifr,
                    snip: t.replace(/\\s+/g, ' ').trim()
                  };
                })()
                """,
            )
            log.info("CF page state after load: %s diag=%s", st, diag)
        except Exception:
            log.info("CF page state after load: %s", st)

        if st == "clear":
            log.info("Sign-up ready (no CF challenge)")
            await _post_cf_session_harden(tab, config)
            await af.asleep(0.8, 1.5, label="post_cf_clear")
            return

        if st == "failed":
            # Official CF fail page — wait then ONE clean reload (no click spam)
            log.warning(
                "CF verification FAILED page — wait then single clean reload (no spam click)"
            )
            await asyncio.sleep(random.uniform(12.0, 18.0))
            if attempt >= max_try:
                log.error("CF failed page persists after retries")
                return
            try:
                await tab.go_to(url)
            except Exception as e:
                last_err = e
            await asyncio.sleep(random.uniform(4.0, 6.0))
            continue

        # unknown: wait for hydration / late Turnstile inject before treating as challenge
        if st == "unknown":
            log.warning("CF state unknown — wait 8s for app/Turnstile hydrate…")
            for _ in range(4):
                await asyncio.sleep(2.0)
                st = await _cf_page_state(tab)
                if st in ("clear", "challenge", "failed"):
                    break
            if st == "clear":
                log.info("Sign-up ready after hydrate wait")
                await _post_cf_session_harden(tab, config)
                return
            # If still unknown but has email / sign-up copy → treat clear
            try:
                looks = await _exec_js(
                    tab,
                    """
                    (() => {
                      const t = (document.body && document.body.innerText || '').toLowerCase();
                      const hasEmail = !!document.querySelector(
                        'input[type=email], input[name=email], input[autocomplete=email]'
                      );
                      const hasBtn = [...document.querySelectorAll('button,a')].some(el =>
                        /sign up with email|create your account|continue with email/i.test(
                          (el.innerText||'') + ' ' + (el.getAttribute('href')||'')
                        )
                      );
                      return !!(hasEmail || hasBtn || t.includes('create your account')
                        || t.includes('sign up with email'));
                    })()
                    """,
                )
                if looks:
                    log.info("Unknown CF but signup UI present — treat as clear")
                    await _post_cf_session_harden(tab, config)
                    return
            except Exception:
                pass

        # challenge (or stubborn unknown): soft solve only if widgets/challenge text exist
        info_pre = await _turnstile_widget_info(tab)
        has_cf_ui = bool(
            info_pre.get("widgets")
            or info_pre.get("challengeText")
            or info_pre.get("label")
            or st == "challenge"
        )
        if has_cf_ui:
            log.info(
                "CF challenge present — soft solve (widgets=%s)…",
                len(info_pre.get("widgets") or []),
            )
            cleared = await force_click_cloudflare_checkbox(tab, wait_sec=wait_captcha)
        else:
            log.warning(
                "No CF widget/challenge UI (state=%s) — skip spam click, reload path",
                st,
            )
            cleared = False
        st2 = await _cf_page_state(tab)
        if cleared or st2 == "clear":
            log.info("Sign-up page ready (CF clear)")
            await _post_cf_session_harden(tab, config)
            await af.asleep(1.2, 2.5, label="post_cf_clear")
            return

        if st2 == "failed":
            log.warning("CF flipped to FAILED after solve attempt")
            if attempt < max_try:
                await asyncio.sleep(random.uniform(10.0, 16.0))
                continue
            return

        if attempt < max_try:
            wait_s = af.human_delay(12.0, 20.0)
            log.warning("CF still blocking — wait %.0fs then ONE reload", wait_s)
            await asyncio.sleep(wait_s)
            try:
                await tab.go_to(url)
            except Exception as e:
                last_err = e
            await asyncio.sleep(random.uniform(3.5, 5.5))
        else:
            log.error("CF not cleared after %s attempts — continue carefully", max_try)
            if last_err:
                log.debug("last CF err: %s", last_err)


async def open_or_attach_browser(config: dict[str, Any]) -> BrowserHandle:
    """
    Anti-flag default: always START a fresh Chrome with clean profile.
    Attach only when fresh_profile_per_account=false AND port already live.
    """
    # Before each acc: only tool Chrome (never user browser)
    try:
        rep = chrome_clean.kill_tool_chrome(reason="pre_start_acc")
        if rep.get("killed_count"):
            log.info(
                "Pre-start cleaned tool Chrome: killed=%s remaining_tool=%s",
                rep.get("killed_count"),
                rep.get("remaining_tool"),
            )
    except Exception as e:
        log.debug("pre-start chrome cleanup: %s", e)

    port = chrome_debug_port(config)
    fp = af.pick_fingerprint(config)
    config["_fingerprint"] = fp
    fresh = bool(config.get("fresh_profile_per_account", True))
    # overnight / anti-flag: do not keep browser open between accounts
    keep_open = bool(config.get("keep_browser_open", False)) and not fresh

    # Pick free debug port BEFORE constructing Chrome (pydoll mutates options
    # with defaults on __init__ — never construct Chrome twice on same options).
    if fresh and probe_cdp_ws(port):
        for delta in range(1, 40):
            cand = port + delta
            if not probe_cdp_ws(cand):
                port = cand
                config["chrome_debug_port"] = port
                log.info("Port busy — using free debug port %s", port)
                break

    options = build_chrome_options(config, fingerprint=fp)
    _safe_add_arg(options, f"--remote-debugging-port={port}")
    browser = Chrome(options=options, connection_port=port)

    # Always START clean Chrome for reg (do NOT attach leftover tabs that may
    # already be logged-in as a previous account — that confuses batch runs).
    if not fresh and probe_cdp_ws(port):
        try:
            chrome_clean.kill_tool_chrome(reason="pre_start_no_attach_logged_in")
            await asyncio.sleep(1.0)
        except Exception:
            pass

    log.info(">>> START Chrome (profile=%s port %s) <<<", config.get("chrome_user_data_dir"), port)
    tab = await browser.start()

    # Web / desktop work: never pop Chrome in front unless chrome_steal_focus=true
    await asyncio.sleep(0.4)
    win_mode = str(config.get("chrome_window_mode") or "offscreen").lower()
    if not steal_focus_allowed(config):
        win_mode = "offscreen"
        pull_back_automation_chrome(config, reason="start_no_focus")
        minimize_automation_chrome(config)
        await asyncio.sleep(0.15)
        minimize_automation_chrome(config)
        log.info("Chrome parked off-screen (web/desktop — no focus steal)")
    elif win_mode in ("minimized", "background", "offscreen"):
        minimize_automation_chrome(config)
        await asyncio.sleep(0.2)
        minimize_automation_chrome(config)
    else:
        log.info(
            "Chrome LEFT VISIBLE for Cloudflare (mode=%s) — will kéo về after CF/done",
            win_mode,
        )

    # Guest start: drop previous account identity so batch doesn't open last logged-in user.
    # Competitor: clear SSO only — KEEP cf_clearance / Castle device (full wipe → error_generic).
    antiflag = config.get("antiflag") or {}
    want_cookies = bool(antiflag.get("clear_cookies_on_start", False))
    want_storage = bool(antiflag.get("clear_storage_on_start", False))
    force_guest = config.get("force_guest_on_start", True)
    # force_guest_mode: sso_only (default) | full
    guest_mode = str(config.get("force_guest_mode") or "sso_only").strip().lower()
    if force_guest or want_cookies or want_storage:
        sso_only = force_guest and guest_mode != "full" and not want_cookies
        log.info(
            "Wipe session (force_guest=%s mode=%s cookies=%s storage=%s) → guest",
            force_guest,
            "sso_only" if sso_only else "full",
            want_cookies or (force_guest and not sso_only),
            want_storage or (force_guest and not sso_only),
        )
        try:
            # Need a real origin to list/delete domain cookies (competitor soft reset)
            await tab.go_to("https://accounts.x.ai/")
            await asyncio.sleep(1.0)
        except Exception:
            try:
                await tab.go_to("about:blank")
            except Exception:
                pass
        if sso_only:
            # Competitor (grok-register-web): jar-level identity wipe + verify guest
            # NOT just deleteCookies(name, fixed domains) — that leaves multi-domain SSO.
            try:
                guest = await af.ensure_guest_session(tab, _exec_js)
                if guest.get("logged_in_after"):
                    slog.api_info(
                        "⚠️",
                        "Vẫn còn login acc cũ sau wipe — wipe lần 2 + về sign-up",
                    )
                    await af.clear_sso_identity_only(tab)
                    try:
                        await tab.go_to("https://accounts.x.ai/sign-up")
                        await asyncio.sleep(1.0)
                    except Exception:
                        pass
                slog.api_info(
                    "🧹",
                    f"Đã xóa SSO acc cũ (giữ CF/Castle) deleted≈{guest.get('wiped', 0)} — reg guest",
                )
            except Exception as e:
                log.warning("ensure_guest_session failed (%s) — fallback clear_sso", e)
                await af.clear_browser_session(
                    browser,
                    tab,
                    _exec_js,
                    clear_cookies=False,
                    clear_storage=False,
                    sso_only=True,
                )
                slog.api_info(
                    "🧹",
                    "Đã xóa SSO acc cũ (giữ CF/Castle) — reg guest",
                )
        else:
            await af.clear_browser_session(
                browser,
                tab,
                _exec_js,
                clear_cookies=bool(want_cookies or (force_guest and not sso_only)),
                clear_storage=bool(want_storage or (force_guest and not sso_only)),
                sso_only=False,
            )
            slog.api_info("🧹", "Đã xóa session acc cũ — bắt đầu reg với guest profile")
    # Do NOT inject stealth on blank tab before CF — it breaks Turnstile.
    # Optional hide-webdriver only after CF clear (navigate_signup_with_cf).

    return BrowserHandle(browser, tab, False, port, keep_open)


async def close_browser_handle(handle: BrowserHandle) -> None:
    """Detach CDP; STOP process if we started it — unless keep_open."""
    cfg = getattr(handle, "config", None) or {}
    # handle.keep_open is authoritative (set in register_one finally).
    # Do NOT re-OR config.keep_browser_open — that left guest Chrome open on errors.
    keep = bool(handle.keep_open)

    # Leave browser visible for user inspection when keep_open
    if keep:
        log.info(
            "Browser LEFT OPEN on port %s profile kept — user can inspect "
            "(do NOT kill Chrome). Window stays on-screen.",
            handle.port,
        )
        try:
            # Bring window on-screen at visible position
            pos = str(cfg.get("chrome_window_position") or "80,40")
            # reuse pull logic but with visible coords if off-screen was used
            if cfg.get("chrome_pull_back_after_done") is False or keep:
                # force show: SetWindowPos to 80,40 + SW_RESTORE
                x, y = 80, 40
                try:
                    parts = pos.replace(" ", "").split(",")
                    x, y = int(parts[0]), int(parts[1] if len(parts) > 1 else 40)
                    if x < 0:
                        x, y = 80, 40
                except Exception:
                    x, y = 80, 40
                ps = f"""
$ErrorActionPreference='SilentlyContinue'
Add-Type @"
using System; using System.Runtime.InteropServices;
public class GrokWinShow {{
  [DllImport("user32.dll")] public static extern bool SetWindowPos(
    IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h, int n);
}}
"@
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {{
  $_.CommandLine -and $_.CommandLine -match 'remote-debugging-port' -and
  ($_.CommandLine -match 'grok_tool|chrome_runs|chrome_profile')
}} | ForEach-Object {{
  $proc = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
  if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {{
    $h = $proc.MainWindowHandle
    [GrokWinShow]::ShowWindowAsync($h, 9) | Out-Null  # SW_RESTORE
    [GrokWinShow]::SetWindowPos($h, [IntPtr]::Zero, {x}, {y}, 0, 0, 0x0005) | Out-Null
  }}
}}
"""
                from grokreg.core import winhide

                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    timeout=10,
                    **winhide.kwargs(),
                )
                log.info("Chrome restored on-screen at %s,%s for inspection", x, y)
        except Exception as e:
            log.debug("show chrome: %s", e)
        # Detach CDP only — leave chrome.exe running
        try:
            if hasattr(handle.browser, "_connection_handler"):
                await handle.browser._connection_handler.close()
        except Exception:
            pass
        return

    # Normal close path
    try:
        if cfg.get("chrome_pull_back_after_done", True):
            pull_back_automation_chrome(cfg, reason="after_done")
    except Exception as e:
        log.debug("pull_back after done: %s", e)
    try:
        if hasattr(handle.browser, "_connection_handler"):
            await handle.browser._connection_handler.close()
    except Exception:
        pass
    try:
        if await handle.browser._is_browser_running(timeout=2):
            await handle.browser.stop()
            log.info("Browser stopped")
    except Exception as e:
        log.debug("stop browser: %s", e)
    try:
        rep = chrome_clean.kill_tool_chrome(reason="close_browser_handle")
        if rep.get("killed_count") or rep.get("matched_before"):
            log.info(
                "Post-stop tool Chrome: killed=%s remaining_tool=%s total_chrome=%s",
                rep.get("killed_count"),
                rep.get("remaining_tool"),
                rep.get("total_chrome"),
            )
    except Exception as e:
        log.debug("post-stop chrome cleanup: %s", e)


async def detect_page_step(tab: Any) -> str:
    """
    landing | email_form | otp | complete_signup | password | name | done | rate_limit | unknown

    complete_signup = one page with First name + Last name + Password + "Complete sign up"
    """
    # lazy import — detect_page_error lives in page_flow (avoid circular import)
    from grokreg.browser.page_flow import detect_page_error

    err = await detect_page_error(tab)
    if err and err.startswith("rate_limit"):
        return "rate_limit"

    pure = """
    (() => {
      const t = (document.body && document.body.innerText || '').toLowerCase();
      const href = (location.href || '').toLowerCase();
      const has = (sel) => !!document.querySelector(sel);
      if (has('input[name="code"]') || t.includes('verify your email') || t.includes('one time security code'))
        return 'otp';
      // xAI: Complete your sign up (first + last + password together)
      // note: password field is often input[name=password] type=text (not type=password)
      if (t.includes('complete your sign up') || t.includes('complete sign up'))
        return 'complete_signup';
      if ((has('input[type="password"]') || has('input[name="password"]')) && (
          has('input[autocomplete="given-name"]') || has('input[name="givenName"]') ||
          has('input[name="firstName"]') || has('input[autocomplete="family-name"]') ||
          has('input[name="familyName"]') || has('input[name="lastName"]') ||
          t.includes('first name')
        ))
        return 'complete_signup';
      if (has('input[type="password"]') || has('input[name="password"]') || t.includes('create a password') || t.includes('choose a password'))
        return 'password';
      if ((has('input[name="name"]') || has('input[autocomplete="name"]') || t.includes('what should we call')) && !has('input[type="password"]') && !has('input[name="password"]'))
        return 'name';
      if (has('input[type="email"]') || has('input[name="email"]'))
        return 'email_form';
      if (t.includes('sign up with email') || t.includes('sign up with x'))
        return 'landing';
      if (href.includes('grok.com') || t.includes('welcome') || t.includes('dashboard'))
        return 'done';
      return 'unknown';
    })()
    """
    step = await _exec_js(tab, pure)
    if isinstance(step, str) and step:
        return step
    return "unknown"



