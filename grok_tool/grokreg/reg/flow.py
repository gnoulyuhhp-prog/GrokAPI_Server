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
from grokreg.core.helpers import *
from grokreg.core.cleanup import kill_old_runs
from grokreg.mail.mail_api import *
from grokreg.mail.providers import *
from grokreg.browser.chrome import *
from grokreg.browser.chrome import (  # noqa: F401
    _cf_page_state,
    _cf_still_blocking,
    _turnstile_widget_info,
    _cdp_click_xy,
)
from grokreg.browser.jsutil import _exec_js, _unwrap_js_result  # noqa: F401
from grokreg.browser.network_castle import *
from grokreg.browser.page_flow import *
from grokreg.browser.page_flow import (  # noqa: F401
    _grab_sso_cookie,
    _try_session_via_navigation,
)

def _normalize_email_provider(name: str) -> str:
    """
    Return: hotmail | azpopmail | tmail_wibu | tinyhost | tempmail_lol | tempmail_vip | racing | auto_temp | mailtm | auto
    """
    n = (name or "hotmail").lower().strip().replace(" ", "").replace("-", "_")
    if n in ("auto_temp", "temp", "tempmail", "temporary", "failover", "smart_temp"):
        return "auto_temp"
    if n in ("auto", "both", "any", "all"):
        return "auto"
    if n in ("hotmail", "outlook", "ms", "microsoft"):
        return "hotmail"
    if n in ("azpopmail", "azpop", "vipmail"):
        return "azpopmail"
    if n in (
        "tmail_wibu",
        "tmailwibu",
        "wibu",
        "wibucrypto",
        "tmail.wibucrypto",
        "tmail.wibucrypto.pro",
        "tmail",
    ):
        return "tmail_wibu"
    if n in ("tinyhost", "tiny", "tiny_host"):
        return "tinyhost"
    if n in ("tempmail_lol", "tempmaillol", "lol"):
        return "tempmail_lol"
    if n in ("tempmail_vip", "tempmailvip", "vip"):
        return "tempmail_vip"
    if n in ("racing", "fastest", "race", "fast_temp", "dua_mail"):
        return "racing"
    if n in ("mailtm", "mail.tm"):
        return "mailtm"
    log.warning("Unknown email_provider=%r — using auto_temp", name)
    return "auto_temp"


def _create_temp_session(
    which: str,
    azpop: AzpopMailProvider,
    tmail_wibu: Optional[TmailWibuProvider],
    config: dict[str, Any],
) -> EmailSession:
    """Create mailbox on azpop, tmail, tinyhost, tempmail_lol, tempmail_vip or racing."""
    order = [which] + [p for p in tmr.PROVIDERS if p != which]
    last_err: Exception | None = None
    for prov in order:
        try:
            if prov == "azpopmail":
                sess = azpop.create()
                log.info("TempMail using azpopmail → %s", sess.address)
                return sess
            if prov == "tmail_wibu":
                client = tmail_wibu or TmailWibuProvider(config.get("tmail_wibu") or {})
                address, extra = client.create_mailbox()
                log.info("TempMail using tmail_wibu → %s", address)
                return EmailSession(
                    address=address,
                    password="",
                    provider="tmail_wibu",
                    token="",
                    extra=extra,
                )
            if prov == "tinyhost":
                tiny_base = str((config.get("tinyhost") or {}).get("base_url") or config.get("tinyhost_base_url") or "https://tinyhost.shop")
                sess = TinyHostProvider(base_url=tiny_base).create_session()
                log.info("TempMail using tinyhost → %s", sess.address)
                return sess
            if prov == "tempmail_lol":
                lol_key = str((config.get("tempmail_lol") or {}).get("api_key") or config.get("tempmail_lol_key") or "")
                sess = TempMailLolProvider(api_key=lol_key).create_session()
                log.info("TempMail using tempmail_lol → %s", sess.address)
                return sess
            if prov == "tempmail_vip":
                vip_key = str((config.get("tempmail_vip") or {}).get("api_key") or config.get("tempmail_vip_key") or "")
                sess = TempMailVipProvider(api_key=vip_key).create_session()
                log.info("TempMail using tempmail_vip → %s", sess.address)
                return sess
            if prov == "racing":
                sess = RacingMailProvider(config).create_session()
                log.info("TempMail using racing → %s (%s)", sess.address, sess.provider)
                return sess
        except Exception as e:
            last_err = e
            log.warning("TempMail create failed on %s: %s — try next", prov, e)
            tmr.mark_temp_result(prov, ok=False, reason=f"create:{e}"[:80])
    raise RuntimeError(f"All temp mail providers failed: {last_err}")


def _auto_fix_next_temp_email(
    config: dict[str, Any],
    azpop: AzpopMailProvider,
    tmail_wibu: Optional[TmailWibuProvider],
    *,
    avoid_provider: str = "",
    avoid_domain: str = "",
) -> EmailSession:
    """
    AUTO-FIX: pick the other temp provider (or re-pick domain) after email_submit/otp fail.
    """
    avoid = (avoid_provider or "").lower()
    if avoid in tmr.PROVIDERS:
        which = "tmail_wibu" if avoid == "azpopmail" else "azpopmail"
    else:
        which = tmr.pick_temp_provider(
            preferred_order=list(
                config.get("temp_mail_order") or ["azpopmail", "tmail_wibu"]
            )
        )
    # If avoid domain on azpop, domain ranker already deprioritizes fails
    log.info(
        "AUTO-FIX next temp: prefer=%s (avoid_provider=%s avoid_domain=%s)",
        which,
        avoid or "-",
        avoid_domain or "-",
    )
    return _create_temp_session(which, azpop, tmail_wibu, config)

def acquire_email_session(
    config: dict[str, Any],
    mailtm: MailTmProvider,
    azpop: AzpopMailProvider,
    tmail_wibu: Optional[TmailWibuProvider] = None,
) -> tuple[EmailSession, Optional[HotmailProvider]]:
    """
    Pick email for this run (mode already chosen by user menu/CLI).
      hotmail    → hotmails.txt
      azpopmail  → azpopmail.com only
      tmail_wibu → tmail.wibucrypto.pro only
      auto_temp  → healthier of azpop/wibu (failover)
      mailtm     → Mail.tm
      auto       → hotmail if pool usable else auto_temp
    """
    mode = _normalize_email_provider(str(config.get("email_provider", "hotmail")))
    list_path = ROOT / str(config.get("hotmail_list", "data/hotmails.txt"))
    default_cid = str(
        (config.get("mail_api") or {}).get("client_id") or MS_CLIENT_IDS[0]
    )
    hotmail: Optional[HotmailProvider] = None

    def _hotmail_handle() -> Optional[HotmailProvider]:
        if list_path.exists():
            return HotmailProvider.from_config(list_path, config)
        return None

    if mode == "hotmail":
        hotmail = HotmailProvider.from_config(list_path, config)
        return hotmail.acquire(default_client_id=default_cid), hotmail

    if mode == "mailtm":
        return mailtm.create(), _hotmail_handle()

    if mode == "azpopmail":
        return azpop.create(), _hotmail_handle()

    if mode in ("tmail_wibu", "tinyhost", "tempmail_lol", "tempmail_vip", "racing"):
        return (
            _create_temp_session(mode, azpop, tmail_wibu, config),
            _hotmail_handle(),
        )

    if mode == "auto_temp":
        which = tmr.pick_temp_provider(
            preferred_order=list(
                config.get("temp_mail_order")
                or ["azpopmail", "tmail_wibu"]
            )
        )
        return _create_temp_session(which, azpop, tmail_wibu, config), _hotmail_handle()

    # ---- auto: hotmail first, else auto_temp ----
    hotmail = HotmailProvider.from_config(list_path, config)
    usable, total = hotmail.available_count()
    if usable > 0:
        try:
            session = hotmail.acquire(default_client_id=default_cid)
            log.info(
                "email_provider=auto → HOTMAIL (%s usable / %s in pool)",
                usable,
                total,
            )
            return session, hotmail
        except RuntimeError as e:
            log.warning("Hotmail acquire failed (%s) — fall back auto_temp", e)
    else:
        log.info(
            "email_provider=auto → auto_temp (hotmail pool empty/rate-limited: %s lines)",
            total,
        )
    which = tmr.pick_temp_provider(
        preferred_order=list(config.get("temp_mail_order") or ["azpopmail", "tmail_wibu"])
    )
    return _create_temp_session(which, azpop, tmail_wibu, config), hotmail


