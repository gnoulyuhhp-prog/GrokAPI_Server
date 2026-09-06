#!/usr/bin/env python3
"""
Continue: Grok free already registered → add into Sub2API.

Rules:
  - Prefer ATTACH existing Chrome on chrome_debug_port (same profile as reg).
  - If Chrome is down, START once with the SAME profile dir (not a new random profile).
  - Prefer SSO cookie → Sub2API sso-to-oauth API (competitor path); browser OAuth fallback.
  - Sub2API admin + OAuth use sibling tabs only (never a second Chrome profile).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from grokreg.core.config import load_config  # noqa: E402
from grokreg.core.runtime import log  # noqa: E402
from grokreg.browser.chrome import (  # noqa: E402
    open_or_attach_browser,
    close_browser_handle,
    chrome_debug_port,
    probe_cdp_ws,
)
from grokreg.browser.page_flow import _exec_js  # noqa: E402
from grokreg.delivery.sub2api_oauth import add_grok_to_sub2api  # noqa: E402


async def read_account_email(tab) -> str:
    data = await _exec_js(
        tab,
        """
        (() => {
          const text = (document.body && document.body.innerText) || '';
          const m = text.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
          const lines = text.split(/\\n+/).map(s => s.trim()).filter(Boolean);
          for (let i = 0; i < lines.length; i++) {
            if (/^email$/i.test(lines[i]) && i + 1 < lines.length) {
              const e = lines[i+1].match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i);
              if (e) return e[0];
            }
          }
          return m ? m[0] : '';
        })()
        """,
    )
    return str(data or "").strip()


def password_for_email(email: str, accounts_file: Path) -> str:
    if not accounts_file.exists():
        return ""
    email_l = email.lower()
    found = ""
    for line in accounts_file.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[0].strip().lower() == email_l:
            found = parts[1].strip()
    return found


async def main() -> int:
    config = load_config()
    # Force sub2api on for this script
    config.setdefault("sub2api", {})
    if isinstance(config["sub2api"], dict):
        config["sub2api"]["enabled"] = True

    port = chrome_debug_port(config)
    attached = bool(probe_cdp_ws(port))
    if attached:
        log.info(">>> ATTACH Chrome port %s (same profile as reg) — NO new profile <<<", port)
    else:
        log.warning(
            ">>> Chrome port %s down — START once with SAME profile=%s (not a second profile) <<<",
            port,
            config.get("chrome_user_data_dir"),
        )

    handle = await open_or_attach_browser(config)
    tab = handle.tab
    log.info(
        "Browser handle: attached=%s port=%s keep_open=%s",
        handle.attached,
        handle.port,
        handle.keep_open,
    )

    try:
        href = str(await _exec_js(tab, "location.href") or "")
        log.info("Current tab URL: %s", href)

        # Keep this tab on account page (session). Sub2API opens sibling tabs.
        if "accounts.x.ai/account" not in href.lower():
            log.info("Navigate current tab → accounts.x.ai/account (keep session)")
            await tab.go_to("https://accounts.x.ai/account")
            await asyncio.sleep(2)
            href = str(await _exec_js(tab, "location.href") or "")
            log.info("URL now: %s", href)

        email = await read_account_email(tab)
        acc_path = ROOT / str(config.get("save_file", "data/accounts.txt"))
        if not email:
            for line in reversed(
                acc_path.read_text(encoding="utf-8", errors="replace").splitlines()
            ):
                parts = line.split("|")
                if len(parts) >= 3 and parts[2].strip().lower() in (
                    "success",
                    "manual_check",
                    "manual_finish",
                    "added_sub2api",
                ) or (len(parts) >= 3 and "success" in parts[2].lower()):
                    email = parts[0].strip()
                    break
        # Prefer Nash if present as last success
        if not email:
            log.error("Cannot detect Grok email — open accounts.x.ai/account first")
            return 1

        password = password_for_email(email, acc_path) or str(
            config.get("fixed_password") or ""
        )
        if not password:
            log.error("No password for %s — set fixed_password in config.json", email)
            return 1

        log.info(">>> Sub2API import (SSO API first, OAuth fallback) for %s <<<", email)
        log.info("Password len=%s | account tab kept | admin+oauth = sibling tabs", len(password))

        sso = ""
        try:
            from grokreg.delivery.sso_capture import capture_sso_cookie, sso_preview

            sso = await capture_sso_cookie(tab, navigate_if_needed=True)
            if sso:
                log.info("SSO captured: %s", sso_preview(sso))
            else:
                log.warning("SSO not found — will try browser OAuth if mode allows")
        except Exception as e:
            log.warning("SSO capture: %s", e)

        s2 = await add_grok_to_sub2api(
            handle.browser,
            tab,
            config,
            email,
            password,
            sso_cookie=sso or None,
        )
        if s2.ok:
            log.info("SUCCESS Sub2API name=%s msg=%s", s2.name, s2.message)
            lines = acc_path.read_text(encoding="utf-8", errors="replace").splitlines()
            out = []
            done = False
            for line in lines:
                parts = line.split("|")
                if (
                    not done
                    and len(parts) >= 2
                    and parts[0].strip().lower() == email.lower()
                    and parts[1].strip() == password
                ):
                    out.append(f"{email}|{password}|added_sub2api:{s2.name}")
                    done = True
                else:
                    out.append(line)
            if not done:
                out.append(f"{email}|{password}|added_sub2api:{s2.name}")
            acc_path.write_text("\n".join(out) + "\n", encoding="utf-8")
            try:
                from grokreg.core.helpers import remember_account_time

                remember_account_time(email)
            except Exception:
                pass
            try:
                from grokreg.reg.flow import push_results_to_gsheet

                push_results_to_gsheet(config, email)
            except Exception as e:
                log.error("Google Sheet push failed after continue_sub2api %s: %s", email, e)
            return 0

        log.error("FAIL stage=%s msg=%s", s2.stage, s2.message)
        return 1
    finally:
        # Detach CDP only — never kill Chrome (keep_browser_open)
        try:
            await close_browser_handle(handle)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