async def _extract_session_display(tab: Any) -> tuple[str, str]:
    """
    Display-only: best-effort UserId + SSO/JWT token from page storage.
    Does not alter status / accounts / Sub2API.
    """
    try:
        raw = await _exec_js(
            tab,
            """
            (() => {
              let userId = '';
              let token = '';
              const tryParse = (s) => {
                try { return JSON.parse(s); } catch(e) { return null; }
              };
              const scanObj = (o, depth) => {
                if (!o || depth > 4) return;
                if (typeof o === 'string') {
                  if (!userId && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(o))
                    userId = o;
                  if (!token && o.startsWith('eyJ') && o.length > 40) token = o;
                  return;
                }
                if (typeof o !== 'object') return;
                for (const [k, v] of Object.entries(o)) {
                  const kl = (k || '').toLowerCase();
                  if (!userId && typeof v === 'string' &&
                      (/userid|user_id|sub|accountid|account_id/.test(kl) ||
                       /^[0-9a-f-]{36}$/i.test(v))) {
                    if (/^[0-9a-f-]{36}$/i.test(v)) userId = v;
                  }
                  if (!token && typeof v === 'string' &&
                      (/token|sso|jwt|access|id_token|session/.test(kl) || v.startsWith('eyJ'))) {
                    if (v.startsWith('eyJ') && v.length > 40) token = v;
                  }
                  if (typeof v === 'object') scanObj(v, depth + 1);
                  if (typeof v === 'string' && (v.startsWith('{') || v.startsWith('[')))
                    scanObj(tryParse(v), depth + 1);
                }
              };
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i);
                  const v = localStorage.getItem(k);
                  scanObj({[k]: v}, 0);
                }
              } catch(e) {}
              try {
                for (let i = 0; i < sessionStorage.length; i++) {
                  const k = sessionStorage.key(i);
                  const v = sessionStorage.getItem(k);
                  scanObj({[k]: v}, 0);
                }
              } catch(e) {}
              try {
                const parts = (document.cookie || '').split(';');
                for (const p of parts) {
                  const eq = p.indexOf('=');
                  if (eq < 0) continue;
                  const k = p.slice(0, eq).trim();
                  const v = decodeURIComponent(p.slice(eq + 1).trim());
                  scanObj({[k]: v}, 0);
                }
              } catch(e) {}
              return {userId: userId || '', token: token || ''};
            })()
            """,
        )
        if isinstance(raw, dict):
            return str(raw.get("userId") or ""), str(raw.get("token") or "")
    except Exception:
        pass
    return "", ""


def push_results_to_gsheet(
    config: dict[str, Any],
    email: str = "",
    *,
    force: bool = False,
) -> bool:
    """
    Push full success ledger from accounts.txt → Google Sheet (existing payload/API).
    Called after each successful reg/Sub2API so results are not only in accounts.txt.
    """
    gs = config.get("google_sheets") or {}
    if not gs.get("enabled") and not force:
        return False
    sheet_name = str(gs.get("sheet_title") or gs.get("tab_name") or "Grok Acc Trắng")
    try:
        from grokreg.delivery.gsheets_export import (
            append_one_to_sheet,
            export_to_google_sheets,
        )

        if email:
            msg = append_one_to_sheet(email, tab="grok")
        else:
            msg = export_to_google_sheets({})
        log.info("Google Sheet push OK: %s", str(msg)[:160])
        if email:
            slog.sheet_ok(email, sheet_name=sheet_name)
        else:
            slog.sheet_ok("(batch)", sheet_name=sheet_name)
        return True
    except Exception as e:
        log.error("Google Sheet push FAILED: %s", e)
        if email:
            slog.sheet_fail(email, str(e)[:100])
        else:
            slog.sheet_fail("(batch)", str(e)[:100])
        return False



def _delay_bounds(config: dict, lo: float, hi: float) -> tuple[float, float]:
    """Scale step delays: fast mode clamps toward competitor-like 0.3–1.5s."""
    speed = str((config or {}).get("reg_speed") or "fast").strip().lower()
    if speed != "fast":
        return lo, hi
    # Prefer config human bounds; otherwise scale original lo/hi down.
    cmin = float((config or {}).get("human_delay_min") or 0)
    cmax = float((config or {}).get("human_delay_max") or 0)
    if cmin > 0 and cmax > cmin:
        # map [lo,hi] into [cmin,cmax] proportionally but never exceed cmax
        return cmin, min(cmax, max(cmin + 0.2, cmax * 0.85 + cmin * 0.15))
    scale = 0.35
    nlo = max(0.2, min(lo * scale, 1.2))
    nhi = max(nlo + 0.15, min(hi * scale, 2.0))
    return nlo, nhi

async def register_one(config: dict[str, Any]) -> None:
    if is_stop_requested():
        log.warning("register_one skipped — stop already requested (%s)", stop_reason())
        return "stopped"

    provider_mode = _normalize_email_provider(str(config.get("email_provider", "hotmail")))
    log.info("=" * 52)
    log.info("GROK REGISTER TOOL  |  email_provider=%s", provider_mode)
    log.info("=" * 52)

    # Share config with CF solvers (external Camoufox / YesCaptcha)
    try:
        click_turnstile_checkbox_robust._cfg = config  # type: ignore[attr-defined]
    except Exception:
        pass
    ts = config.get("turnstile") or {}
    if ts.get("mode", "auto") not in ("browser", "off", "none"):
        try:
            from grokreg.captcha.turnstile_solver_client import probe_solver

            url = str(ts.get("solver_url") or "http://127.0.0.1:5072")
            st = probe_solver(url)
            if st.get("online"):
                log.info(
                    "Turnstile EXTERNAL solver ONLINE %s (%sms)",
                    url,
                    st.get("latency_ms"),
                )
            else:
                log.warning(
                    "Turnstile external solver OFFLINE %s — will use pydoll click. "
                    "Chạy CHAY_SOLVER.bat để bật Camoufox solver.",
                    url,
                )
        except Exception as e:
            log.debug("solver probe: %s", e)

    # Undo mistaken 6h hard-bans from error_generic (IP-level, not domain block)
    try:
        cleared = af.clear_ip_level_hard_bans()
        if cleared:
            log.info("Domain stats: cleared %s IP-level hard-bans", cleared)
    except Exception as e:
        log.debug("clear_ip_level_hard_bans: %s", e)

    save_path = ROOT / str(config.get("save_file", "data/accounts.txt"))
    timeout_otp = int(config.get("timeout_otp", 180))
    pw_len = int(config.get("password_length", 14))

    mail_api = MailApiClient(config.get("mail_api") or {})
    mailtm = MailTmProvider()
    azpop = AzpopMailProvider(config.get("azpopmail") or {})
    tmail_wibu = TmailWibuProvider(config.get("tmail_wibu") or {})
    email_session, hotmail = acquire_email_session(
        config, mailtm, azpop, tmail_wibu=tmail_wibu
    )
    global _CURRENT_EMAIL_PROVIDER
    _CURRENT_EMAIL_PROVIDER = str(email_session.provider or "")

    grok_password = resolve_password(config)
    # --- styled terminal (display only) ---
    slog.set_email(email_session.address)
    slog.task_header(email_session.address)
    slog.api_info("🚀", "Khởi tạo Browser JS Context...")
    slog.api_info("⚡", f"Bắt đầu quy trình Đăng ký: {email_session.address}")
    # fixed_* only if non-empty; leave blank in config → random varied names
    fixed_f = str(config.get("fixed_first_name") or "").strip()
    fixed_l = str(config.get("fixed_last_name") or "").strip()
    custom_first = config.get("first_names") or config.get("name_first_pool")
    custom_last = config.get("last_names") or config.get("name_last_pool")
    first_pool = (
        [str(x).strip() for x in custom_first if str(x).strip()]
        if isinstance(custom_first, list)
        else None
    )
    last_pool = (
        [str(x).strip() for x in custom_last if str(x).strip()]
        if isinstance(custom_last, list)
        else None
    )
    if fixed_f and fixed_l:
        first_name, last_name = fixed_f, fixed_l
        log.warning(
            "Using FIXED name %s %s — set fixed_first/last_name empty for random (less flag risk)",
            first_name,
            last_name,
        )
    else:
        first_name, last_name = random_name(first_pool, last_pool)
        if fixed_f:
            first_name = fixed_f
        if fixed_l:
            last_name = fixed_l
    full_name = f"{first_name} {last_name}"

    log.info("Email     : %s  [%s]", email_session.address, email_session.provider)
    log.info("Password  : %s", grok_password)
    log.info("Name      : %s", full_name)
    log.info("mail_api  : %s", "ON" if mail_api.enabled else "OFF")

    # NOT error:unknown — that blocked the "unclear → login landing" path
    # because str.startswith("error:") matched and skipped credential login.
    status = "pending"
    # Do not mark hotmail used on rate_limit / soft failures
    keep_hotmail = False
    handle: Optional[BrowserHandle] = None

    try:
        # ATTACH existing Chrome if already open — do NOT spawn new profile mid-OTP
        handle = await open_or_attach_browser(config)
        tab = handle.tab
        default_cid = str(
            (config.get("mail_api") or {}).get("client_id") or MS_CLIENT_IDS[0]
        )
        # F12 Network capture — dump real API body when email_submit fails
        try:
            await enable_network_capture(tab)
        except Exception as e:
            log.warning("enable_network_capture: %s", e)

        # Competitor: never start signup while previous account still logged in
        try:
            if await page_is_logged_in(tab):
                log.warning(
                    "Start on LOGGED-IN session (prev acc) — ensure_guest again before signup"
                )
                await af.ensure_guest_session(tab, _exec_js)
                slog.api_info("🧹", "Ép logout acc cũ trước khi reg (competitor soft reset)")
        except Exception as e:
            log.debug("pre-signup guest check: %s", e)

        # ---------- RESUME: already on OTP page ----------
        step = await detect_page_step(tab)
        log.info("Page step on attach/start: %s (attached=%s)", step, handle.attached)

        if step == "otp":
            log.info(">>> RESUME OTP only — no new profile, no new signup <<<")
            page_email = await extract_email_from_otp_page(tab)
            if page_email and hotmail:
                matched = find_hotmail_session_for_email(hotmail, page_email, default_cid)
                if matched:
                    email_session = matched
                    log.info("Matched hotmail for page email: %s", page_email)
                else:
                    log.warning(
                        "Page email %s not in hotmails.txt — still try OTP with acquired session %s",
                        page_email,
                        email_session.address,
                    )
            # baseline empty: any xAI mail newer than 3 min
            since_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 180)
            )
            baseline_ids: set[str] = set()
            if mail_api.enabled and email_session.provider == "hotmail":
                # only ignore welcome; allow recent codes
                pass
            log.info("Fetching OTP for %s ...", email_session.address)
            otp = await asyncio.to_thread(
                wait_otp_smart,
                email_session,
                mail_api,
                mailtm,
                hotmail,
                timeout_otp,
                ignore_ids=baseline_ids,
                since_iso=since_iso,
                azpop=azpop,
                tmail_wibu=tmail_wibu,
            )
            if not otp:
                status = "error:otp_timeout_resume"
                log.error("No OTP while resuming — browser left open")
                keep_hotmail = True
            else:
                ok = await fill_otp_on_page(tab, otp)
                if not ok:
                    status = "error:otp_fill_failed_resume"
                    keep_hotmail = True
                else:
                    page_err = await detect_page_error(tab)
                    if page_err and page_err.startswith("rate_limit"):
                        status = f"error:{page_err[:100]}"
                        mins = int(config.get("rate_limit_cooldown_min") or 55)
                        m = re.search(r"retry in\s+(\d+)\s*minute", page_err, re.I)
                        if m:
                            mins = int(m.group(1)) + 2
                        set_email_rate_limit(email_session.address, mins, page_err[:120])
                        keep_hotmail = True
                    elif page_err and "invalid" in page_err.lower():
                        status = f"error:{page_err[:80]}"
                    else:
                        # continue password/name below by jumping — fall through after setting flag
                        step = await detect_page_step(tab)
                        log.info("After resume OTP, step=%s", step)
                        if step in ("password", "name", "done"):
                            status = "resumed_ok"
                        else:
                            status = "manual_check"

            # If we only resumed OTP and got password page, continue with password fill
            if status == "resumed_ok" or (
                status not in (
                    "error:otp_timeout_resume",
                    "error:otp_fill_failed_resume",
                )
                and (await detect_page_step(tab)) in ("password", "name", "done")
            ):
                # fall through to password section by not returning — set skip_to_password
                skip_to_password = True
            else:
                skip_to_password = False
                if status.startswith("error:") or status == "manual_check":
                    save_account(save_path, email_session.address, grok_password, status)
                    return
        else:
            skip_to_password = False

        if not skip_to_password:
            # ---------- FRESH or continue from landing/email ----------
            # If previous run left us logged-in (step=done) or on wrong page,
            # always force a clean sign-up — otherwise email fill hits wrong UI
            # and OTP is never sent (seen: continue click "v0.1.165").
            force_signup = step in ("done", "complete_signup") or (
                step not in ("landing", "email_form", "otp", "password", "name", "unknown")
            )
            if force_signup:
                log.info(
                    "Page step=%s — force fresh https://accounts.x.ai/sign-up (new email)",
                    step,
                )
                step = "unknown"

            if (
                force_signup
                or not handle.attached
                or step in ("landing", "unknown", "email_form")
            ):
                if force_signup or step in ("landing", "unknown") or not handle.attached:
                    # only navigate if not already on signup form
                    if force_signup or step not in ("email_form", "otp", "password", "name"):
                        await navigate_signup_with_cf(tab, config)
                        await af.asleep(
                            float(config.get("human_delay_min") or 1.5),
                            float(config.get("human_delay_max") or 4.5),
                            label="after_signup_nav",
                        )

                await dismiss_cookie_banner(tab)
                await af.asleep(*_delay_bounds(config, 0.6, 1.8), label="post_cookie")
                # Castle device signals — before interacting with signup form
                try:
                    await castle_human_warmup(tab, config)
                except Exception as e:
                    log.warning("castle warmup (landing): %s", e)

            # ---- Step A: choose "Sign up with email" if needed ----
            step = await detect_page_step(tab)
            if step == "landing":
                log.info("Selecting: Sign up with email")
            if step in ("landing", "unknown"):
                await af.human_pre_click(tab, _exec_js, af.asleep)
                if not await click_sign_up_with_email(tab):
                    status = "error:signup_with_email_not_found"
                    log.error("Could not click 'Sign up with email'")
                    save_account(save_path, email_session.address, grok_password, status)
                    if hotmail:
                        hotmail.mark_used(email_session)
                    af.mark_mail_fail(
                        email_session.address,
                        int(config.get("mail_fail_cooldown_min") or 120),
                        "signup_with_email_not_found",
                    )
                    return

            await wait_for_selector_js(
                tab,
                [
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[autocomplete="email"]',
                    'input[inputmode="email"]',
                ],
                timeout=12,
            )
            # Extra Castle mint attempt on email form (method=email_password path)
            try:
                tok = await castle_try_create_token(tab)
                log.info("Castle on email form: %s", tok)
                if not tok.get("ok"):
                    # short extra dwell + mouse
                    await af.asleep(*_delay_bounds(config, 2.0, 4.0), label="castle_email_dwell")
                    await _exec_js(
                        tab,
                        f"""
                        (() => {{
                          document.dispatchEvent(new MouseEvent('mousemove', {{
                            bubbles:true, clientX:{random.randint(100,600)},
                            clientY:{random.randint(100,400)}
                          }}));
                          return 1;
                        }})()
                        """,
                    )
                    tok2 = await castle_try_create_token(tab)
                    log.info("Castle on email form retry: %s", tok2)
            except Exception as e:
                log.debug("castle email form: %s", e)
            await af.asleep(*_delay_bounds(config, 0.8, 2.2), label="before_email_fill")
            # ---- Step B: fill email input only ----
            email_ok = await type_into(
                tab,
                [
                    {"css_selector": 'input[type="email"]'},
                    {"tag_name": "input", "attributes": {"type": "email"}},
                    {"css_selector": 'input[name="email"]'},
                    {"css_selector": 'input[autocomplete="email"]'},
                    {"css_selector": 'input[inputmode="email"]'},
                    {"css_selector": 'input[placeholder*="mail"]'},
                    {"css_selector": 'input[placeholder*="Email"]'},
                    {"css_selector": 'input[id*="email"]'},
                    {"css_selector": 'input[id*="Email"]'},
                    {"tag_name": "input", "attributes": {"name": "email"}},
                    {"css_selector": "input"},
                ],
                email_session.address,
                "email",
            )
            if not email_ok:
                status = "error:email_field_not_found"
                log.error("Email input not found after Sign up with email")
                slog.api_err("Email input not found after Sign up with email")
                save_account(save_path, email_session.address, grok_password, status)
                # do not burn hotmail pool forever — fail-cooldown instead
                af.mark_mail_fail(
                    email_session.address,
                    int(config.get("mail_fail_cooldown_min") or 120),
                    "email_field_not_found",
                )
                keep_hotmail = True
                return

            await af.asleep(*_delay_bounds(config, 1.2, 3.0), label="after_email_fill")
            # Snapshot OLD inbox BEFORE submit — if done after submit, brand-new
            # OTP mail can be included in baseline and never matched (stuck wait).
            since_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2)
            )
            baseline_ids: set[str] = set()
            if email_session.provider == "hotmail" and mail_api.enabled:
                log.info("Snapshot inbox BEFORE submit (ignore only old mails)...")
                baseline_ids = await asyncio.to_thread(
                    mail_api.snapshot_inbox_ids, email_session
                )
                log.info("Baseline ids=%s since=%s", len(baseline_ids), since_iso)

            # ---- Step C: Turnstile + Continue (match working Grok bots) ----
            await prepare_and_submit_email(tab, config, email_session.address)
            await af.asleep(*_delay_bounds(config, 2.0, 4.5), label="after_email_continue")
            # ---- Wait until OTP page (or hard fail). Do NOT poll mail if xAI never advanced. ----
            # Root cause of many "otp_timeout": error_generic / still on email form → no mail sent.
            # EARLY FAIL: poll fast (~0.7s), surface UI error immediately, stop burning time.
            reached_otp = False
            last_step = "unknown"
            last_page_err: Optional[str] = None
            for _wait_i in range(24):  # ~17s max if no error, break ASAP on error
                last_step = await detect_page_step(tab)
                last_page_err = await detect_page_error(tab)
                href = ""
                try:
                    href = str(await _exec_js(tab, "location.href") or "")
                except Exception:
                    pass
                if last_page_err:
                    if last_page_err.startswith("rate_limit"):
                        status = f"error:{last_page_err[:100]}"
                        log.error(
                            "xAI rate-limit OTP — STOP. Wait and retry later. (%s)",
                            last_page_err,
                        )
                        slog.api_err(f"EARLY FAIL rate_limit: {last_page_err[:80]}")
                        mins = int(config.get("rate_limit_cooldown_min") or 55)
                        m = re.search(r"retry in\s+(\d+)\s*minute", last_page_err, re.I)
                        if m:
                            mins = int(m.group(1)) + 2
                        set_email_rate_limit(
                            email_session.address, mins, last_page_err[:120]
                        )
                        save_account(
                            save_path, email_session.address, grok_password, status
                        )
                        keep_hotmail = True
                        return
                    if last_page_err.startswith("email_already_used"):
                        status = "error:email_already_used"
                        log.error("Email already registered on xAI")
                        slog.api_err("EARLY FAIL: email already used on xAI")
                        save_account(
                            save_path, email_session.address, grok_password, status
                        )
                        if hotmail:
                            hotmail.mark_used(email_session)
                        return
                    # Fatal page errors → mail will never arrive (AUTO-FIX retries below)
                    if (
                        last_page_err.startswith("error_generic")
                        or last_page_err.startswith("verification_failed")
                        or last_page_err.startswith("invalid_input_undefined")
                        or last_page_err.startswith("email_rejected")
                        or last_page_err.startswith("alert:")
                    ):
                        # Dump exact UI error for user (not vague codes)
                        detail = await capture_page_error_exact(tab)
                        exact = str(
                            (detail or {}).get("exact") or last_page_err or ""
                        )
                        # EARLY surface to web UI log (do not wait soft-retry dump)
                        slog.api_err(
                            f"EARLY FAIL email submit: {exact[:100] or last_page_err[:100]}"
                        )
                        log.error(
                            "Email submit rejected by xAI | step=%s | EXACT_UI=%r | err=%s | href=%s",
                            last_step,
                            exact[:200],
                            last_page_err,
                            href[:120],
                        )
                        try:
                            form_diag = await dump_email_form_diag(tab)
                        except Exception:
                            form_diag = {}
                        try:
                            net_diag = await read_xai_fetch_sniffer(tab)
                        except Exception:
                            net_diag = {}
                        # Wait a beat so response bodies are available, then dump F12 Network
                        await asyncio.sleep(1.2)
                        net_rows: list[dict[str, Any]] = []
                        try:
                            net_rows = await dump_xai_network_capture(
                                tab,
                                label="email_submit",
                                email=email_session.address,
                            )
                        except Exception as e:
                            log.warning("dump_xai_network_capture: %s", e)
                        try:
                            dump = ROOT / "data" / "last_xai_error.txt"
                            # summarize top network hits for the txt file
                            net_summary = []
                            for r in (net_rows or [])[:8]:
                                net_summary.append(
                                    {
                                        "method": r.get("method"),
                                        "status": r.get("status"),
                                        "url": r.get("url"),
                                        "errorText": r.get("errorText"),
                                        "body": str(r.get("responseBody") or "")[:400],
                                        "bodyError": r.get("bodyError"),
                                    }
                                )
                            dump.write_text(
                                f"time={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"email={email_session.address}\n"
                                f"provider={email_session.provider}\n"
                                f"step={last_step}\n"
                                f"href={href}\n"
                                f"exact={exact}\n"
                                f"err={last_page_err}\n"
                                f"form_diag={json.dumps(form_diag, ensure_ascii=False)[:1500]}\n"
                                f"fetch_sniffer={json.dumps(net_diag, ensure_ascii=False)[:1500]}\n"
                                f"network_f12={json.dumps(net_summary, ensure_ascii=False)[:6000]}\n"
                                f"detail={json.dumps(detail, ensure_ascii=False)[:2000]}\n"
                                f"network_file=network_capture_latest.json\n",
                                encoding="utf-8",
                            )
                            log.info(
                                "Email form diag: email=%r tsLen=%s tsPresent=%s btns=%s",
                                (form_diag or {}).get("email"),
                                (form_diag or {}).get("turnstileLen"),
                                (form_diag or {}).get("turnstilePresent"),
                                (form_diag or {}).get("buttons"),
                            )
                            if net_diag:
                                log.info(
                                    "xAI fetch sniffer last=%s",
                                    json.dumps(
                                        (net_diag or {}).get("last") or net_diag,
                                        ensure_ascii=False,
                                    )[:300],
                                )
                        except Exception:
                            pass
                        status = f"error:email_submit:{exact[:120] or last_page_err[:120]}"
                        save_account(
                            save_path, email_session.address, grok_password, status
                        )
                        # Soft mark only for IP-level generic (no 6h hard-ban)
                        try:
                            if "@" in email_session.address:
                                af.mark_domain_otp(
                                    email_session.address.split("@")[-1],
                                    ok=False,
                                    elapsed=0,
                                    reason=status[:80],
                                )
                        except Exception:
                            pass
                        # break wait loop; outer AUTO-FIX may switch email
                        reached_otp = False
                        break
                if last_step == "otp":
                    reached_otp = True
                    log.info(
                        "OTP page ready (href=%s) — start mailbox poll",
                        href[:100],
                    )
                    slog.api_ok("Đã Submit Email! Đang quét OTP...")
                    # Safe to hide Chrome now — keyboard interact done for email step
                    if config.get("chrome_pull_back_after_otp", True):
                        try:
                            pull_back_automation_chrome(config, reason="after_otp_page")
                        except Exception:
                            pass
                    break
                if last_step in ("password", "name", "complete_signup", "done"):
                    # already past OTP somehow
                    log.info("Page already past OTP step=%s", last_step)
                    reached_otp = True
                    if config.get("chrome_pull_back_after_otp", True):
                        try:
                            pull_back_automation_chrome(config, reason="after_past_otp")
                        except Exception:
                            pass
                    break
                await asyncio.sleep(0.7)

            if not reached_otp:
                # Soft retry same email ONLY when F12 proves Castle mint failed
                # (not for generic "Something went wrong" — that is IP/session, fail fast)
                retries_left = int(config.get("castle_retry_on_mint_fail") or 0)
                castle_fail = False
                try:
                    castle_fail = bool(castle_mint_failed_in_network())
                except Exception:
                    castle_fail = False
                if (
                    retries_left > 0
                    and last_page_err
                    and (
                        last_page_err.startswith("error_generic")
                        or "something went wrong" in last_page_err.lower()
                    )
                    and castle_fail
                ):
                    log.warning(
                        "Castle mint_failed detected in Network — soft-retry same email "
                        "after longer warmup (no domain rotate yet)"
                    )
                    try:
                        config["_castle_retry_budget"] = retries_left - 1
                        # temporarily extend warmup
                        old_w = config.get("castle_warmup_sec")
                        old_t = config.get("castle_wait_token_sec")
                        config["castle_warmup_sec"] = max(
                            float(old_w or 12), 18.0
                        )
                        config["castle_wait_token_sec"] = max(
                            float(old_t or 28), 40.0
                        )
                        await navigate_signup_with_cf(tab, config)
                        await dismiss_cookie_banner(tab)
                        await castle_human_warmup(tab, config)
                        stn = await detect_page_step(tab)
                        if stn in ("landing", "unknown"):
                            await click_sign_up_with_email(tab)
                        await wait_for_selector_js(
                            tab,
                            [
                                'input[type="email"]',
                                'input[name="email"]',
                                'input[autocomplete="email"]',
                            ],
                            timeout=12,
                        )
                        await prepare_and_submit_email(
                            tab, config, email_session.address
                        )
                        for _ in range(20):
                            last_step = await detect_page_step(tab)
                            last_page_err = await detect_page_error(tab)
                            if last_step == "otp" or last_step in (
                                "password",
                                "name",
                                "complete_signup",
                                "done",
                            ):
                                reached_otp = True
                                log.info(
                                    "Castle soft-retry reached step=%s", last_step
                                )
                                break
                            if last_page_err and last_page_err.startswith(
                                "error_generic"
                            ):
                                log.error(
                                    "Castle soft-retry still error_generic: %s",
                                    last_page_err,
                                )
                                break
                            await asyncio.sleep(2.0)
                        if old_w is not None:
                            config["castle_warmup_sec"] = old_w
                        if old_t is not None:
                            config["castle_wait_token_sec"] = old_t
                    except Exception as e:
                        log.warning("Castle soft-retry failed: %s", e)

            if not reached_otp:
                log.error(
                    "Never reached OTP page after email submit (step=%s err=%s) — no mail will arrive",
                    last_step,
                    last_page_err,
                )
                slog.api_err(
                    f"EARLY FAIL no OTP page: step={last_step} err={(last_page_err or 'none')[:90]}"
                )
                if not str(status).startswith("error:email_submit"):
                    status = f"error:no_otp_page:step={last_step}:err={last_page_err or 'none'}"
                    save_account(save_path, email_session.address, grok_password, status)
                    try:
                        if "@" in email_session.address:
                            af.mark_domain_otp(
                                email_session.address.split("@")[-1],
                                ok=False,
                                elapsed=0,
                                reason=status[:80],
                            )
                    except Exception:
                        pass
                # AUTO-FIX: rotate temp mail / domain until OTP page appears.
                # error_generic is usually IP/session — FAIL FAST (0 autofix default).
                max_autofix = int(config.get("email_autofix_retries") or 5)
                is_generic = bool(
                    last_page_err
                    and (
                        last_page_err.startswith("error_generic")
                        or "something went wrong" in last_page_err.lower()
                    )
                ) or str(status).startswith("error:email_submit")
                if is_generic:
                    # Default 0 = stop immediately (user asked: detect error ASAP)
                    max_autofix = int(config.get("email_autofix_on_generic") or 0)
                    log.warning(
                        "error_generic (IP/session) → AUTO-FIX attempts=%s. "
                        "Đổi VPN / chờ, không spam domain.",
                        max_autofix,
                    )
                    if max_autofix <= 0:
                        slog.api_err(
                            "STOP sớm: xAI 'Something went wrong' — đổi VPN rồi Start lại"
                        )
                        return status
                if email_session.provider in ("azpopmail", "tmail_wibu", "mailtm"):
                    try:
                        for attempt in range(1, max_autofix + 1):
                            old = email_session.address
                            old_p = email_session.provider
                            # Soft-ban previous domain (IP-level generic ≠ disposable block)
                            try:
                                if "@" in old:
                                    af.ban_domain(
                                        old.split("@")[-1],
                                        hours=0.33 if is_generic else 6,
                                        reason=f"email_submit_reject:{last_page_err or status}",
                                        soft=is_generic,
                                    )
                            except Exception:
                                pass
                            email_session = _auto_fix_next_temp_email(
                                config,
                                azpop,
                                tmail_wibu,
                                avoid_provider=old_p if attempt % 2 == 0 else "",
                                avoid_domain=old.split("@")[-1] if "@" in old else "",
                            )
                            _CURRENT_EMAIL_PROVIDER = email_session.provider
                            log.info(
                                "AUTO-FIX new email after no_otp_page (%s/%s): %s [%s] (was %s)",
                                attempt,
                                max_autofix,
                                email_session.address,
                                email_session.provider,
                                old,
                            )
                            await navigate_signup_with_cf(tab, config)
                            await af.asleep(*_delay_bounds(config, 1.5, 3.0), label="autofix_renav")
                            await dismiss_cookie_banner(tab)
                            stn = await detect_page_step(tab)
                            if stn in ("landing", "unknown"):
                                await click_sign_up_with_email(tab)
                            await wait_for_selector_js(
                                tab,
                                [
                                    'input[type="email"]',
                                    'input[name="email"]',
                                    'input[autocomplete="email"]',
                                ],
                                timeout=12,
                            )
                            since_iso = time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2)
                            )
                            await prepare_and_submit_email(
                                tab, config, email_session.address
                            )
                            await af.asleep(*_delay_bounds(config, 2.5, 5.0), label="autofix_after_continue")
                            reached_otp = False
                            last_page_err = None
                            for _ in range(18):
                                last_step = await detect_page_step(tab)
                                last_page_err = await detect_page_error(tab)
                                if last_page_err and last_page_err.startswith("rate_limit"):
                                    status = f"error:{last_page_err[:100]}"
                                    save_account(
                                        save_path,
                                        email_session.address,
                                        grok_password,
                                        status,
                                    )
                                    keep_hotmail = True
                                    return
                                if last_page_err and (
                                    last_page_err.startswith("error_generic")
                                    or last_page_err.startswith("verification_failed")
                                    or last_page_err.startswith("email_rejected")
                                    or last_page_err.startswith("alert:")
                                ):
                                    status = f"error:email_submit:{last_page_err[:120]}"
                                    save_account(
                                        save_path,
                                        email_session.address,
                                        grok_password,
                                        status,
                                    )
                                    log.error(
                                        "AUTO-FIX attempt %s/%s rejected: %s",
                                        attempt,
                                        max_autofix,
                                        last_page_err,
                                    )
                                    break
                                if last_step == "otp" or last_step in (
                                    "password",
                                    "name",
                                    "complete_signup",
                                    "done",
                                ):
                                    reached_otp = True
                                    log.info(
                                        "AUTO-FIX reached OTP/step=%s on attempt %s",
                                        last_step,
                                        attempt,
                                    )
                                    break
                                await asyncio.sleep(2)
                            if reached_otp:
                                break
                        if not reached_otp:
                            status = f"error:no_otp_page:autofix:step={last_step}"
                            save_account(
                                save_path,
                                email_session.address,
                                grok_password,
                                status,
                            )
                            return
                    except Exception as e:
                        log.exception("AUTO-FIX no_otp_page failed: %s", e)
                        return
                else:
                    return

            # ---- OTP mailbox poll (only after OTP page confirmed) ----
            log.info(
                "Waiting NEW xAI code (since=%s, ignore_ids=%s, email=%s provider=%s)...",
                since_iso,
                len(baseline_ids),
                email_session.address,
                email_session.provider,
            )
            await asyncio.sleep(2)
            otp = await asyncio.to_thread(
                wait_otp_smart,
                email_session,
                mail_api,
                mailtm,
                hotmail,
                timeout_otp,
                ignore_ids=baseline_ids,
                since_iso=since_iso,
                azpop=azpop,
                tmail_wibu=tmail_wibu,
            )

            if not otp:
                status = "error:otp_timeout"
                log.error(
                    "OTP timeout — mailbox never returned code for %s [%s] "
                    "(if manual works: check domain block or inbox API)",
                    email_session.address,
                    email_session.provider,
                )
                slog.api_err(f"OTP timeout cho {email_session.address}")
                save_account(save_path, email_session.address, grok_password, status)
                af.mark_mail_fail(
                    email_session.address,
                    int(config.get("mail_fail_cooldown_min") or 120),
                    "otp_timeout",
                )
                # soft fail: keep hotmail for later, do not burn pool
                keep_hotmail = True
                await af.asleep(*_delay_bounds(config, 5.0, 12.0), label="otp_timeout_pause")
                return

            log.info(
                "OTP from mail (display): %s → input: %s",
                otp,
                normalize_otp_for_input(otp),
            )
            slog.api_ok(f"OTP: {normalize_otp_for_input(otp)}")
            await af.asleep(*_delay_bounds(config, 1.0, 2.8), label="before_otp_fill")
            # ensure CF not blocking before OTP submit
            if await _cf_still_blocking(tab):
                log.info("CF visible on OTP page — force checkbox before fill")
                await force_click_cloudflare_checkbox(tab, wait_sec=15.0)
            if not await fill_otp_on_page(tab, otp):
                status = "error:otp_fill_failed"
                slog.api_err("OTP fill failed")
                save_account(save_path, email_session.address, grok_password, status)
                af.mark_mail_fail(
                    email_session.address,
                    int(config.get("mail_fail_cooldown_min") or 90),
                    "otp_fill_failed",
                )
                keep_hotmail = True
                return
            await af.asleep(*_delay_bounds(config, 2.0, 4.0), label="after_otp")
            page_err = await detect_page_error(tab)
            if page_err:
                if page_err == "verification_failed":
                    log.error("Verification failed after OTP — one soft CF recover")
                    await force_click_cloudflare_checkbox(tab, wait_sec=15.0)
                    await wait_turnstile_token(tab, timeout=12.0)
                    # re-submit OTP once
                    await fill_otp_on_page(tab, otp)
                    await af.asleep(*_delay_bounds(config, 2.5, 4.0), label="otp_after_verify_recover")
                    page_err = await detect_page_error(tab)
                    if page_err == "verification_failed":
                        status = "error:verification_failed_otp"
                        save_account(save_path, email_session.address, grok_password, status)
                        keep_hotmail = True
                        return
                if page_err and page_err.startswith("rate_limit"):
                    status = f"error:{page_err[:100]}"
                    log.error("Rate-limit after OTP step — STOP: %s", page_err)
                    mins = int(config.get("rate_limit_cooldown_min") or 55)
                    m = re.search(r"retry in\s+(\d+)\s*minute", page_err, re.I)
                    if m:
                        mins = int(m.group(1)) + 2
                    set_email_rate_limit(email_session.address, mins, page_err[:120])
                    save_account(save_path, email_session.address, grok_password, status)
                    keep_hotmail = True
                    log.warning("Hotmail kept for retry after ~%sm", mins)
                    return
                if page_err and (
                    page_err.startswith("invalid_code")
                    or page_err.startswith("invalid_input")
                    or "undefined" in page_err.lower()
                ):
                    status = f"error:{page_err[:80]}:{otp}"
                    log.error("OTP/form rejected: %s", page_err)
                    save_account(save_path, email_session.address, grok_password, status)
                    af.mark_mail_fail(
                        email_session.address,
                        int(config.get("mail_fail_cooldown_min") or 90),
                        page_err[:80],
                    )
                    keep_hotmail = True
                    return
                if page_err:
                    log.warning("Page alert after OTP: %s", page_err)

        # ---- after OTP: Complete your sign up (first + last + password) ----
        step_now = await detect_page_step(tab)
        log.info("Post-OTP page step: %s", step_now)
        success = False  # set True only after session confirmed

        if step_now in ("complete_signup", "password", "name", "unknown", "otp", "done"):
            # wait for password field (complete signup form)
            if step_now != "done":
                await wait_for_selector_js(
                    tab,
                    [
                        'input[name="password"]',
                        'input[type="password"]',
                        'input[name="givenName"]',
                        'input[autocomplete="given-name"]',
                        'input[name="firstName"]',
                    ],
                    timeout=25,
                )
                await asyncio.sleep(0.5)
                step_now = await detect_page_step(tab)

            if step_now in ("complete_signup", "password", "name", "unknown"):
                log.info("Using SHARED Grok password: %s", grok_password)
                await af.asleep(*_delay_bounds(config, 1.0, 2.5), label="before_complete_signup")
                # Keep window visible (Castle mint fails if tab hidden)
                try:
                    from grokreg.browser.chrome import maybe_bring_to_front

                    await maybe_bring_to_front(tab, config)
                except Exception:
                    pass
                # 1) Fill name+password ONCE
                await fill_complete_signup(
                    tab,
                    first_name,
                    last_name,
                    grok_password,
                    click_submit=False,
                    fill_fields=True,
                )
                if not await complete_signup_fields_ok(
                    tab, first_name, last_name, grok_password
                ):
                    log.warning("Complete fields empty after first fill — retry fill")
                    await fill_complete_signup(
                        tab,
                        first_name,
                        last_name,
                        grok_password,
                        click_submit=False,
                        fill_fields=True,
                    )
                # 2) Tick Cloudflare human check (may clear fields)
                slog.api_info("🛡️", "Đang chờ Cloudflare Turnstile Token...")
                await click_turnstile_checkbox_robust(
                    tab, wait_sec=35.0, reason="complete_signup"
                )
                await wait_turnstile_token(tab, timeout=15.0)
                slog.api_ok("Cloudflare Turnstile Token đã sẵn sàng!")
                # 3) Re-sync fields if CF wiped React state, then submit
                if not await complete_signup_fields_ok(
                    tab, first_name, last_name, grok_password
                ):
                    log.warning(
                        "Fields missing after CF — re-fill ALL (name+password) once"
                    )
                    await fill_complete_signup(
                        tab,
                        first_name,
                        last_name,
                        grok_password,
                        click_submit=False,
                        fill_fields=True,
                    )
                else:
                    await ensure_complete_signup_password(tab, grok_password)
                pre = await read_complete_signup_fields(tab)
                log.info("Pre-submit complete form snapshot: %s", pre)
                if not await complete_signup_fields_ok(
                    tab, first_name, last_name, grok_password
                ):
                    status = "error:complete_fields_empty"
                    log.error(
                        "REFUSING submit — complete form still empty. snapshot=%s", pre
                    )
                    save_account(
                        save_path, email_session.address, grok_password, status
                    )
                    keep_hotmail = True
                    return
                # 4) MUST be visible — Castle/session cookies break when visibility=hidden
                try:
                    from grokreg.browser.chrome import maybe_bring_to_front

                    await maybe_bring_to_front(tab, config)
                    # restore tool chrome window on-screen
                    pos = str(config.get("chrome_window_position") or "80,40")
                    from grokreg.core import winhide

                    subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            f"""
$ErrorActionPreference='SilentlyContinue'
Add-Type @"
using System; using System.Runtime.InteropServices;
public class GrokShow2 {{
  [DllImport(\\"user32.dll\\")] public static extern bool ShowWindowAsync(IntPtr h, int n);
  [DllImport(\\"user32.dll\\")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport(\\"user32.dll\\")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int X, int Y, int cx, int cy, uint f);
}}
"@
Get-CimInstance Win32_Process -Filter \\"Name='chrome.exe'\\" | Where-Object {{
  $_.CommandLine -and $_.CommandLine -match 'remote-debugging-port' -and
  ($_.CommandLine -match 'grok_tool|chrome_profile')
}} | ForEach-Object {{
  $p = Get-Process -Id $_.ProcessId -EA SilentlyContinue
  if ($p -and $p.MainWindowHandle -ne [IntPtr]::Zero) {{
    [GrokShow2]::ShowWindowAsync($p.MainWindowHandle, 9) | Out-Null
    [GrokShow2]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
    [GrokShow2]::SetWindowPos($p.MainWindowHandle, [IntPtr]::Zero, 80, 40, 0, 0, 0x0005) | Out-Null
  }}
}}
""",
                        ],
                        capture_output=True,
                        timeout=8,
                        **winhide.kwargs(),
                    )
                except Exception as e:
                    log.debug("force visible: %s", e)
                await _exec_js(tab, "try{window.focus()}catch(e){}; document.visibilityState")
                await asyncio.sleep(0.5)

                # 5) TOS interceptor + accept, then Complete (must create SESSION not just user)
                try:
                    # Install EARLY + tick boxes; re-install right before click
                    await install_tos_fetch_interceptor(tab)
                    await accept_tos_on_complete_form(tab)
                    await asyncio.sleep(0.4)
                    await install_tos_fetch_interceptor(tab)
                except Exception as e:
                    log.warning("pre-submit TOS: %s", e)
                slog.api_ok("Submit Complete sign up...")
                await click_complete_signup_button(tab)
                slog.api_wait(
                    "Đang chờ xAI tạo tài khoản thành công (trang accounts.x.ai/account)..."
                )
                # Wait for navigation / session (createUserAndSession can be slow)
                for w in range(18):
                    await asyncio.sleep(1.2)
                    post_href = str(await _exec_js(tab, "location.href") or "")
                    post_step = await detect_page_step(tab)
                    if await page_is_logged_in(tab) or await page_looks_success(tab):
                        break
                    if "/oauth2/consent" in post_href.lower():
                        break
                    if post_step != "complete_signup" or "/sign-in" in post_href.lower():
                        # give one more beat for cookie set after redirect
                        if w < 3:
                            await asyncio.sleep(1.5)
                        break
                post_href = str(await _exec_js(tab, "location.href") or "")
                post_err = await detect_page_error(tab)
                post_step = await detect_page_step(tab)
                patched = await _exec_js(tab, "window.__grokTosPatched || 0")
                log.info(
                    "After Complete click: step=%s href=%s err=%s tosPatched=%s vis=%s",
                    post_step,
                    post_href[:120],
                    post_err,
                    patched,
                    await _exec_js(tab, "document.visibilityState"),
                )
                if not patched:
                    log.warning(
                        "tosPatched=0 after Complete — createUser may lack tosAcceptedVersion "
                        "(session cookie often missing; will try login fallback)"
                    )
                # Always dump network after complete for diagnosis
                try:
                    await dump_xai_network_capture(
                        tab,
                        label=(
                            "after_complete_to_signin"
                            if "/sign-in" in post_href.lower()
                            else "after_complete"
                        ),
                        email=email_session.address,
                    )
                except Exception as e:
                    log.debug("dump after complete: %s", e)

                # Detect client log "User successfully signed up" even if bounced to sign-in
                signup_ok_client = False
                try:
                    for e in list(_NET_REQUESTS.values())[-40:]:
                        pd = str(e.get("postData") or "")
                        if "successfully signed up" in pd.lower():
                            signup_ok_client = True
                            break
                    # also check capture dump file quickly via in-memory responses
                    for e in list(_NET_REQUESTS.values())[-80:]:
                        if "successfully signed up" in str(e).lower():
                            signup_ok_client = True
                            break
                except Exception:
                    pass
                # re-read latest capture file
                try:
                    cap = json.loads((ROOT / "data" / "network_capture_latest.json").read_text(encoding="utf-8"))
                    for row in cap.get("entries") or []:
                        if "successfully signed up" in str(row.get("postData") or "").lower():
                            signup_ok_client = True
                            break
                except Exception:
                    pass

                if "/sign-in" in post_href.lower():
                    # 0) CDP SSO first — log showed sso+sso-rw exist while UI still sign-in
                    sso_now = await _grab_sso_cookie(tab)
                    if sso_now:
                        config["_last_sso_cookie"] = sso_now
                        success = True
                        log.info(
                            "Post-complete: SSO cookie captured (len=%s) → reg OK / Sub2API API path",
                            len(sso_now),
                        )
                        slog.api_ok(
                            f"Bắt SSO cookie (len={len(sso_now)}) — bỏ qua login UI kẹt CF"
                        )
                    # 1) Maybe UI session too
                    elif await _try_session_via_navigation(tab):
                        success = True
                        sso_now = await _grab_sso_cookie(tab)
                        if sso_now:
                            config["_last_sso_cookie"] = sso_now
                        log.info("Post-complete: session recovered via navigation/SSO")
                    else:
                        # Account often CREATED but session cookie missing when:
                        #  - tab was off-screen / CF weak
                        #  - tosAcceptedVersion was $undefined (tosPatched=0)
                        # Finish session by signing in with the password we just set.
                        log.warning(
                            "Complete → sign-in (signup_ok_client=%s tosPatched=%s). "
                            "Finishing session for %s via credentials…",
                            signup_ok_client,
                            patched,
                            email_session.address,
                        )
                        if not signup_ok_client:
                            log.warning(
                                "No client 'successfully signed up' signal — still try login "
                                "(account may exist)"
                            )
                        slog.api_info(
                            "🔑",
                            "Acc có thể đã tạo — đang login lại để lấy session…",
                        )
                        # brief pause so backend indexes the new user
                        await asyncio.sleep(random.uniform(3.0, 5.0))
                        if await login_with_credentials(
                            tab,
                            email_session.address,
                            grok_password,
                            config,
                            attempts=2,
                        ):
                            success = True
                            sso_now = await _grab_sso_cookie(tab)
                            if sso_now:
                                config["_last_sso_cookie"] = sso_now
                            log.info("Post-signup session established via credentials")
                            slog.api_ok("Session OK sau login fallback")
                        else:
                            # last chance: SSO may appear after failed UI login attempts
                            sso_now = await _grab_sso_cookie(tab)
                            if sso_now:
                                config["_last_sso_cookie"] = sso_now
                                success = True
                                log.warning(
                                    "UI login failed but SSO cookie present — continue Sub2API"
                                )
                                slog.api_ok("Có SSO cookie dù login UI fail — đi Sub2API")
                            else:
                                status = "error:signup_ok_session_fail"
                                log.error(
                                    "Account likely created but could not establish session "
                                    "(email=%s). Try CHAY_SOLVER.bat for CF + visible Chrome.",
                                    email_session.address,
                                )
                                slog.api_err(
                                    "signup_ok_session_fail — reg form xong nhưng chưa login được"
                                )
            elif step_now == "done":
                log.info("Already past signup form")

            log.info("Waiting for SIGNUP success / logged-in dashboard (up to 90s)...")
            # may already be True if post-signup session was established above
            if not success:
                success = False
            submit_count = 1  # already submitted once above when on complete form
            verify_recoveries = 0
            signin_bounce_logged = False
            for poll_i in range(0 if success else 24):
                if success:
                    break
                href_now = str(await _exec_js(tab, "location.href") or "")
                # Consent page after complete = reg worked (SSO active)
                if "/oauth2/consent" in href_now.lower():
                    log.info("Post-complete landed on OAuth consent — treating as reg OK")
                    await dismiss_oauth_consent_if_present(tab)
                    success = True
                    break
                if await page_looks_success(tab):
                    success = True
                    href_ok = await _exec_js(tab, "location.href")
                    log.info("Signup SUCCESS detected href=%s", str(href_ok or "")[:120])
                    break
                # Bounce to /sign-in: only hard-fail if we did NOT already create the user
                if "/sign-in" in href_now.lower():
                    if str(status).startswith("error:signup"):
                        break
                    if not signin_bounce_logged:
                        signin_bounce_logged = True
                        log.error(
                            "Still on /sign-in after Complete — no session yet. "
                            "status was %s",
                            status,
                        )
                        if not str(status).startswith("error:"):
                            status = "error:signup_to_signin"
                        try:
                            snap = await read_complete_signup_fields(tab)
                            log.error("sign-in bounce snapshot: %s", snap)
                        except Exception:
                            pass
                    break
                # progress log every ~8s
                if poll_i % 2 == 0:
                    try:
                        href_p = await _exec_js(tab, "location.href")
                        st_p = await detect_page_step(tab)
                        log.info(
                            "success-poll %s step=%s href=%s",
                            poll_i,
                            st_p,
                            str(href_p or "")[:100],
                        )
                    except Exception:
                        pass
                page_err = await detect_page_error(tab)

                # --- Verification failed: recover ONCE (soft), never spam ---
                if page_err == "verification_failed":
                    log.error("Page: Verification failed (Turnstile/xAI/CF)")
                    if verify_recoveries >= 1:
                        status = "error:verification_failed"
                        break
                    verify_recoveries += 1
                    # Soft recover: check CF failure page → wait → ONE soft solve → resubmit once
                    cf_st = await _cf_page_state(tab)
                    log.warning("Recover verification_failed once (cf_state=%s)", cf_st)
                    if cf_st == "failed":
                        await asyncio.sleep(random.uniform(12.0, 18.0))
                        try:
                            await tab.go_to("https://accounts.x.ai/sign-up")
                        except Exception:
                            pass
                        await asyncio.sleep(4.0)
                        status = "error:cf_verification_failed_page"
                        break
                    await force_click_cloudflare_checkbox(tab, wait_sec=25.0)
                    await wait_turnstile_token(tab, timeout=20.0)
                    # re-sync password if CF cleared it, then submit only
                    await ensure_complete_signup_password(tab, grok_password)
                    await click_complete_signup_button(tab)
                    submit_count += 1
                    await af.asleep(*_delay_bounds(config, 4.0, 6.0), label="after_verify_recover")
                    continue

                if page_err == "password_too_weak":
                    log.warning("Password too weak — re-set shared password once")
                    await ensure_complete_signup_password(tab, grok_password)
                    await click_complete_signup_button(tab)
                    await af.asleep(*_delay_bounds(config, 2.5, 4.0), label="after_pw_weak")
                    continue

                if page_err and page_err.startswith("rate_limit"):
                    status = f"error:{page_err[:100]}"
                    log.error("Rate limit on complete: %s", page_err)
                    break

                st = await detect_page_step(tab)
                # At most 2 total Complete clicks — spam causes Verification failed
                if st == "complete_signup" and submit_count < 2:
                    log.info(
                        "Still on complete form — second submit with field verify count=%s",
                        submit_count,
                    )
                    await click_turnstile_checkbox_robust(
                        tab, wait_sec=15.0, reason="2nd_complete"
                    )
                    await wait_turnstile_token(tab, timeout=12.0)
                    if not await complete_signup_fields_ok(
                        tab, first_name, last_name, grok_password
                    ):
                        await fill_complete_signup(
                            tab,
                            first_name,
                            last_name,
                            grok_password,
                            click_submit=False,
                            fill_fields=True,
                        )
                    else:
                        await ensure_complete_signup_password(tab, grok_password)
                    snap2 = await read_complete_signup_fields(tab)
                    log.info("2nd complete pre-submit: %s", snap2)
                    await click_complete_signup_button(tab)
                    submit_count += 1
                    await af.asleep(*_delay_bounds(config, 3.0, 5.0), label="after_2nd_complete")
                    continue

                if st in ("done",) or await page_looks_success(tab):
                    success = True
                    break

                # mild continue only if not on complete/otp/sign-in (sign-in has own login path)
                href_mild = str(await _exec_js(tab, "location.href") or "").lower()
                if (
                    st not in ("complete_signup", "otp")
                    and "sign-in" not in href_mild
                    and poll_i > 3
                    and poll_i % 4 == 0
                ):
                    await human_click_button_by_text(
                        tab,
                        ["continue", "next", "get started", "done", "skip"],
                        timeout=2,
                        exclude_social=True,
                        config=config,
                    )
                await af.asleep(*_delay_bounds(config, 2.5, 4.5), label="success_poll")
            if success:
                # Prefer SSO already stashed after Complete
                sso_tok = str(config.get("_last_sso_cookie") or "").strip()
                user_id = ""

                # Double-check we are NOT on sign-in (false success bug)
                if not await page_is_logged_in(tab):
                    log.warning(
                        "success flag but page_is_logged_in false — "
                        "wait redirect / force account hop / use SSO cookie"
                    )
                    for _ in range(4):
                        await asyncio.sleep(1.5)
                        if await page_is_logged_in(tab):
                            break
                logged = await page_is_logged_in(tab)
                if not logged and sso_tok:
                    # SSO cookie is enough — skip UI login that dies on CF iframe
                    logged = True
                    log.info(
                        "Accepting SSO cookie as session proof (skip CF-stuck login UI)"
                    )
                if not logged:
                    logged = await ensure_logged_in_landing(
                        tab,
                        config,
                        email=email_session.address,
                        password=grok_password,
                    )
                if logged:
                    status = "success"
                    log.info(
                        "Registration SUCCESS + session ready (UI and/or SSO cookie)"
                    )
                    # Capture real SSO cookie (HttpOnly) for Sub2API sso-to-oauth
                    try:
                        from grokreg.delivery.sso_capture import (
                            capture_sso_cookie,
                            capture_session_display,
                            sso_preview,
                        )

                        try:
                            await tab.go_to("https://accounts.x.ai/account")
                            await asyncio.sleep(1.5)
                        except Exception:
                            pass
                        user_id, sso2 = await capture_session_display(tab)
                        if sso2:
                            sso_tok = sso2
                        if not sso_tok:
                            sso_tok = await capture_sso_cookie(
                                tab, navigate_if_needed=True
                            )
                        if sso_tok:
                            log.info(
                                "SSO cookie captured: %s",
                                sso_preview(sso_tok),
                            )
                        else:
                            user_id2, sso3 = await _extract_session_display(tab)
                            user_id = user_id or user_id2
                            sso_tok = sso3 or sso_tok
                            if not sso_tok:
                                log.warning(
                                    "SSO cookie missing after login — Sub2API will use browser OAuth"
                                )
                    except Exception as e:
                        log.debug("SSO capture display: %s", e)
                        try:
                            user_id, sso3 = await _extract_session_display(tab)
                            sso_tok = sso3 or sso_tok
                        except Exception:
                            pass
                    # stash on config for Sub2API step (same register_one call)
                    try:
                        config["_last_sso_cookie"] = sso_tok or ""
                    except Exception:
                        pass
                    try:
                        href_ok = str(await _exec_js(tab, "location.href") or "")
                    except Exception:
                        href_ok = "https://accounts.x.ai/account"
                    if "account" in href_ok.lower() or "grok.com" in href_ok.lower():
                        slog.api_ok(
                            f"Đã xác nhận hoàn tất tạo tài khoản xAI: {href_ok[:80]}"
                        )
                    slog.api_info(
                        "🔄",
                        "Chuyển hướng sang grok.com để đồng bộ Session & Lấy UserId...",
                    )
                    slog.api_ok(
                        f"Bắt được Session & UserId thành công: {user_id or '(n/a)'}"
                    )
                    slog.success_block(
                        email_session.address,
                        grok_password,
                        user_id=user_id,
                        sso_token=sso_tok,
                    )
                else:
                    # still mark success if complete form passed, but flag session
                    status = "success_not_logged_in"
                    log.error(
                        "Reg form done but browser NOT on logged-in Grok — check manually"
                    )
                    slog.api_err(
                        "Reg form xong nhưng chưa xác nhận logged-in Grok — check manual"
                    )
            elif str(status).startswith("error:") and status != "pending":
                log.warning("Registration failed: %s", status)
                slog.api_err(f"Registration failed: {status}")
            else:
                # pending / unclear after complete form — try credential login landing
                href = str(await _exec_js(tab, "location.href") or "")
                log.warning(
                    "Unclear/pending status=%s href=%s — try login landing",
                    status,
                    href[:100],
                )
                logged = await ensure_logged_in_landing(
                    tab,
                    config,
                    email=email_session.address,
                    password=grok_password,
                )
                status = "success" if logged else "manual_check"
                await asyncio.sleep(3)

            # ---- Pipeline: reg success → Storage State & Sub2API ----
            if status == "success" and handle is not None:
                if bool(config.get("save_storage_state", True)) and tab is not None:
                    try:
                        from grokreg.delivery.sso_capture import save_storage_state, get_all_cookies
                        cookies = await get_all_cookies(tab)
                        save_storage_state(email_session.address, cookies)
                    except Exception as exc:
                        log.debug("[storage_state] auto-save session error: %s", exc)

                sub_cfg = (config.get("sub2api") or {})
                if sub_cfg.get("enabled", True):
                    mode = str(sub_cfg.get("mode") or "auto")
                    log.info(
                        ">>> REG OK → Sub2API import (mode=%s, SSO API preferred) <<<",
                        mode,
                    )
                    await af.asleep(*_delay_bounds(config, 2.0, 5.0), label="before_sub2api")
                    try:
                        from grokreg.delivery.sub2api_oauth import add_grok_to_sub2api

                        sso_for_api = str(config.get("_last_sso_cookie") or "")
                        s2 = await add_grok_to_sub2api(
                            handle.browser,
                            tab,
                            config,
                            email_session.address,
                            grok_password,
                            sso_cookie=sso_for_api or None,
                        )
                        if s2.ok:
                            status = f"added_sub2api:{s2.name}"
                            log.info(
                                "Sub2API OK name=%s stage=%s msg=%s",
                                s2.name,
                                s2.stage,
                                s2.message,
                            )
                            slog.sub2api_ok(email_session.address)
                        else:
                            # created-but-test-fail still counts as usable import
                            if (
                                "created but test" in (s2.message or "").lower()
                                or s2.stage == "test"
                            ):
                                status = f"added_sub2api_untested:{s2.name}"
                                log.warning(
                                    "Sub2API account created, test skipped/failed: %s",
                                    s2.message[:100],
                                )
                                slog.sub2api_ok(email_session.address)
                            else:
                                # Reg still success; durable queue may retry SSO later
                                status = f"success_sub2api_fail:{s2.stage}:{s2.message[:80]}"
                                log.error(
                                    "Sub2API FAIL stage=%s name=%s msg=%s (reg kept; durable retry if SSO queued)",
                                    s2.stage,
                                    s2.name,
                                    s2.message,
                                )
                                slog.sub2api_fail(
                                    email_session.address,
                                    f"{s2.stage}:{s2.message[:60]}",
                                )
                    except Exception as e:
                        status = f"success_sub2api_fail:{str(e)[:80]}"
                        log.exception("Sub2API import error: %s", e)
                        slog.sub2api_fail(email_session.address, str(e)[:80])
                else:
                    log.info("sub2api.enabled=false — skip auto-import")
            # Final park: when user wants to inspect, ALWAYS end on logged-in Grok
            # (Sub2API OAuth may have navigated away to consent/admin pages)
            if (
                handle is not None
                and tab is not None
                and bool(config.get("keep_browser_open"))
                and (
                    str(status).startswith("success")
                    or str(status).startswith("added_sub2api")
                )
            ):
                log.info(">>> FINAL PARK: ensure LOGGED-IN page before leave-open <<<")
                try:
                    parked = await ensure_logged_in_landing(
                        tab,
                        config,
                        email=email_session.address,
                        password=grok_password,
                    )
                    final_href = str(await _exec_js(tab, "location.href") or "")
                    if parked:
                        log.info(
                            "PARKED LOGGED-IN for user: %s | account=%s",
                            final_href[:140],
                            email_session.address,
                        )
                    else:
                        log.error(
                            "PARK FAILED — still not logged-in href=%s account=%s",
                            final_href[:140],
                            email_session.address,
                        )
                        if status == "success":
                            status = "success_not_logged_in"
                except Exception as e:
                    log.warning("final park error: %s", e)

    except StopRequested as e:
        status = "stopped"
        log.warning("STOP mid-register: %s", e.reason)
        try:
            slog.api_info("🛑", f"ESC/STOP — hủy acc đang reg ({e.reason})")
        except Exception:
            pass

    except Exception as e:
        if isinstance(e, StopRequested):
            status = "stopped"
            log.warning("STOP mid-register: %s", e.reason)
        else:
            status = f"error:{str(e)[:100]}"
            log.exception("Fatal: %s", e)

    finally:
        # Close or leave open based on keep_browser_open / handle.keep_open
        if handle is not None:
            try:
                # Attach config so close_browser_handle can respect pull/keep flags
                try:
                    handle.config = config  # type: ignore[attr-defined]
                except Exception:
                    pass
                # Only leave Chrome open when we actually have a usable session.
                # On error (CF fail / email_field_not_found) closing avoids
                # confusing user with a guest/sign-in page after "done".
                st_l = str(status or "").lower()
                success_ish = (
                    st_l.startswith("success")
                    or st_l.startswith("added_sub2api")
                    or st_l in ("manual_check", "resumed_ok")
                )
                if bool(config.get("keep_browser_open")) and success_ish:
                    handle.keep_open = True
                    log.info(
                        "keep_browser_open: YES (status=%s) — leave on current page",
                        status,
                    )
                elif bool(config.get("keep_browser_open")) and not success_ish:
                    handle.keep_open = False
                    log.warning(
                        "keep_browser_open ignored — status=%s (not logged-in). Closing Chrome.",
                        status,
                    )
                elif bool(config.get("fresh_profile_per_account", True)):
                    handle.keep_open = False
                await close_browser_handle(handle)
            except Exception as e:
                log.debug("close handle: %s", e)
        else:
            if not bool(config.get("keep_browser_open")):
                try:
                    chrome_clean.kill_tool_chrome(reason="register_one_finally_no_handle")
                except Exception:
                    pass

    # Normal end save (early returns already saved + returned before this)
    # Note: early `return` inside try still runs finally, then skips this only if return was used.
    # Python: return in try executes finally then returns — code after try/finally is NOT run.
    # So this only runs when we fall through without return.
    save_account(save_path, email_session.address, grok_password, status)
    if hotmail and not keep_hotmail and not str(status).startswith("error:rate_limit"):
        hotmail.mark_used(email_session)
    elif hotmail and (keep_hotmail or str(status).startswith("error:rate_limit")):
        log.info("Hotmail NOT removed (rate-limit / keep): %s", email_session.address)
    log.info("Done. status=%s", status)

    # Push Google Sheet after accounts.txt is written (success ledger)
    st_final = str(status or "")
    if st_final.startswith("added_sub2api"):
        ok_sheet = push_results_to_gsheet(
            config, email_session.address
        )
        if not ok_sheet and (config.get("google_sheets") or {}).get(
            "require_sheet_success"
        ):
            log.error(
                "require_sheet_success=true but sheet push failed for %s",
                email_session.address,
            )

    return status



