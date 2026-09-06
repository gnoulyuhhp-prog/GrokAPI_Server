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


from grokreg.browser.chrome import *
# import * skips underscore names — pull CF/turnstile helpers explicitly
from grokreg.browser.chrome import (  # noqa: F401
    _cdp_click_xy,
    _cf_page_state,
    _cf_still_blocking,
    _turnstile_widget_info,
)
from grokreg.browser.jsutil import _exec_js, _unwrap_js_result  # noqa: F401
from grokreg.browser.network_castle import *
from grokreg.core.config import load_config
from grokreg.core.helpers import extract_otp, normalize_otp_for_input, save_account
from grokreg.mail.mail_api import EmailSession, MailApiClient
from grokreg.mail.providers import (
    MailTmProvider,
    AzpopMailProvider,
    HotmailProvider,
    wait_otp_smart,
)

async def prepare_and_submit_email(
    tab: Any, config: dict[str, Any], email_addr: str
) -> bool:
    """
    Align with working Grok bots:
      dismiss cookies → fill email (CDP keyboard) → Turnstile → submit continue.
    Returns True if continue was clicked.
    """
    try:
        await install_xai_fetch_sniffer(tab)
    except Exception as e:
        log.debug("fetch sniffer: %s", e)

    # Cookie/privacy modal blocks Sign up (diag showed VI cookie buttons still open)
    try:
        await dismiss_cookie_banner(tab)
        await asyncio.sleep(0.4)
        await dismiss_cookie_banner(tab)
    except Exception as e:
        log.debug("cookie pre-email: %s", e)

    email_strats = [
        {"css_selector": 'input[type="email"]'},
        {"css_selector": 'input[name="email"]'},
        {"css_selector": 'input[autocomplete="email"]'},
        {"css_selector": 'input[inputmode="email"]'},
    ]
    # Primary: CDP real keyboard (not React-only — that left form state empty)
    await type_into(tab, email_strats, email_addr, "email")
    got = await _dom_field_value(
        tab,
        [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
            'input[inputmode="email"]',
        ],
    )
    if email_addr.lower() not in (got or "").lower():
        log.warning(
            "Email field mismatch after CDP fill got=%r want=%r — second keyboard pass",
            got,
            email_addr,
        )
        # Second pass: force focus + tab.keyboard only
        el = await find_first(tab, email_strats, timeout=6)
        if el:
            await _cdp_clear_and_type(tab, el, email_addr, "email")
        got = await _dom_field_value(
            tab,
            [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
            ],
        )
    if email_addr.lower() not in (got or "").lower():
        log.warning("Still mismatch — React set as last resort got=%r", got)
        await _exec_js(tab, _react_set_value_js('input[type="email"]', email_addr))
        await _exec_js(tab, _react_set_value_js('input[name="email"]', email_addr))
        await _exec_js(tab, _react_set_value_js('input[autocomplete="email"]', email_addr))
        got = await _dom_field_value(tab, ['input[type="email"]', 'input[name="email"]'])
    log.info("Email DOM value before submit: %r", (got or "")[:60])

    # Castle mint right before submit (this was mint_failed in F12 capture)
    try:
        ctok = await castle_try_create_token(tab)
        log.info("Castle pre-submit mint: %s", ctok)
        if not ctok.get("ok"):
            # dwell + motion, try again once
            await asyncio.sleep(random.uniform(2.0, 4.0))
            await _exec_js(
                tab,
                f"""
                (() => {{
                  document.dispatchEvent(new MouseEvent('mousemove', {{
                    bubbles:true, clientX:{random.randint(120,700)},
                    clientY:{random.randint(120,480)}
                  }}));
                  document.dispatchEvent(new Event('focus', {{bubbles:true}}));
                  return 1;
                }})()
                """,
            )
            ctok = await castle_try_create_token(tab)
            log.info("Castle pre-submit mint retry: %s", ctok)
    except Exception as e:
        log.debug("castle pre-submit: %s", e)

    # Critical: tick CF human checkbox + wait token before email submit
    ts_timeout = float(config.get("turnstile_before_email_sec") or 18)
    info0 = await _turnstile_widget_info(tab)
    if info0.get("widgets") or info0.get("challengeText") or not info0.get("tokenReady"):
        log.info(
            "Turnstile before email: widgets=%s challengeText=%s tokenLen=%s — robust click",
            len(info0.get("widgets") or []),
            info0.get("challengeText"),
            info0.get("tokenLen"),
        )
        await click_turnstile_checkbox_robust(
            tab, wait_sec=max(ts_timeout, 25.0), reason="before_email_submit"
        )
    ok_ts = await wait_turnstile_token(tab, timeout=min(12.0, ts_timeout))
    if not ok_ts:
        log.warning("Turnstile token still empty after robust click — one more pass")
        await click_turnstile_checkbox_robust(tab, wait_sec=15.0, reason="email_retry")
        await wait_turnstile_token(tab, timeout=10.0)

    # last cookie dismiss right before click (banner can reappear)
    try:
        await dismiss_cookie_banner(tab)
    except Exception:
        pass
    await af.asleep(0.8, 1.6, label="pre_email_submit")
    clicked = await human_click_continue_after_email(tab, config)
    if not clicked:
        log.warning("Continue after email not found — try Enter key via JS")
        await _exec_js(
            tab,
            """
            (() => {
              const email = document.querySelector(
                'input[type="email"], input[name="email"], input[autocomplete="email"]'
              );
              if (!email) return false;
              email.focus();
              email.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
              }));
              email.dispatchEvent(new KeyboardEvent('keyup', {
                key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
              }));
              const form = email.closest('form');
              if (form) { form.requestSubmit ? form.requestSubmit() : form.submit(); }
              return true;
            })()
            """,
        )
        clicked = True
    return bool(clicked)


async def install_tos_fetch_interceptor(tab: Any) -> bool:
    """
    Minimal patch: only replace tosAcceptedVersion $undefined sentinel in string bodies.
    Do NOT JSON re-parse (breaks Next.js server actions / React Flight).
    """
    raw = await _exec_js(
        tab,
        r"""
        (() => {
          if (window.__grokTosPatchInstalled) return {ok:true, already:true};
          const fixBody = (body) => {
            if (typeof body !== 'string') return body;
            if (!body.includes('tosAcceptedVersion') && !body.includes('createUserAndSessionRequest'))
              return body;
            let s = body;
            // protobuf field tos_accepted_version is int32 — NOT a date string.
            // Server error when string: "invalid int32: NaN"
            // React Flight sends "$undefined" when form never set the version.
            const INT = '1';
            s = s.replace(/"tosAcceptedVersion"\s*:\s*"\$undefined"/g,
                          '"tosAcceptedVersion":' + INT);
            s = s.replace(/"tosAcceptedVersion"\s*:\s*"undefined"/g,
                          '"tosAcceptedVersion":' + INT);
            s = s.replace(/"tosAcceptedVersion"\s*:\s*null/g,
                          '"tosAcceptedVersion":' + INT);
            s = s.replace(/"tosAcceptedVersion"\s*:\s*undefined\b/g,
                          '"tosAcceptedVersion":' + INT);
            // fix our previous bad string patches / any non-int string
            s = s.replace(/"tosAcceptedVersion"\s*:\s*"[^"]*"/g,
                          '"tosAcceptedVersion":' + INT);
            // NaN if somehow present
            s = s.replace(/"tosAcceptedVersion"\s*:\s*NaN\b/g,
                          '"tosAcceptedVersion":' + INT);
            if (s !== body) window.__grokTosPatched = (window.__grokTosPatched || 0) + 1;
            return s;
          };
          const ofetch = window.fetch.bind(window);
          window.fetch = function(input, init) {
            try {
              if (init && typeof init.body === 'string') {
                const nb = fixBody(init.body);
                if (nb !== init.body) {
                  init = Object.assign({}, init, {body: nb});
                }
              }
            } catch (e) {}
            return ofetch(input, init);
          };
          const send = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.send = function(body) {
            try {
              if (typeof body === 'string') body = fixBody(body);
            } catch (e) {}
            return send.call(this, body);
          };
          window.__grokTosPatchInstalled = true;
          return {ok:true, installed:true};
        })()
        """,
    )
    ok = isinstance(raw, dict) and raw.get("ok")
    log.info("TOS fetch interceptor: %s", raw)
    return bool(ok)


async def accept_tos_on_complete_form(tab: Any) -> dict[str, Any]:
    """
    Critical: createUserAndSessionRequest.tosAcceptedVersion must NOT be $undefined.
    Network capture of failed signup (redirect → /sign-in) showed:
      tosAcceptedVersion: "$undefined"
    while client still logged "User successfully signed up with email"
    → account may exist but NO session cookie.
    Tick every TOS/privacy checkbox + set hidden tos version if present.
    """
    try:
        await install_tos_fetch_interceptor(tab)
    except Exception as e:
        log.debug("tos interceptor: %s", e)
    raw = await _exec_js(
        tab,
        """
        (() => {
          const out = {clicked: [], setHidden: [], checked: 0, versions: []};
          // 1) Visible checkboxes near terms / privacy / agree
          const boxes = [...document.querySelectorAll(
            'input[type=checkbox], [role=checkbox], button[role=checkbox]'
          )];
          for (const el of boxes) {
            const label = (
              (el.getAttribute('aria-label') || '') + ' ' +
              (el.name || '') + ' ' + (el.id || '') + ' ' +
              (el.closest('label')?.innerText || '') + ' ' +
              (el.parentElement?.innerText || '')
            ).toLowerCase().slice(0, 200);
            const isTos = /term|tos|privacy|agree|accept|policy|condition|điều khoản|chính sách/.test(label)
              || boxes.length <= 3; // few checkboxes → likely TOS only
            if (!isTos) continue;
            const checked = el.checked === true || el.getAttribute('aria-checked') === 'true'
              || el.getAttribute('data-state') === 'checked';
            if (!checked) {
              try { el.click(); out.clicked.push(label.slice(0, 60)); } catch(e) {}
              // React controlled checkbox
              try {
                const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
                if (desc && desc.set && 'checked' in el) {
                  desc.set.call(el, true);
                  el.dispatchEvent(new Event('input', {bubbles:true}));
                  el.dispatchEvent(new Event('change', {bubbles:true}));
                }
              } catch(e) {}
            }
            if (el.checked || el.getAttribute('aria-checked') === 'true'
                || el.getAttribute('data-state') === 'checked') out.checked += 1;
          }
          // 2) Hidden / text inputs for tos version
          for (const el of document.querySelectorAll('input, select')) {
            const n = ((el.name||'') + ' ' + (el.id||'') + ' ' + (el.getAttribute('data-testid')||'')).toLowerCase();
            if (!/tos|terms|accepted.?version|agreement/.test(n)) continue;
            const v = (el.value || '').trim();
            out.versions.push({name: el.name||el.id, value: v, type: el.type});
            if (!v || v === 'undefined' || v === '$undefined') {
              // common xAI TOS version patterns — prefer existing option
              let setTo = '1';
              if (el.tagName === 'SELECT' && el.options && el.options.length) {
                setTo = el.options[el.options.length - 1].value || '1';
              }
              const desc = Object.getOwnPropertyDescriptor(
                el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype,
                'value'
              );
              const prev = el.value;
              if (desc && desc.set) desc.set.call(el, setTo); else el.value = setTo;
              if (el._valueTracker) try { el._valueTracker.setValue(prev); } catch(e) {}
              el.dispatchEvent(new InputEvent('input', {bubbles:true, composed:true}));
              el.dispatchEvent(new Event('change', {bubbles:true}));
              out.setHidden.push({name: el.name||el.id, to: setTo});
            }
          }
          // 3) Patch React state via window if form exposes it (best-effort)
          try {
            // Next.js server action payload sometimes reads from closed-over state;
            // force any data-tos / meta tag version into a global the form may read.
            const meta = document.querySelector('meta[name*=tos], meta[name*=terms]');
            if (meta && meta.content) out.metaTos = meta.content;
          } catch(e) {}
          // 4) Click "I agree" text buttons if present
          for (const b of document.querySelectorAll('button, [role=button], label')) {
            const t = (b.innerText || '').replace(/\\s+/g,' ').trim().toLowerCase();
            if (!t || t.length > 80) continue;
            if (/^(i agree|agree|accept terms|accept all|đồng ý)/.test(t)
                || (t.includes('agree') && t.includes('term'))) {
              try { b.click(); out.clicked.push(t.slice(0, 50)); } catch(e) {}
            }
          }
          return out;
        })()
        """,
    )
    info = raw if isinstance(raw, dict) else {"raw": str(raw)[:200]}
    log.info("TOS accept on complete form: %s", info)
    return info


async def click_complete_signup_button(tab: Any) -> bool:
    """Click Complete sign up only — do NOT re-type password/name."""
    # MUST accept TOS first — otherwise tosAcceptedVersion=$undefined → no session
    try:
        await accept_tos_on_complete_form(tab)
    except Exception as e:
        log.warning("TOS accept failed: %s", e)
    await af.asleep(0.4, 0.9, label="pre_complete_click_only")

    # Wait until Complete button is enabled (disabled while validating)
    for i in range(10):
        st = await read_complete_signup_fields(tab)
        if st.get("btnDisabled") is False:
            break
        log.info("Complete button disabled — wait validate… try=%s", i + 1)
        await asyncio.sleep(0.8)

    # Prefer real CDP coord click (JS click often no-ops on disabled/loading buttons)
    coords = await _exec_js(
        tab,
        """
        (() => {
          const btns = [...document.querySelectorAll('button, [role=button], input[type=submit]')];
          for (const b of btns) {
            const t = (b.innerText || b.value || '').replace(/\\s+/g,' ').trim().toLowerCase();
            if (t === 'complete sign up' || t.includes('complete sign up')) {
              // force-enable if still disabled after wait (loading stuck)
              if (b.disabled) { b.disabled = false; b.removeAttribute('disabled'); }
              if (b.getAttribute('aria-disabled') === 'true')
                b.setAttribute('aria-disabled', 'false');
              const r = b.getBoundingClientRect();
              return {
                text: t, disabled: !!b.disabled,
                cx: r.left + r.width/2, cy: r.top + r.height/2,
                w: r.width, h: r.height
              };
            }
          }
          return null;
        })()
        """,
    )
    clicked = False
    if isinstance(coords, dict) and coords.get("cx"):
        log.info("Complete button coords: %s", coords)
        try:
            ok = await _cdp_click_xy(tab, float(coords["cx"]), float(coords["cy"]))
            clicked = bool(ok)
            if clicked:
                log.info("CDP coord click Complete sign up")
        except Exception as e:
            log.debug("CDP complete click: %s", e)

    if not clicked:
        clicked = await click_button_by_text(
            tab,
            ["complete sign up", "complete your sign up", "create account"],
            exclude_social=True,
        )
    if not clicked:
        r = await _exec_js(
            tab,
            """
            (() => {
              for (const b of document.querySelectorAll('button')) {
                const t = (b.innerText||'').trim().toLowerCase();
                if (t.includes('complete sign up') || t === 'complete sign up') {
                  b.disabled = false;
                  b.click();
                  // also try form requestSubmit
                  const f = b.closest('form');
                  if (f && f.requestSubmit) try { f.requestSubmit(b); } catch(e) {}
                  return t;
                }
              }
              const sub = document.querySelector('button[type=submit], input[type=submit]');
              if (sub) { sub.disabled = false; sub.click(); return 'submit'; }
              const f = document.querySelector('form');
              if (f) {
                if (f.requestSubmit) f.requestSubmit(); else f.submit();
                return 'form';
              }
              return null;
            })()
            """,
        )
        clicked = bool(r)
        log.info("JS Complete fallback: %s", r)
    if clicked:
        log.info("Clicked Complete sign up (submit only — no re-fill password)")
    return bool(clicked)


async def ensure_complete_signup_password(tab: Any, password: str) -> None:
    """If password field was cleared (e.g. after CF), set it once via React — no full retype."""
    got = await _exec_js(
        tab,
        f"""
        (() => {{
          const pass = {json.dumps(password)};
          const pw = document.querySelector(
            'input[name="password"], input[type="password"], input[autocomplete="new-password"]'
          );
          if (!pw) return {{ok:false, reason:'no_pw'}};
          const cur = (pw.value || '');
          if (cur === pass) return {{ok:true, already:true}};
          const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
          const prev = pw.value;
          if (desc && desc.set) desc.set.call(pw, pass); else pw.value = pass;
          if (pw._valueTracker) try {{ pw._valueTracker.setValue(prev); }} catch(e) {{}}
          pw.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:pass}}));
          pw.dispatchEvent(new Event('change', {{bubbles:true}}));
          return {{ok: pw.value === pass, set:true}};
        }})()
        """,
    )
    if isinstance(got, dict) and got.get("set"):
        log.info("Password field re-synced after CF (was empty/changed) — no full retype")
    elif isinstance(got, dict) and got.get("already"):
        log.debug("Password field still intact after CF")


async def read_complete_signup_fields(tab: Any) -> dict[str, Any]:
    """Read live Complete-signup form values + visibility (debug empty-submit bugs)."""
    raw = await _exec_js(
        tab,
        """
        (() => {
          const vis = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none';
          };
          const given = document.querySelector(
            'input[name="givenName"], input[autocomplete="given-name"], input[name="firstName"]'
          );
          const family = document.querySelector(
            'input[name="familyName"], input[autocomplete="family-name"], input[name="lastName"]'
          );
          const pw = document.querySelector(
            'input[name="password"], input[type="password"], input[autocomplete="new-password"]'
          );
          const btn = [...document.querySelectorAll('button')].find(b =>
            /complete sign up/i.test((b.innerText||'').trim())
          );
          return {
            href: location.href,
            title: document.title,
            visibility: document.visibilityState,
            first: given ? (given.value || '') : null,
            last: family ? (family.value || '') : null,
            passLen: pw ? (pw.value || '').length : 0,
            passName: pw ? (pw.name || '') : null,
            passType: pw ? (pw.type || '') : null,
            givenVis: vis(given), familyVis: vis(family), pwVis: vis(pw),
            btnDisabled: btn ? !!btn.disabled : null,
            bodySnip: ((document.body && document.body.innerText) || '').slice(0, 160)
              .replace(/\\s+/g, ' '),
          };
        })()
        """,
    )
    return raw if isinstance(raw, dict) else {"raw": str(raw)[:200]}


async def complete_signup_fields_ok(
    tab: Any, first_name: str, last_name: str, password: str
) -> bool:
    """True when name+password are actually in the form (not empty React state)."""
    st = await read_complete_signup_fields(tab)
    first_ok = (st.get("first") or "") == first_name or (
        first_name and first_name in str(st.get("first") or "")
    )
    last_ok = (st.get("last") or "") == last_name or (
        last_name and last_name in str(st.get("last") or "")
    )
    pass_ok = int(st.get("passLen") or 0) >= max(8, min(12, len(password)))
    # stricter when password known
    if password and st.get("passLen") == len(password):
        pass_ok = True
    ok = bool(first_ok and last_ok and pass_ok)
    log.info(
        "Complete form check ok=%s first=%r last=%r passLen=%s vis=%s/%s/%s pageVis=%s",
        ok,
        (st.get("first") or "")[:20],
        (st.get("last") or "")[:20],
        st.get("passLen"),
        st.get("givenVis"),
        st.get("familyVis"),
        st.get("pwVis"),
        st.get("visibility"),
    )
    return ok


async def fill_complete_signup(
    tab: Any,
    first_name: str,
    last_name: str,
    password: str,
    *,
    click_submit: bool = False,
    fill_fields: bool = True,
) -> bool:
    """
    xAI single page after OTP:
      Complete your sign up
      First name | Last name | Password
      [Complete sign up]

    fill_fields=False → only click submit (used after CF tick so we don't type password twice).
    """
    if not fill_fields:
        if click_submit:
            await ensure_complete_signup_password(tab, password)
            return await click_complete_signup_button(tab)
        return True

    log.info("Filling Complete sign up form: %s %s (password once)", first_name, last_name)

    # JS-only visibility; do not steal OS focus (chrome_steal_focus=false)
    await _exec_js(
        tab,
        """
        (() => {
          // scroll form into view
          const el = document.querySelector(
            'input[name="givenName"], input[autocomplete="given-name"], input[name="password"]'
          );
          if (el) el.scrollIntoView({block:'center', behavior:'instant'});
          window.focus();
          return document.visibilityState;
        })()
        """,
    )

    # First name — only given-name, never full name into first field
    ok1 = await type_into(
        tab,
        [
            {"css_selector": 'input[name="givenName"]'},
            {"css_selector": 'input[autocomplete="given-name"]'},
            {"css_selector": 'input[name="firstName"]'},
            {"css_selector": 'input[name="first_name"]'},
            {"css_selector": 'input[placeholder*="First"]'},
            {"css_selector": 'input[aria-label*="First"]'},
            {"tag_name": "input", "attributes": {"autocomplete": "given-name"}},
        ],
        first_name,
        "first name",
    )
    await _exec_js(tab, _react_set_value_js('input[name="givenName"]', first_name))
    await _exec_js(tab, _react_set_value_js('input[autocomplete="given-name"]', first_name))
    await _exec_js(tab, _react_set_value_js('input[name="firstName"]', first_name))

    ok2 = await type_into(
        tab,
        [
            {"css_selector": 'input[name="familyName"]'},
            {"css_selector": 'input[autocomplete="family-name"]'},
            {"css_selector": 'input[name="lastName"]'},
            {"css_selector": 'input[name="last_name"]'},
            {"css_selector": 'input[placeholder*="Last"]'},
            {"css_selector": 'input[aria-label*="Last"]'},
            {"tag_name": "input", "attributes": {"autocomplete": "family-name"}},
        ],
        last_name,
        "last name",
    )
    await _exec_js(tab, _react_set_value_js('input[name="familyName"]', last_name))
    await _exec_js(tab, _react_set_value_js('input[autocomplete="family-name"]', last_name))
    await _exec_js(tab, _react_set_value_js('input[name="lastName"]', last_name))

    # xAI often uses input[name=password] with type="text" (not type=password)
    ok3 = await type_into(
        tab,
        [
            {"css_selector": 'input[name="password"]'},
            {"css_selector": 'input[type="password"]'},
            {"css_selector": 'input[autocomplete="new-password"]'},
            {"css_selector": 'input[autocomplete="current-password"]'},
            {"tag_name": "input", "attributes": {"name": "password"}},
            {"tag_name": "input", "attributes": {"type": "password"}},
        ],
        password,
        "password",
    )
    await _exec_js(tab, _react_set_value_js('input[name="password"]', password))
    await _exec_js(tab, _react_set_value_js('input[type="password"]', password))

    # Multi-field React force — name=password may be type=text on xAI
    await _exec_js(
        tab,
        f"""
        (() => {{
          const first = {json.dumps(first_name)};
          const last = {json.dumps(last_name)};
          const pass = {json.dumps(password)};
          const setReact = (el, val) => {{
            if (!el) return;
            el.focus();
            const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            const prev = el.value;
            if (desc && desc.set) desc.set.call(el, '');
            if (el._valueTracker) try {{ el._valueTracker.setValue(prev); }} catch(e) {{}}
            if (desc && desc.set) desc.set.call(el, val); else el.value = val;
            if (el._valueTracker) try {{ el._valueTracker.setValue(prev); }} catch(e) {{}}
            el.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:val}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            el.dispatchEvent(new Event('blur', {{bubbles:true}}));
          }};
          const given = document.querySelector('input[name="givenName"], input[autocomplete="given-name"]');
          const family = document.querySelector('input[name="familyName"], input[autocomplete="family-name"]');
          // password: name=password first (xAI uses type=text), then type=password
          const pw = document.querySelector('input[name="password"], input[type="password"], input[autocomplete="new-password"]');
          setReact(given, first);
          setReact(family, last);
          setReact(pw, pass);
          return {{
            first: given && given.value,
            last: family && family.value,
            pass: pw && pw.value,
            pwType: pw && pw.type,
            pwName: pw && pw.name,
          }};
        }})()
        """,
    )
    log.info("Complete signup fields filled once (first=%s last=%s pw=%s)", ok1, ok2, ok3)
    await asyncio.sleep(0.5)

    if not click_submit:
        return True

    # Submit only — caller should have already ticked Turnstile
    return await click_complete_signup_button(tab)


async def extract_email_from_otp_page(tab: Any) -> Optional[str]:
    """Parse 'We've emailed ... to user@outlook.com' from OTP step."""
    pure = r"""
    (() => {
      const t = document.body && document.body.innerText || '';
      const m = t.match(/([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})/);
      return m ? m[1] : null;
    })()
    """
    v = await _exec_js(tab, pure)
    if isinstance(v, str) and "@" in v:
        return v.strip()
    return None


async def fill_otp_on_page(tab: Any, otp: str) -> bool:
    """
    Fill ONLY verification code field (never first/last name).
    Code WITHOUT hyphen (YI2-BKR → YI2BKR).
    """
    otp_input = normalize_otp_for_input(otp)
    log.info(
        ">>> FILL OTP raw/display=%r normalized_input=%r (len=%s) <<<",
        otp,
        otp_input,
        len(otp_input),
    )
    # Wait for real OTP field — do NOT wait input[type=text] (matches name fields)
    await wait_for_selector_js(
        tab,
        [
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[placeholder*="code" i]',
            'input[aria-label*="code" i]',
        ],
        15,
    )
    # Focus code field only via JS (robust when pydoll says not visible)
    focused = await _exec_js(
        tab,
        """
        (() => {
          const sels = [
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name="otp"]',
            'input[id*="code"]',
            'input[placeholder*="code" i]',
          ];
          for (const s of sels) {
            const el = document.querySelector(s);
            if (!el) continue;
            // never use name fields
            const meta = ((el.name||'')+(el.id||'')+(el.autocomplete||'')+(el.placeholder||'')).toLowerCase();
            if (meta.includes('given') || meta.includes('family') || meta.includes('first')
                || meta.includes('last') || meta.includes('password') || meta.includes('email'))
              continue;
            el.scrollIntoView({block:'center'});
            el.focus();
            try { el.select(); } catch(e) {}
            try { el.click(); } catch(e) {}
            return {ok:true, sel:s, name: el.name||'', auto: el.autocomplete||''};
          }
          return {ok:false};
        })()
        """,
    )
    log.info("OTP focus: %s", focused)

    # Prefer CDP keyboard into focused field
    try:
        kb = getattr(tab, "keyboard", None)
        if kb is not None:
            # clear
            try:
                from pydoll.constants import Key
                from pydoll.protocol.input.types import KeyModifier

                await kb.press(Key.A, modifiers=KeyModifier.CTRL)
                await asyncio.sleep(0.05)
                await kb.press(Key.BACKSPACE)
            except Exception:
                pass
            await kb.type_text(otp_input, humanize=False)
            log.info("Typed OTP via tab.keyboard len=%s", len(otp_input))
    except Exception as e:
        log.warning("OTP keyboard type failed: %s", e)

    # Always force React set on code field only (never generic type_into fallback)
    got = await _exec_js(
        tab,
        f"""
        (() => {{
          const val = {json.dumps(otp_input)};
          const sels = [
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name="otp"]',
          ];
          for (const s of sels) {{
            const el = document.querySelector(s);
            if (!el) continue;
            const meta = ((el.name||'')+(el.autocomplete||'')+(el.placeholder||'')).toLowerCase();
            if (meta.includes('given') || meta.includes('family') || meta.includes('password')
                || meta.includes('email') || meta.includes('first') || meta.includes('last'))
              continue;
            el.focus();
            const proto = HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            const last = el.value;
            if (desc && desc.set) {{
              desc.set.call(el, '');
              if (el._valueTracker) try {{ el._valueTracker.setValue(last); }} catch(e) {{}}
              el.dispatchEvent(new Event('input', {{bubbles:true}}));
              desc.set.call(el, val);
              if (el._valueTracker) try {{ el._valueTracker.setValue(''); }} catch(e) {{}}
            }} else el.value = val;
            el.dispatchEvent(new InputEvent('input', {{
              bubbles:true, composed:true, inputType:'insertText', data: val
            }}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            el.dispatchEvent(new Event('blur', {{bubbles:true}}));
            const v = (el.value || '').replace(/[^A-Za-z0-9]/gi,'').toUpperCase();
            return {{ok: v === val, value: el.value||'', sel: s}};
          }}
          return {{ok:false, reason:'no_code_field'}};
        }})()
        """,
    )
    log.info("OTP force-set: %s", got)
    verified = False
    if isinstance(got, dict) and got.get("ok"):
        verified = True
        log.info("OTP field OK: %s", got.get("value"))
    if not verified:
        # one more check
        check = await _exec_js(
            tab,
            f"""
            (() => {{
              const el = document.querySelector('input[name="code"], input[autocomplete="one-time-code"]');
              if (!el) return null;
              const v = (el.value||'').replace(/[^A-Za-z0-9]/gi,'').toUpperCase();
              return v === {json.dumps(otp_input)} ? (el.value||v) : null;
            }})()
            """,
        )
        if check:
            verified = True
            log.info("OTP field OK (recheck): %s", check)
    if not verified:
        log.error("OTP field still incomplete after fill (got=%s)", got)
        return False

    await asyncio.sleep(0.4)
    await click_button_by_text(
        tab, ["confirm email", "confirm", "continue", "verify"], exclude_social=True
    )
    await asyncio.sleep(3)
    return True


def find_hotmail_session_for_email(
    hotmail: HotmailProvider, email: str, default_client_id: str
) -> Optional[EmailSession]:
    """Match hotmails.txt line for an email already on the OTP page.

    Accepts plus-aliases (``user+2@domain``) and maps them back to the mailbox.
    """
    from grokreg.mail import hotmail_alias as halt

    email_l = (email or "").strip().lower()
    max_a = getattr(hotmail, "max_aliases", 5)
    for raw in hotmail._read_lines():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2:
            continue
        mailbox = parts[0]
        if not halt.alias_matches_mailbox(email_l, mailbox, max_a) and mailbox.lower() != email_l:
            continue
        idx = halt.alias_index_of(email_l, mailbox)
        if hasattr(hotmail, "_build_session"):
            sess = hotmail._build_session(
                raw, alias_index=idx, default_client_id=default_client_id
            )
            # Keep the page address if it already is the alias (index 0 = mailbox).
            if email_l:
                sess.address = email.strip()
            return sess
        return EmailSession(
            address=email.strip() or mailbox,
            password=parts[1],
            provider="hotmail",
            refresh_token=parts[2] if len(parts) >= 3 else "",
            client_id=parts[3] if len(parts) >= 4 else default_client_id,
            raw_line=raw,
            list_path=hotmail.list_path,
            mailbox=mailbox,
            extra={
                "mailbox": mailbox,
                "main_email": mailbox,
                "alias_index": idx,
                "max_aliases": max_a,
            },
        )
    return None



def _load_rate_limits() -> dict[str, Any]:
    if not RATE_LIMIT_PATH.exists():
        return {}
    try:
        return json.loads(RATE_LIMIT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_rate_limits(data: dict[str, Any]) -> None:
    RATE_LIMIT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def set_email_rate_limit(email: str, minutes: int = 55, note: str = "") -> None:
    data = _load_rate_limits()
    until = time.time() + max(1, minutes) * 60
    data[email.lower()] = {
        "until": until,
        "until_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
        "note": note or "otp_rate_limit",
    }
    _save_rate_limits(data)
    log.warning(
        "Rate-limit recorded for %s → retry after %s min (%s)",
        email,
        minutes,
        data[email.lower()]["until_iso"],
    )


def is_email_rate_limited(email: str) -> tuple[bool, int]:
    """Returns (limited, seconds_left)."""
    data = _load_rate_limits()
    row = data.get(email.lower())
    if not row:
        return False, 0
    until = float(row.get("until") or 0)
    left = int(until - time.time())
    if left <= 0:
        # expired — clean
        data.pop(email.lower(), None)
        _save_rate_limits(data)
        return False, 0
    return True, left


async def find_first(tab: Any, strategies: list[dict[str, Any]], timeout: int = 8) -> Any:
    per = max(1, timeout // max(1, len(strategies)))
    for s in strategies:
        try:
            kwargs = dict(s)
            kwargs["timeout"] = s.get("timeout", per)
            el = await tab.find(**kwargs)
            if el:
                return el
        except Exception:
            continue
    return None


def _unwrap_js_result(result: Any) -> Any:
    """Normalize pydoll/CDP evaluate return values."""
    if not isinstance(result, dict):
        return result
    # CDP shape: {id, result: {result: {type, value}}}
    try:
        inner = result
        for _ in range(6):
            if not isinstance(inner, dict):
                break
            # terminal CDP remote object
            if "type" in inner and ("value" in inner or inner.get("type") == "undefined"):
                val = inner.get("value")
                # auto-parse JSON strings we wrap ourselves
                if isinstance(val, str):
                    s = val.strip()
                    if (s.startswith("{") and s.endswith("}")) or (
                        s.startswith("[") and s.endswith("]")
                    ):
                        try:
                            return json.loads(s)
                        except Exception:
                            pass
                return val
            if "result" in inner:
                inner = inner["result"]
                continue
            if "value" in inner and len(inner) <= 3:
                return inner["value"]
            break
        return result
    except Exception:
        return result


async def _exec_js(tab: Any, script: str) -> Any:
    """
    Execute JS and return a Python value.
    pydoll returns objectId for objects unless return_by_value=True;
    we also JSON.stringify complex expressions as fallback.
    """
    # Prefer scripts that already stringify; otherwise wrap when needed
    script_stripped = script.strip()
    candidates = [script_stripped]
    # If it's an IIFE returning object, also try stringify wrap
    if script_stripped.startswith("(()") or script_stripped.startswith("(function"):
        candidates.append(
            f"(() => {{ const __r = ({script_stripped}); "
            f"try {{ return JSON.stringify(__r); }} catch (e) {{ return __r; }} }})()"
        )

    for method_name in ("execute_script", "evaluate"):
        if not hasattr(tab, method_name):
            continue
        fn = getattr(tab, method_name)
        for sc in candidates:
            try:
                try:
                    raw = await fn(sc, return_by_value=True)
                except TypeError:
                    raw = await fn(sc)
                val = _unwrap_js_result(raw)
                # skip useless objectId-only remote objects
                if isinstance(val, dict) and set(val.keys()) <= {
                    "id",
                    "result",
                    "type",
                    "className",
                    "description",
                    "objectId",
                }:
                    continue
                return val
            except Exception:
                continue
    return None


# Social / OAuth CTAs — never treat these as the email-register Continue button
_SOCIAL_BLOCKLIST = (
    "with x",
    "with twitter",
    "with google",
    "with apple",
    "with facebook",
    "with microsoft",
    "with github",
    "sign in with",
    "sign up with",
    "continue with",
    "log in with",
    "login with",
    "oauth",
    "sso",
)


def _is_social_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(b in t for b in _SOCIAL_BLOCKLIST)


async def click_button_by_text(
    tab: Any,
    texts: list[str],
    timeout: int = 6,
    *,
    exact_first: bool = True,
    exclude_social: bool = True,
) -> bool:
    """
    Click a primary action button by label.
    Prefer exact text match; never click social/OAuth buttons when exclude_social=True.
    """
    targets = [t.lower().strip() for t in texts if t.strip()]
    block = list(_SOCIAL_BLOCKLIST) if exclude_social else []

    # pydoll text find — only try exact-ish labels (skip broad "sign up" via find)
    for t in texts:
        if exclude_social and _is_social_label(t):
            continue
        # Skip ultra-broad tokens that match social CTAs via substring find
        if t.lower().strip() in {"sign up", "sign in", "log in", "login"}:
            continue
        try:
            el = await tab.find(tag_name="button", text=t, timeout=1)
            if el:
                await el.click()
                log.info("Clicked button: %s", t)
                return True
        except Exception:
            pass

    lit_targets = json.dumps(targets)
    lit_block = json.dumps(block)
    pure = f"""
    (() => {{
      const targets = {lit_targets};
      const block = {lit_block};
      const exactFirst = {str(exact_first).lower()};

      const labelOf = (n) => (n.innerText || n.value || n.getAttribute('aria-label')
        || n.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().toLowerCase();

      const isBlocked = (t) => {{
        if (!t) return true;
        for (const b of block) {{
          if (t.includes(b)) return true;
        }}
        // also block if looks like "… with <provider>"
        if (/\\bwith\\s+(x|google|apple|facebook|microsoft|github|twitter)\\b/.test(t)) return true;
        return false;
      }};

      const nodes = Array.from(document.querySelectorAll(
        'button, [role="button"], input[type="submit"], a[href]'
      )).filter(n => {{
        // visible-ish
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      }});

      const score = (t, want) => {{
        if (t === want) return 100;
        if (t.startsWith(want + ' ') || t.endsWith(' ' + want)) return 60;
        // allow contains only for multi-word wants like "create account"
        if (want.includes(' ') && t.includes(want)) return 50;
        if (!want.includes(' ') && t === want) return 100;
        return 0;
      }};

      // Pass 1: exact match
      if (exactFirst) {{
        for (const want of targets) {{
          for (const n of nodes) {{
            const t = labelOf(n);
            if (isBlocked(t)) continue;
            if (t === want) {{
              n.click();
              return t;
            }}
          }}
        }}
      }}

      // Pass 2: best fuzzy among non-social
      let best = null;
      let bestScore = 0;
      for (const n of nodes) {{
        const t = labelOf(n);
        if (isBlocked(t)) continue;
        for (const want of targets) {{
          // never match social-ish labels even if want is "sign up"
          if (isBlocked(t)) continue;
          let s = score(t, want);
          // weak contains only if button is type=submit / primary-ish and want is continue/next
          if (s === 0 && (want === 'continue' || want === 'next' || want === 'verify')) {{
            if (t.includes(want) && t.length <= want.length + 12) s = 40;
          }}
          if (s > bestScore) {{
            bestScore = s;
            best = {{ node: n, t, s }};
          }}
        }}
      }}
      if (best && bestScore >= 40) {{
        best.node.click();
        return best.t;
      }}
      return null;
    }})()
    """
    result = await _exec_js(tab, pure)
    # pydoll may wrap as CDP result dict
    if isinstance(result, dict):
        try:
            result = result.get("result", {}).get("result", {}).get("value", result)
        except Exception:
            pass
    if result:
        log.info("Clicked button: %s", result)
        return True
    return False


async def human_click_button_by_text(
    tab: Any,
    texts: list[str],
    timeout: int = 6,
    *,
    exact_first: bool = True,
    exclude_social: bool = True,
    config: dict[str, Any] | None = None,
) -> bool:
    """Click with pre-jiggle + human pause (important CTAs)."""
    antiflag = (config or {}).get("antiflag") or {}
    if antiflag.get("pre_click_jiggle", True):
        await af.human_pre_click(tab, _exec_js, af.asleep)
    else:
        await af.asleep(0.3, 0.9, label="pre_click_min")
    return await click_button_by_text(
        tab, texts, timeout, exact_first=exact_first, exclude_social=exclude_social
    )


async def click_continue_after_email(tab: Any) -> bool:
    """Only Continue/Next near the email form — never social signup."""
    pure = """
    (() => {
      const bad = /with\\s+(x|google|apple|facebook|microsoft|github|twitter)|sign up with|continue with|sign in with/i;
      const labelOf = (n) => (n.innerText || n.value || n.getAttribute('aria-label') || '')
        .replace(/\\s+/g, ' ').trim();

      // Prefer submit/continue inside same form as email input
      const email = document.querySelector(
        'input[type="email"], input[name="email"], input[autocomplete="email"]'
      );
      const roots = [];
      if (email) {
        const form = email.closest('form');
        if (form) roots.push(form);
        let p = email.parentElement;
        for (let i = 0; i < 6 && p; i++, p = p.parentElement) roots.push(p);
      }
      roots.push(document);

      const wantRe = /^(continue|next|submit|verify|sign up|xác nhận|tiếp tục)$/i;
      // reject version badges / noise (e.g. "v0.1.165")
      const junk = /v?\\d+\\.\\d+|cookie|privacy|terms|learn more|©/i;

      for (const root of roots) {
        const nodes = Array.from(root.querySelectorAll(
          'button, [role="button"], input[type="submit"]'
        ));
        // exact Continue/Next first
        for (const n of nodes) {
          const t = labelOf(n);
          if (!t || bad.test(t) || junk.test(t) || t.length > 24) continue;
          const r = n.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) continue;
          if (wantRe.test(t)) {
            n.click();
            return t;
          }
        }
        // type=submit in form (must look like a real CTA label)
        for (const n of nodes) {
          const t = labelOf(n);
          if (!t || bad.test(t) || junk.test(t) || t.length > 24) continue;
          if ((n.getAttribute('type') === 'submit' || n.type === 'submit')
              && /continue|next|submit|sign up|verify/i.test(t)) {
            n.click();
            return t || 'submit';
          }
        }
      }
      // last resort: primary button near email with short label
      if (email) {
        const form = email.closest('form') || email.parentElement;
        if (form) {
          for (const n of form.querySelectorAll('button, [role=button], input[type=submit]')) {
            const t = labelOf(n);
            if (!t || bad.test(t) || junk.test(t) || t.length > 20) continue;
            const r = n.getBoundingClientRect();
            if (r.width > 40 && r.height > 20) { n.click(); return t; }
          }
        }
      }
      return null;
    })()
    """
    result = await _exec_js(tab, pure)
    if isinstance(result, dict):
        try:
            result = result.get("result", {}).get("result", {}).get("value", result)
        except Exception:
            pass
    if result:
        log.info("Clicked email-form continue: %s", result)
        return True
    return False


async def human_click_continue_after_email(
    tab: Any, config: dict[str, Any] | None = None
) -> bool:
    antiflag = (config or {}).get("antiflag") or {}
    if antiflag.get("pre_click_jiggle", True):
        await af.human_pre_click(tab, _exec_js, af.asleep)
    ok = await click_continue_after_email(tab)
    if ok:
        return True
    # fallback human button labels
    return await human_click_button_by_text(
        tab,
        ["continue", "sign up", "next", "submit"],
        exclude_social=True,
        config=config,
    )


async def _element_value(el: Any) -> str:
    try:
        if hasattr(el, "value"):
            v = el.value
            if asyncio.iscoroutine(v):
                v = await v
            return str(v or "")
    except Exception:
        pass
    try:
        if hasattr(el, "get_attribute"):
            v = await el.get_attribute("value")
            return str(v or "")
    except Exception:
        pass
    return ""


def _react_set_value_js(css: str, value: str) -> str:
    """JS that updates React controlled inputs (fixes Zod: expected string, received undefined)."""
    return f"""
    (() => {{
      const val = {json.dumps(value)};
      const el = document.querySelector({json.dumps(css)});
      if (!el) return {{ok:false, reason:'not_found'}};
      el.focus();
      el.click();
      try {{ el.select(); }} catch (e) {{}}

      // React 16/17/18: reset _valueTracker so onChange fires
      const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      const last = el.value;
      if (desc && desc.set) {{
        desc.set.call(el, '');
        if (el._valueTracker) el._valueTracker.setValue(last);
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        desc.set.call(el, val);
        if (el._valueTracker) el._valueTracker.setValue('');
      }} else {{
        el.value = val;
      }}
      el.dispatchEvent(new InputEvent('input', {{
        bubbles: true, cancelable: true, composed: true,
        inputType: 'insertText', data: val
      }}));
      el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
      // also poke common form libs
      try {{
        const setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) setter.set.call(el, val);
      }} catch (e) {{}}
      return {{ok: el.value === val || (el.value||'').includes(val), value: el.value||''}};
    }})()
    """


async def _dom_field_value(tab: Any, css_list: list[str]) -> str:
    """Read live DOM value (more reliable than pydoll element cache)."""
    sels = [c for c in css_list if c]
    if not sels:
        return ""
    raw = await _exec_js(
        tab,
        f"""
        (() => {{
          const sels = {json.dumps(sels)};
          for (const s of sels) {{
            try {{
              const el = document.querySelector(s);
              if (el && (el.value || '').length) return el.value || '';
              if (el) return el.value || '';
            }} catch (e) {{}}
          }}
          return '';
        }})()
        """,
    )
    return str(raw or "")


async def _cdp_clear_and_type(tab: Any, el: Any, value: str, label: str) -> bool:
    """
    Real keyboard path via CDP (pydoll Keyboard / type_text).
    Avoids React-only value set that leaves form state undefined.
    """
    # Always scroll + JS focus first (works even if pydoll visibility check fails)
    try:
        await _exec_js(
            tab,
            """
            (() => {
              const sels = [
                'input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]',
                'input[name="password"]', 'input[type="password"]',
                'input[name="givenName"]', 'input[autocomplete="one-time-code"]'
              ];
              for (const s of sels) {
                const el = document.querySelector(s);
                if (!el) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 2 && r.height < 2) continue;
                el.scrollIntoView({block:'center', inline:'nearest'});
                el.focus();
                try { el.click(); } catch (e) {}
                return {ok:true, sel:s, y: r.top};
              }
              // last resort: first visible text-like input
              const inputs = [...document.querySelectorAll('input, textarea')].filter(el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width>2 && r.height>2 && st.visibility!=='hidden' && el.type!=='hidden';
              });
              if (inputs[0]) {
                inputs[0].scrollIntoView({block:'center'});
                inputs[0].focus();
                try { inputs[0].click(); } catch(e) {}
                return {ok:true, sel:'first-visible'};
              }
              return {ok:false};
            })()
            """,
        )
    except Exception as e:
        log.debug("js focus pre: %s", e)

    # Focus element via pydoll click when possible
    try:
        if hasattr(el, "click"):
            try:
                await el.click(humanize=True)
            except TypeError:
                await el.click()
            except Exception as e:
                # "not visible" — already focused via JS
                log.warning("pydoll click %s failed (%s) — using JS focus", label, e)
    except Exception as e:
        log.warning("focus %s failed: %s", label, e)

    await asyncio.sleep(random.uniform(0.12, 0.28))

    # Clear existing value: Ctrl+A then Backspace via tab.keyboard if present
    try:
        kb = getattr(tab, "keyboard", None)
        if kb is not None:
            try:
                # Prefer Key enums if available
                from pydoll.protocol.input.types import KeyModifier  # type: ignore
                from pydoll.constants import Key  # type: ignore

                await kb.press(Key.A, modifiers=KeyModifier.CTRL)
                await asyncio.sleep(0.05)
                await kb.press(Key.BACKSPACE)
            except Exception:
                # fallback: select-all via JS then keyboard type replaces
                await _exec_js(
                    tab,
                    """
                    (() => {
                      const el = document.activeElement;
                      if (el && typeof el.select === 'function') el.select();
                      else if (el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }
                      return true;
                    })()
                    """,
                )
        elif hasattr(el, "press_keyboard_key"):
            # older API — Key tuple may be required; try then ignore
            try:
                from pydoll.constants import Key  # type: ignore
                from pydoll.protocol.input.types import KeyModifier  # type: ignore

                await el.press_keyboard_key(Key.A, modifiers=KeyModifier.CTRL)
                await asyncio.sleep(0.05)
                await el.press_keyboard_key(Key.BACKSPACE)
            except Exception:
                await _exec_js(
                    tab,
                    "(() => { const el=document.activeElement; if(el&&el.select) el.select(); return 1; })()",
                )
    except Exception as e:
        log.debug("clear field %s: %s", label, e)

    await asyncio.sleep(0.08)

    # Type with real key events (humanize for email/name, faster for OTP)
    human = True
    if label and "otp" in label.lower():
        human = False  # codes: accurate first
    try:
        kb = getattr(tab, "keyboard", None)
        if kb is not None and hasattr(kb, "type_text"):
            await kb.type_text(value, humanize=human)
            log.info("Typed %s via tab.keyboard CDP chars=%s", label, len(value))
            return True
        if hasattr(el, "type_text"):
            try:
                await el.type_text(value, humanize=human)
            except TypeError:
                await el.type_text(value)
            log.info("Typed %s via el.type_text CDP chars=%s", label, len(value))
            return True
        if hasattr(el, "insert_text"):
            # pydoll insert_text uses JS — less ideal but better than nothing
            await el.insert_text(value)
            log.info("Filled %s via el.insert_text", label)
            return True
    except Exception as e:
        log.warning("CDP type %s failed: %s", label, e)
        return False
    return False


def _css_from_strategies(strategies: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in strategies:
        css = s.get("css_selector")
        if css:
            out.append(str(css))
            continue
        if s.get("tag_name") == "input" and s.get("attributes"):
            attrs = s["attributes"]
            parts = [s["tag_name"]]
            for ak, av in attrs.items():
                parts.append(f'[{ak}="{av}"]')
            out.append("".join(parts))
    return out


async def type_into(
    tab: Any, strategies: list[dict[str, Any]], value: str, label: str
) -> bool:
    """
    Fill React/controlled inputs properly.
    Priority: CDP real keyboard → verify live DOM → React tracker only as last resort.
    Root cause of "expected string, received undefined": DOM value set without
    React onChange → form state stays undefined on submit.
    """
    if value is None:
        log.error("type_into %s: value is None", label)
        return False
    value = str(value)
    css_list = _css_from_strategies(strategies)

    el = await find_first(tab, strategies, timeout=12)
    if el:
        # Method A: real CDP keyboard (best for xAI React forms)
        typed = await _cdp_clear_and_type(tab, el, value, label)
        await asyncio.sleep(af.short_pause(0.25, 0.6))
        # Verify via LIVE DOM — pydoll element cache often stale/empty
        cur = await _dom_field_value(tab, css_list)
        if not cur:
            cur = await _element_value(el)
        if cur == value or (value and value in (cur or "")):
            log.info("Filled %s via CDP keyboard ok=%r", label, (cur or "")[:48])
            # Nudge React: fire input/change without overwriting value
            await _exec_js(
                tab,
                f"""
                (() => {{
                  const sels = {json.dumps(css_list)};
                  for (const s of sels) {{
                    const el = document.querySelector(s);
                    if (!el) continue;
                    el.dispatchEvent(new InputEvent('input', {{
                      bubbles:true, composed:true, inputType:'insertText', data: el.value
                    }}));
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    return true;
                  }}
                  return false;
                }})()
                """,
            )
            return True
        if typed:
            log.warning(
                "CDP typed %s but DOM mismatch got=%r want=%r — try React reconcile",
                label,
                (cur or "")[:48],
                value[:48],
            )

    # Method B: React _valueTracker hack by CSS (last resort — competitor tools avoid this)
    for s in strategies:
        css = s.get("css_selector")
        if not css:
            if s.get("tag_name") == "input" and s.get("attributes"):
                attrs = s["attributes"]
                parts = [s["tag_name"]]
                for ak, av in attrs.items():
                    parts.append(f'[{ak}="{av}"]')
                css = "".join(parts)
            else:
                continue
        got = await _exec_js(tab, _react_set_value_js(css, value))
        ok = False
        if isinstance(got, dict):
            ok = bool(got.get("ok"))
            if ok:
                log.warning(
                    "Filled %s via React tracker JS (fallback) val=%r",
                    label,
                    str(got.get("value"))[:40],
                )
                return True
        elif got == value or (isinstance(got, str) and value in got):
            log.warning("Filled %s via JS fallback", label)
            return True

    # Method C: generic visible inputs + React _valueTracker (label-aware ranking)
    pure = f"""
    (() => {{
      const val = {json.dumps(value)};
      const label = {json.dumps(label)}.toLowerCase();
      const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(el => {{
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width > 2 && r.height > 2 && !el.disabled && el.type !== 'hidden'
          && st.visibility !== 'hidden' && st.display !== 'none';
      }});
      const meta = (el) => ((el.placeholder||'')+' '+(el.name||'')+' '+(el.id||'')+' '+(el.autocomplete||'')+' '+(el.type||'')+' '+(el.getAttribute('aria-label')||'')).toLowerCase();
      const ranked = inputs.map(el => {{
        const s = meta(el);
        let score = 0;
        if (label.includes('email') && (el.type==='email' || s.includes('email') || s.includes('mail'))) score += 50;
        if (label.includes('otp') || label.includes('code')) {{
          if (s.includes('code') || s.includes('otp') || s.includes('verif')) score += 50;
        }}
        if (label.includes('password') && (el.type==='password' || s.includes('password'))) score += 50;
        if (label.includes('name') && s.includes('name')) score += 40;
        if (!el.value) score += 5;
        return {{el, score, s}};
      }}).sort((a,b) => b.score - a.score);

      for (const item of ranked) {{
        const el = item.el;
        el.scrollIntoView({{block:'center'}});
        el.focus(); el.click();
        try {{ el.select(); }} catch (e) {{}}
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        const last = el.value;
        if (desc && desc.set) {{
          desc.set.call(el, val);
          if (el._valueTracker) try {{ el._valueTracker.setValue(last); }} catch(e) {{}}
        }} else el.value = val;
        el.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:val}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        el.dispatchEvent(new Event('blur', {{bubbles:true}}));
        if (el.value === val || (el.value||'').includes(val)) {{
          return {{ok:true, name: item.s.slice(0,60), value: el.value, score: item.score}};
        }}
      }}
      return {{ok:false, count: inputs.length}};
    }})()
    """
    got = await _exec_js(tab, pure)
    if isinstance(got, dict) and got.get("ok"):
        log.info(
            "Filled %s via React hack (score=%s, %s)",
            label,
            got.get("score"),
            got.get("name"),
        )
        return True
    log.warning(
        "Cannot find/fill field: %s (visible_inputs=%s)",
        label,
        got.get("count") if isinstance(got, dict) else "?",
    )
    return False


def _url_path_host(url: str) -> tuple[str, str]:
    """Return (hostname_lower, path_lower) safely."""
    try:
        from urllib.parse import urlparse

        p = urlparse(url or "")
        return (p.hostname or "").lower(), (p.path or "/").lower()
    except Exception:
        u = (url or "").lower()
        return u, u


async def page_is_logged_in(tab: Any) -> bool:
    """
    True only when session is authenticated — NOT guest / sign-in wall.
    Fixes false positive: '/account' matching hostname 'accounts.x.ai'.
    """
    pure = """
    (() => {
      const href = (location.href || '').toLowerCase();
      const path = (location.pathname || '/').toLowerCase();
      const host = (location.hostname || '').toLowerCase();
      const t = ((document.body && document.body.innerText) || '').toLowerCase();

      // Always reject auth pages
      if (/sign-in|sign-up|signup|login|oauth2\\/authorize/.test(path + ' ' + href))
        return {ok:false, reason:'auth_path', href};
      if (t.includes('sign in to continue') || t.includes('create your account'))
        return {ok:false, reason:'auth_copy', href};

      // accounts.x.ai logged-in account area (path must be /account not hostname)
      if (host === 'accounts.x.ai' || host.endsWith('.accounts.x.ai')) {
        if (path === '/account' || path.startsWith('/account/')
            || path.startsWith('/settings') || path.startsWith('/profile')
            || path.startsWith('/api-keys') || path.startsWith('/teams')) {
          return {ok:true, reason:'accounts_account', href};
        }
        // root with account UI
        if (t.includes('api keys') || t.includes('manage account') || t.includes('billing'))
          return {ok:true, reason:'accounts_ui', href};
        return {ok:false, reason:'accounts_not_dashboard', href};
      }

      // grok.com — need chat UI, not marketing/sign-in
      if (host === 'grok.com' || host.endsWith('.grok.com')) {
        const chat = document.querySelector(
          'textarea, [contenteditable="true"], form textarea, [data-testid*="chat"], [class*="chat"] input, [class*="Chat"] textarea'
        );
        const signInBtn = [...document.querySelectorAll('a,button')].some(el => {
          const s = ((el.innerText||el.textContent||'') + ' ' + (el.getAttribute('href')||'')).toLowerCase();
          return /^\\s*sign in\\s*$/i.test((el.innerText||'').trim()) || s.includes('/sign-in');
        });
        if (chat && chat.getBoundingClientRect().width > 20) {
          return {ok:true, reason:'grok_chat', href};
        }
        // logged-in often has user menu / new chat without forced sign-in CTA
        if (!signInBtn && (t.includes('new chat') || t.includes('what do you want to know')
            || t.includes('ask grok') || document.querySelector('nav, aside'))) {
          return {ok:true, reason:'grok_shell', href};
        }
        return {ok:false, reason:'grok_guest', href};
      }

      if (host.includes('console.x.ai')) {
        if (!/sign-in|login/.test(path)) return {ok:true, reason:'console', href};
      }
      return {ok:false, reason:'unknown', href};
    })()
    """
    try:
        r = await _exec_js(tab, pure)
        if isinstance(r, dict):
            ok = bool(r.get("ok"))
            if ok:
                log.info("Logged-in OK reason=%s href=%s", r.get("reason"), str(r.get("href") or "")[:100])
            else:
                log.debug("Not logged-in reason=%s href=%s", r.get("reason"), str(r.get("href") or "")[:100])
            return ok
        return bool(r)
    except Exception:
        return False


async def page_looks_success(tab: Any) -> bool:
    """True when signup finished AND session looks authenticated."""
    url = ""
    try:
        url = str(await _exec_js(tab, "window.location.href") or "")
    except Exception:
        url = ""
    url_l = str(url).lower()
    host, path = _url_path_host(url)

    # OAuth consent after signup = session exists (will click Allow in landing helper)
    if "/oauth2/consent" in path or "/oauth2/consent" in url_l:
        log.info("page_looks_success: oauth2/consent (session OK)")
        return True

    # Hard reject pure auth pages (BUGFIX: '/account' matched hostname 'accounts.x.ai')
    if any(x in path for x in ("/sign-in", "/sign-up", "/signup", "/login")):
        return False
    if path.startswith("/oauth2/authorize"):
        return False

    # accounts dashboard by PATH only (not hostname substring)
    if host.endswith("accounts.x.ai") or host == "accounts.x.ai":
        if path == "/account" or path.startswith("/account/") or path.startswith("/settings"):
            return True

    if host.endswith("grok.com") or host.endswith("console.x.ai"):
        return await page_is_logged_in(tab)

    return await page_is_logged_in(tab)


async def dismiss_oauth_consent_if_present(tab: Any) -> bool:
    """
    After signup, profile may land on leftover Sub2API OAuth consent
    (accounts.x.ai/oauth2/consent). That means user IS logged in — click Allow
    or just leave and navigate away.
    """
    href = str(await _exec_js(tab, "location.href") or "")
    if "oauth2/consent" not in href.lower() and "oauth2/consent" not in (
        await _exec_js(tab, "location.pathname") or ""
    ).lower():
        # also check pathname via href
        if "/oauth2/consent" not in href.lower():
            return False
    log.info("OAuth consent page detected (logged-in session) href=%s", href[:120])
    # Click Allow / Authorize if present
    clicked = await human_click_button_by_text(
        tab,
        [
            "allow",
            "authorize",
            "accept",
            "continue",
            "đồng ý",
            "cho phép",
            "xác nhận",
            "同意",
            "授权",
        ],
        timeout=4,
        exclude_social=True,
        config=None,
    )
    if clicked:
        log.info("Clicked OAuth consent Allow/Authorize")
        await asyncio.sleep(2.5)
    else:
        # force click via JS common consent CTAs
        r = await _exec_js(
            tab,
            """
            (() => {
              const want = /^(allow|authorize|accept|continue|đồng ý|cho phép)$/i;
              for (const b of document.querySelectorAll('button, [role=button], input[type=submit]')) {
                const t = (b.innerText || b.value || '').trim();
                if (want.test(t) || /allow|authorize/i.test(t)) { b.click(); return t; }
              }
              return null;
            })()
            """,
        )
        if r:
            log.info("JS consent click: %s", r)
            await asyncio.sleep(2.5)
    return True


async def _try_session_via_navigation(tab: Any) -> bool:
    """
    After Complete, session cookie may already exist but UI bounced to /sign-in.
    Probe /account and grok.com before password login.
    """
    for url in (
        "https://accounts.x.ai/account",
        "https://accounts.x.ai/",
        "https://grok.com/",
    ):
        try:
            await tab.go_to(url)
            await asyncio.sleep(2.5)
            await dismiss_cookie_banner(tab)
            try:
                await dismiss_oauth_consent_if_present(tab)
            except Exception:
                pass
            href = str(await _exec_js(tab, "location.href") or "")
            if await page_is_logged_in(tab) or await page_looks_success(tab):
                log.info("session via navigation OK → %s", href[:100])
                return True
            # still sign-in wall
            if "sign-in" in href.lower() or "sign-up" in href.lower():
                log.debug("nav %s still auth wall: %s", url, href[:80])
                continue
        except Exception as e:
            log.debug("nav session probe %s: %s", url, e)
    # CDP SSO: cookie alone is enough for Sub2API sso-to-oauth
    try:
        from grokreg.delivery.sso_capture import capture_sso_cookie, sso_preview

        sso = await capture_sso_cookie(tab, navigate_if_needed=True)
        if sso and len(sso) > 20:
            log.info(
                "SSO cookie present %s — session OK for Sub2API (UI may still show sign-in)",
                sso_preview(sso),
            )
            try:
                await tab.go_to("https://accounts.x.ai/account")
                await asyncio.sleep(2.0)
            except Exception:
                pass
            # Prefer UI logged-in when possible
            if await page_is_logged_in(tab) or await page_looks_success(tab):
                return True
            # Cookie is still valid session proof for API import
            return True
    except Exception as e:
        log.debug("sso probe: %s", e)
    return False


async def _grab_sso_cookie(tab: Any) -> str:
    """Best-effort SSO cookie for Sub2API; empty if missing."""
    try:
        from grokreg.delivery.sso_capture import capture_sso_cookie

        return (await capture_sso_cookie(tab, navigate_if_needed=True) or "").strip()
    except Exception:
        return ""


async def login_with_credentials(
    tab: Any,
    email: str,
    password: str,
    config: dict[str, Any] | None = None,
    *,
    attempts: int = 2,
) -> bool:
    """
    Explicit sign-in with the account just registered.
    Used when post-signup cookies are missing / guest grok wall.
    Leaves browser on logged-in accounts.x.ai or grok.com when possible.
    """
    cfg = config or {}
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        log.warning("login_with_credentials: missing email/password")
        return False

    # Fresh account sometimes not login-ready for a few seconds after CreateUser
    await asyncio.sleep(random.uniform(2.5, 4.5))

    # Prefer existing session over re-login
    if await _try_session_via_navigation(tab):
        return True

    for attempt in range(max(1, int(attempts))):
        log.info(
            "=== LOGIN with credentials for %s (attempt %s/%s, pw_len=%s) ===",
            email,
            attempt + 1,
            attempts,
            len(password),
        )
        ok = await _login_with_credentials_once(tab, email, password, cfg)
        if ok:
            return True
        log.warning("login attempt %s failed — cool down then retry", attempt + 1)
        await asyncio.sleep(random.uniform(3.0, 5.5))
        # hard reload clean sign-in
        try:
            await tab.go_to("https://accounts.x.ai/sign-in")
            await asyncio.sleep(3.0)
        except Exception:
            pass
    return False


async def _login_with_credentials_once(
    tab: Any,
    email: str,
    password: str,
    cfg: dict[str, Any],
) -> bool:
    try:
        href0 = str(await _exec_js(tab, "location.href") or "")
        if "sign-in" not in href0.lower():
            await tab.go_to("https://accounts.x.ai/sign-in")
            await asyncio.sleep(3.5)
        else:
            # already on sign-in — soft reload once if no inputs yet
            await asyncio.sleep(1.5)
        await dismiss_cookie_banner(tab)
    except Exception as e:
        log.warning("open sign-in: %s", e)
        return False

    # Soft CF if present on sign-in
    try:
        st = await _cf_page_state(tab)
        info0 = await _turnstile_widget_info(tab)
        if st == "challenge" or info0.get("widgets") or info0.get("challengeText"):
            await click_turnstile_checkbox_robust(
                tab, wait_sec=25.0, reason="sign_in_cf"
            )
            await wait_turnstile_token(tab, timeout=12.0)
    except Exception as e:
        log.debug("sign-in CF soft: %s", e)

    # xAI sign-in landing shows social + "Login with email" — NO email field until click
    async def _email_field_visible() -> str | None:
        return await _exec_js(
            tab,
            """
            (() => {
              const sels = [
                'input[type="email"]','input[name="email"]',
                'input[autocomplete="email"]','input[autocomplete="username"]',
                'input[inputmode="email"]'
              ];
              for (const s of sels) {
                const el = document.querySelector(s);
                if (!el) continue;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
                    && st.display !== 'none') return s;
              }
              return null;
            })()
            """,
        )

    if not await _email_field_visible():
        log.info("login: click 'Login with email' to reveal form")
        clicked_email_method = await human_click_button_by_text(
            tab,
            [
                "login with email",
                "sign in with email",
                "continue with email",
                "log in with email",
            ],
            timeout=5,
            exclude_social=True,
            config=cfg,
        )
        if not clicked_email_method:
            # JS fallback — exact button text match
            r = await _exec_js(
                tab,
                """
                (() => {
                  const want = /login with email|sign in with email|continue with email|log in with email/i;
                  for (const b of document.querySelectorAll('button, [role=button], a')) {
                    const t = (b.innerText || b.textContent || '').replace(/\\s+/g,' ').trim();
                    if (want.test(t)) { b.click(); return t; }
                  }
                  return null;
                })()
                """,
            )
            log.info("login: JS click Login with email → %s", r)
            clicked_email_method = bool(r)
        await asyncio.sleep(2.0)

    # Wait for email field to hydrate after method click
    email_ready = False
    for wait_i in range(12):
        ready = await _email_field_visible()
        if ready:
            email_ready = True
            log.info("login email field ready (%s) wait=%s", ready, wait_i)
            break
        if wait_i in (2, 6):
            # re-click Login with email
            await _exec_js(
                tab,
                """
                (() => {
                  const want = /login with email|sign in with email/i;
                  for (const b of document.querySelectorAll('button, [role=button], a')) {
                    const t = (b.innerText || '').replace(/\\s+/g,' ').trim();
                    if (want.test(t)) { b.click(); return t; }
                  }
                  return null;
                })()
                """,
            )
        if wait_i == 8:
            try:
                log.warning("login: no email field yet — reload sign-in")
                await tab.go_to("https://accounts.x.ai/sign-in")
                await asyncio.sleep(3.0)
                await dismiss_cookie_banner(tab)
                await _exec_js(
                    tab,
                    """
                    (() => {
                      const want = /login with email|sign in with email/i;
                      for (const b of document.querySelectorAll('button, [role=button], a')) {
                        const t = (b.innerText || '').replace(/\\s+/g,' ').trim();
                        if (want.test(t)) { b.click(); return t; }
                      }
                      return null;
                    })()
                    """,
                )
            except Exception:
                pass
        await asyncio.sleep(1.2)

    if not email_ready:
        diag = await _exec_js(
            tab,
            """
            (() => ({
              href: location.href, title: document.title,
              body: (document.body && document.body.innerText || '').slice(0,180),
              inputs: document.querySelectorAll('input').length,
              buttons: [...document.querySelectorAll('button')].map(b =>
                (b.innerText||'').slice(0,40)).slice(0,8)
            }))()
            """,
        )
        log.error("login: email field never appeared diag=%s", diag)
        return False

    email_strats = [
        {"css_selector": 'input[type="email"]'},
        {"css_selector": 'input[name="email"]'},
        {"css_selector": 'input[autocomplete="email"]'},
        {"css_selector": 'input[inputmode="email"]'},
        {"css_selector": 'input[autocomplete="username"]'},
        {"css_selector": 'input[type="text"]'},
    ]
    ok_email = await type_into(tab, email_strats, email, "login_email")
    if not ok_email:
        # last resort: React-set first visible input
        got = await _exec_js(
            tab,
            f"""
            (() => {{
              const val = {json.dumps(email)};
              const els = [...document.querySelectorAll('input')].filter(el => {{
                if (el.type === 'hidden' || el.disabled) return false;
                const r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 8;
              }});
              const el = els[0];
              if (!el) return {{ok:false}};
              el.focus();
              const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
              const prev = el.value;
              if (desc && desc.set) desc.set.call(el, val); else el.value = val;
              if (el._valueTracker) try {{ el._valueTracker.setValue(prev); }} catch(e) {{}}
              el.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:val}}));
              el.dispatchEvent(new Event('change', {{bubbles:true}}));
              return {{ok: (el.value||'').includes(val), n: els.length}};
            }})()
            """,
        )
        ok_email = isinstance(got, dict) and got.get("ok")
        log.info("login email last-resort: %s", got)
    if not ok_email:
        log.error("login: cannot fill email")
        return False
    await asyncio.sleep(random.uniform(0.6, 1.2))

    # Continue after email (or sign-in if single-step)
    cont = await human_click_button_by_text(
        tab,
        ["continue", "next", "sign in", "log in", "tiếp tục", "đăng nhập"],
        timeout=5,
        exclude_social=True,
        config=cfg,
    )
    if not cont:
        await click_continue_after_email(tab)
    await asyncio.sleep(2.8)

    # Password step (may already be same page)
    pw_strats = [
        {"css_selector": 'input[type="password"]'},
        {"css_selector": 'input[name="password"]'},
        {"css_selector": 'input[autocomplete="current-password"]'},
        {"css_selector": 'input[autocomplete="password"]'},
    ]
    # Wait briefly for password field
    for _ in range(6):
        has_pw = await _exec_js(
            tab,
            """
            (() => {
              const el = document.querySelector(
                'input[type="password"], input[name="password"], input[autocomplete="current-password"]'
              );
              if (!el) return false;
              const r = el.getBoundingClientRect();
              return r.width > 2 && r.height > 2;
            })()
            """,
        )
        if has_pw:
            break
        # maybe already logged in / redirected
        if await page_is_logged_in(tab):
            log.info("login: already logged-in after email continue")
            return True
        await asyncio.sleep(1.0)

    ok_pw = await type_into(tab, pw_strats, password, "login_password")
    if not ok_pw:
        # one more try via ensure helper-style JS
        got = await _exec_js(
            tab,
            f"""
            (() => {{
              const pass = {json.dumps(password)};
              const pw = document.querySelector(
                'input[type="password"], input[name="password"], input[autocomplete="current-password"]'
              );
              if (!pw) return {{ok:false}};
              const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
              const prev = pw.value;
              if (desc && desc.set) desc.set.call(pw, pass); else pw.value = pass;
              if (pw._valueTracker) try {{ pw._valueTracker.setValue(prev); }} catch(e) {{}}
              pw.dispatchEvent(new InputEvent('input', {{bubbles:true, composed:true, inputType:'insertText', data:pass}}));
              pw.dispatchEvent(new Event('change', {{bubbles:true}}));
              return {{ok: pw.value === pass}};
            }})()
            """,
        )
        ok_pw = isinstance(got, dict) and got.get("ok")
    if not ok_pw:
        log.error("login: cannot fill password")
        return False

    await asyncio.sleep(random.uniform(0.5, 1.0))
    # CF before submit if shown
    try:
        await click_turnstile_checkbox_robust(
            tab, wait_sec=20.0, reason="sign_in_before_submit"
        )
        await wait_turnstile_token(tab, timeout=10.0)
    except Exception:
        pass

    clicked = await human_click_button_by_text(
        tab,
        [
            "sign in",
            "log in",
            "continue",
            "next",
            "submit",
            "đăng nhập",
            "tiếp tục",
        ],
        timeout=5,
        exclude_social=True,
        config=cfg,
    )
    if not clicked:
        await _exec_js(
            tab,
            """
            (() => {
              const want = /^(sign in|log in|continue|next|submit|đăng nhập)$/i;
              for (const b of document.querySelectorAll('button, [role=button], input[type=submit]')) {
                const t = (b.innerText || b.value || '').trim();
                if (want.test(t)) { b.click(); return t; }
              }
              const f = document.querySelector('form');
              if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); return 'form'; }
              return null;
            })()
            """,
        )
    log.info("login: submitted credentials, waiting for session…")

    for i in range(18):
        await asyncio.sleep(2.0)
        try:
            await dismiss_oauth_consent_if_present(tab)
        except Exception:
            pass
        # CF may appear after submit
        if i in (1, 3, 6):
            try:
                st = await _cf_page_state(tab)
                if st == "challenge":
                    await click_turnstile_checkbox_robust(
                        tab, wait_sec=18.0, reason="sign_in_post_submit_cf"
                    )
                    await wait_turnstile_token(tab, timeout=12.0)
            except Exception:
                pass
        href = str(await _exec_js(tab, "location.href") or "")
        if await page_is_logged_in(tab) or await page_looks_success(tab):
            log.info("login OK (poll %s) href=%s", i + 1, href[:120])
            return True
        if "/oauth2/consent" in href.lower():
            try:
                await dismiss_oauth_consent_if_present(tab)
            except Exception:
                pass
            if await page_is_logged_in(tab):
                return True
        # bounced still on sign-in with error?
        err = await detect_page_error(tab)
        if err:
            log.warning("login page err: %s", err)
            if "rate_limit" in str(err):
                return False
            if any(
                x in str(err).lower()
                for x in ("incorrect", "invalid password", "wrong password", "not found")
            ):
                return False
        if i in (5, 10, 14):
            # nudge: open account page / re-check SSO
            try:
                await tab.go_to("https://accounts.x.ai/account")
                await asyncio.sleep(2.0)
                if await page_is_logged_in(tab):
                    return True
            except Exception:
                pass
    # last chance: SSO cookie
    if await _try_session_via_navigation(tab):
        return True
    log.error(
        "login_with_credentials FAILED final=%s",
        str(await _exec_js(tab, "location.href") or "")[:140],
    )
    return False


async def ensure_logged_in_landing(
    tab: Any,
    config: dict[str, Any] | None = None,
    *,
    email: str = "",
    password: str = "",
) -> bool:
    """
    After Complete sign up: land on a page where the NEW account is logged in.
    Order: dismiss oauth consent → accounts.x.ai/account → grok.com (chat)
    → if still guest: SIGN IN with email/password → grok.com again.
    Leave browser there for user.
    """
    cfg = config or {}
    grok_url = str(cfg.get("grok_url") or "https://grok.com/").strip() or "https://grok.com/"

    async def _land_grok_logged_in() -> bool:
        try:
            log.info("Ensure login: open %s", grok_url)
            await tab.go_to(grok_url)
            await asyncio.sleep(4.0)
            await dismiss_cookie_banner(tab)
        except Exception as e:
            log.warning("open grok: %s", e)
            return False
        for attempt in range(4):
            if await page_is_logged_in(tab):
                href = str(await _exec_js(tab, "location.href") or "")
                log.info("LOGGED-IN landing ready: %s", href[:140])
                await asyncio.sleep(1.5)
                return True
            href = str(await _exec_js(tab, "location.href") or "")
            log.warning(
                "Grok not logged-in yet (try %s) href=%s — retry account→grok",
                attempt + 1,
                href[:100],
            )
            try:
                await tab.go_to("https://accounts.x.ai/account")
                await asyncio.sleep(2.5)
                if await page_is_logged_in(tab):
                    await tab.go_to(grok_url)
                    await asyncio.sleep(3.5)
                    if await page_is_logged_in(tab):
                        log.info("LOGGED-IN after SSO hop")
                        return True
            except Exception as e:
                log.debug("sso hop: %s", e)
            await asyncio.sleep(2.0)
        return False

    # 0) Consent page = already authenticated (common after Sub2API leftovers in profile)
    try:
        await dismiss_oauth_consent_if_present(tab)
    except Exception as e:
        log.debug("consent dismiss: %s", e)

    # 1) Prefer account dashboard (proves SSO cookie for this profile)
    session_ok = False
    for attempt in range(3):
        try:
            log.info("Ensure login: open accounts.x.ai/account (try %s)", attempt + 1)
            await tab.go_to("https://accounts.x.ai/account")
            await asyncio.sleep(3.5)
            await dismiss_cookie_banner(tab)
            href = str(await _exec_js(tab, "location.href") or "")
            log.info("accounts page href=%s", href[:120])
            if "/oauth2/consent" in href.lower():
                await dismiss_oauth_consent_if_present(tab)
                await tab.go_to("https://accounts.x.ai/account")
                await asyncio.sleep(2.5)
                href = str(await _exec_js(tab, "location.href") or "")
            if await page_is_logged_in(tab):
                session_ok = True
                break
            # bounced to sign-in — session not ready
            if "sign-in" in href.lower() or "sign-up" in href.lower():
                log.warning("Session not on /account yet (auth wall). Wait cookies…")
                await asyncio.sleep(2.5 + attempt)
                continue
        except Exception as e:
            log.warning("ensure account page: %s", e)
            await asyncio.sleep(2)

    # 2) Open Grok chat if session already good
    if session_ok:
        if await _land_grok_logged_in():
            return True

    # 3) Still guest after signup hops → sign-in with the password we just set.
    # Default ON (needed when Complete creates user but no session cookie).
    # Disable only with config allow_login_fallback=false.
    allow_fb = cfg.get("allow_login_fallback")
    if allow_fb is None:
        allow_fb = True
    if email and password and allow_fb:
        log.warning(
            "Session missing after signup — login fallback for %s",
            email,
        )
        if await login_with_credentials(tab, email, password, cfg, attempts=2):
            if await _land_grok_logged_in():
                return True
            try:
                await tab.go_to("https://accounts.x.ai/account")
                await asyncio.sleep(3.0)
                if await page_is_logged_in(tab):
                    log.info("LOGGED-IN on accounts.x.ai/account after login fallback")
                    return True
            except Exception:
                pass
            return await page_is_logged_in(tab)
    elif email and password and not allow_fb:
        log.error(
            "Session missing after SIGN-UP — allow_login_fallback=false. account=%s",
            email,
        )
    else:
        if await _land_grok_logged_in():
            return True

    href = str(await _exec_js(tab, "location.href") or "")
    log.error("FAILED to confirm logged-in landing. Final href=%s", href[:140])
    return False


async def open_grok_chat(tab: Any, config: dict[str, Any] | None = None) -> None:
    """
    After signup, leave browser on LOGGED-IN grok.com (or accounts.x.ai/account).
    """
    cfg = config or {}
    if cfg.get("open_grok_after_success") is False:
        # still try account page so keep_open is useful
        try:
            await tab.go_to("https://accounts.x.ai/account")
            await asyncio.sleep(3)
        except Exception:
            pass
        return
    ok = await ensure_logged_in_landing(tab, cfg)
    if not ok:
        log.warning("open_grok_chat: could not verify login — browser left for manual check")
    final = await _exec_js(tab, "location.href")
    log.info("Grok/session page now: %s logged_in=%s", final, ok)


async def dismiss_cookie_banner(tab: Any) -> None:
    """Close OneTrust / cookie banners if present (EN + VI). Multiple passes."""
    pure = """
    (() => {
      const clicked = [];
      const ids = [
        'onetrust-accept-btn-handler',
        'onetrust-reject-all-handler',
        'accept-recommended-btn-handler',
      ];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el) { try { el.click(); clicked.push(id); } catch(e) {} }
      }
      // Prefer accept-all style (EN + VI variants seen on xAI)
      const prefer = [
        'accept all', 'accept all cookies', 'allow all', 'allow all cookies',
        'chấp nhận mọi cookie', 'chap nhan moi cookie',
        'chấp nhận tất cả', 'chap nhan tat ca',
        'cho phép tất cả', 'cho phep tat ca', 'cho phép mọi cookie',
        'reject all', 'từ chối tất cả', 'tu choi tat ca',
        'đồng ý', 'dong y', 'agree', 'i agree',
      ];
      const nodes = [...document.querySelectorAll(
        'button, [role="button"], a[role="button"], input[type="button"], input[type="submit"]'
      )];
      const labelOf = (n) => ((n.innerText || n.value || n.getAttribute('aria-label') || '') + '')
        .replace(/\\s+/g, ' ').trim().toLowerCase();
      // pass1: exact / includes prefer
      for (const want of prefer) {
        for (const n of nodes) {
          const t = labelOf(n);
          if (!t || t.length > 48) continue;
          if (t === want || t.includes(want)) {
            try { n.click(); clicked.push(t); } catch(e) {}
          }
        }
        if (clicked.length) break;
      }
      // pass2: any cookie dialog primary
      if (!clicked.length) {
        for (const n of nodes) {
          const t = labelOf(n);
          if (!t || t.length > 48) continue;
          if (/cookie|privacy|consent/.test(t) && /(accept|allow|chấp|cho phép|agree)/.test(t)) {
            try { n.click(); clicked.push(t); break; } catch(e) {}
          }
        }
      }
      // hide leftover overlays that block clicks
      try {
        for (const sel of [
          '#onetrust-banner-sdk', '#onetrust-consent-sdk',
          '.onetrust-pc-dark-filter', '[class*="cookie-banner"]',
          '[id*="cookie"]', '[class*="Consent"]'
        ]) {
          document.querySelectorAll(sel).forEach(el => {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('pointer-events', 'none', 'important');
          });
        }
      } catch(e) {}
      return clicked.length ? clicked.join('|') : null;
    })()
    """
    for pass_i in range(3):
        r = await _exec_js(tab, pure)
        if r:
            log.info("Cookie banner pass%s: %s", pass_i + 1, r)
            await asyncio.sleep(0.6)
        else:
            break


async def click_sign_up_with_email(tab: Any) -> bool:
    """
    Landing page shows: Sign up with X | Sign up with email | Apple | Google.
    Must click exactly 'Sign up with email' before the email input appears.
    """
    # If email field already visible, skip
    already = await _exec_js(
        tab,
        """
        (() => !!document.querySelector(
          'input[type="email"], input[name="email"], input[autocomplete="email"], input[inputmode="email"]'
        ))()
        """,
    )
    if already:
        log.info("Email field already visible")
        return True

    pure = """
    (() => {
      const want = ['sign up with email', 'sign up with e-mail', 'continue with email', 'use email'];
      const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
      // exact match first
      for (const n of nodes) {
        const t = (n.innerText || n.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (!t) continue;
        // hard skip social
        if (/with\\s+(x|twitter|google|apple|facebook|microsoft|github)\\b/.test(t) && !t.includes('email')) {
          continue;
        }
        if (want.includes(t)) {
          n.click();
          return t;
        }
      }
      // contains 'email' and sign/continue, not social providers
      for (const n of nodes) {
        const t = (n.innerText || n.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        if (!t) continue;
        if (t.includes('email') && (t.includes('sign') || t.includes('continue') || t.includes('use'))) {
          if (/with\\s+(x|google|apple|facebook)\\b/.test(t) && !t.includes('email')) continue;
          n.click();
          return t;
        }
      }
      return null;
    })()
    """
    for attempt in range(5):
        result = await _exec_js(tab, pure)
        if result:
            log.info("Clicked signup method: %s", result)
            # wait for email input
            for _ in range(20):
                ok = await _exec_js(
                    tab,
                    """
                    (() => !!document.querySelector(
                      'input[type="email"], input[name="email"], input[autocomplete="email"], input[inputmode="email"]'
                    ))()
                    """,
                )
                if ok:
                    log.info("Email input appeared")
                    return True
                await asyncio.sleep(0.4)
            log.warning("Clicked '%s' but email input not yet visible (attempt %s)", result, attempt + 1)
        else:
            # pydoll text find fallback
            for label in ("Sign up with email", "Sign up with Email", "Continue with email"):
                try:
                    el = await tab.find(tag_name="button", text=label, timeout=2)
                    if el:
                        await el.click()
                        log.info("Clicked button text: %s", label)
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass
        await asyncio.sleep(0.8)

    # final check
    ok = await _exec_js(
        tab,
        """
        (() => !!document.querySelector(
          'input[type="email"], input[name="email"], input[autocomplete="email"]'
        ))()
        """,
    )
    return bool(ok)


async def wait_for_selector_js(tab: Any, css_list: list[str], timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    lit = json.dumps(css_list)
    while time.time() < deadline:
        found = await _exec_js(
            tab,
            f"""
            (() => {{
              const sels = {lit};
              for (const s of sels) {{
                try {{ if (document.querySelector(s)) return s; }} catch (e) {{}}
              }}
              return null;
            }})()
            """,
        )
        if found:
            return True
        await asyncio.sleep(0.35)
    return False


async def capture_page_error_exact(tab: Any) -> dict[str, str]:
    """
    Pull EXACT on-page error text (not a vague code).
    Returns {code, exact, href} — exact is raw UI string for debugging/fix.
    """
    pure = r"""
    (() => {
      const href = location.href || '';
      const rawBody = (document.body && document.body.innerText) || '';
      const t = rawBody.replace(/\s+/g, ' ').trim();
      const low = t.toLowerCase();

      // 1) DOM alert nodes (most precise)
      const sels = [
        '[role="alert"]', '[aria-live="assertive"]', '[aria-live="polite"]',
        '.error', '.text-destructive', '[class*="error"]', '[class*="Error"]',
        '[class*="toast"]', '[data-testid*="error"]', '[data-sonner-toast]',
        'form [class*="message"]', 'form p', '[class*="Alert"]'
      ];
      const alertParts = [];
      const isNoise = (sl) => {
        // Cookie / privacy consent banners are NOT signup errors
        if (sl.includes('your privacy')) return true;
        if (sl.includes('privacy') && (sl.includes('cookie') || sl.includes('consent') || sl.includes('dialog'))) return true;
        if (sl.includes('cookie') || sl.includes('cookies')) return true;
        if (sl.includes('accept all') || sl.includes('reject all') || sl.includes('onetrust')) return true;
        if (sl.includes('we use cookies') || sl.includes('manage preferences')) return true;
        if (sl.includes('dialog closed') || sl.includes('[`dialog')) return true;
        if (sl.includes('sign up with') || sl.includes('continue with google')) return true;
        return false;
      };
      for (const sel of sels) {
        try {
          for (const a of document.querySelectorAll(sel)) {
            const s = (a.innerText || a.textContent || '').trim().replace(/\s+/g, ' ');
            if (s && s.length >= 3 && s.length < 300 && !alertParts.includes(s)) {
              const sl = s.toLowerCase();
              if (isNoise(sl)) continue;
              alertParts.push(s);
            }
          }
        } catch (e) {}
      }

      // 2) Regex snips from full body for known xAI phrases
      const patterns = [
        /too many validation codes[^.!?]{0,100}[.!?]?/i,
        /retry in\s+\d+\s+minutes?[^.!?]{0,40}/i,
        /something went wrong[^.!?]{0,120}[.!?]?/i,
        /please try again[^.!?]{0,80}[.!?]?/i,
        /verification failed[^.!?]{0,120}[.!?]?/i,
        /email (?:address )?(?:is )?(?:invalid|not valid|not allowed)[^.!?]{0,80}[.!?]?/i,
        /(?:disposable|temporary) email[^.!?]{0,80}[.!?]?/i,
        /already (?:registered|exists|in use)[^.!?]{0,80}[.!?]?/i,
        /validation code is invalid[^.!?]{0,60}[.!?]?/i,
        /expected string, received undefined[^.!?]{0,40}/i,
        /wrong email address or password[^.!?]{0,40}/i,
      ];
      const bodyHits = [];
      for (const re of patterns) {
        const m = t.match(re);
        if (m && m[0]) bodyHits.push(m[0].trim());
      }

      // Prefer shortest meaningful alert DOM text, else body hit
      let exact = '';
      if (alertParts.length) {
        alertParts.sort((a, b) => a.length - b.length);
        exact = alertParts[0];
      } else if (bodyHits.length) {
        exact = bodyHits[0];
      }

      // Classify code for branching (keep exact separate)
      let code = '';
      if (low.includes('too many validation codes') || (low.includes('retry in') && low.includes('minute')))
        code = 'rate_limit';
      else if (low.includes('verification failed') || low.includes('not solely due to bot') || low.includes('submit feedback to cloudflare'))
        code = 'verification_failed';
      else if (low.includes('captcha') && (low.includes('failed') || low.includes('expired')))
        code = 'verification_failed';
      else if (low.includes('email validation code is invalid') || low.includes('validation code is invalid'))
        code = 'invalid_code';
      else if (low.includes('expected string, received undefined') || low.includes('invalid input'))
        code = 'invalid_input_undefined';
      else if (low.includes('already registered') || low.includes('already exists') || low.includes('already in use'))
        code = 'email_already_used';
      else if (low.includes('password too weak') || low.includes('stronger password'))
        code = 'password_too_weak';
      else if (low.includes('wrong email') || low.includes('incorrect email or password'))
        code = 'wrong_credentials';
      else if (low.includes('disposable') || low.includes('temporary email') || low.includes('email is not valid')
          || low.includes('invalid email') || low.includes('cannot use this email'))
        code = 'email_rejected';
      else if (low.includes('something went wrong') || low.includes('try again later'))
        code = 'error_generic';
      else if (exact)
        code = 'alert';

      // Drop noise exacts (privacy banner false positives)
      if (exact && isNoise(exact.toLowerCase())) {
        exact = '';
      }
      if (!code && !exact) return null;
      // If only noise was found, not a real error
      if (!code && !exact) return null;
      return JSON.stringify({
        code: code || 'alert',
        exact: (exact || code || '').slice(0, 240),
        href: href.slice(0, 160),
        alerts: alertParts.slice(0, 5),
        bodyHits: bodyHits.slice(0, 5)
      });
    })()
    """
    try:
        r = await _exec_js(tab, pure)
        if not r:
            return {}
        if isinstance(r, dict):
            return {str(k): str(v) if not isinstance(v, list) else v for k, v in r.items()}  # type: ignore
        s = str(r)
        if s.startswith("{"):
            try:
                data = json.loads(s)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"code": s, "exact": s}
    except Exception as e:
        log.debug("capture_page_error_exact: %s", e)
        return {}


def _is_privacy_noise_error(msg: str) -> bool:
    """True if detector hit cookie/privacy UI, not a real xAI signup error."""
    sl = (msg or "").lower()
    return (
        "your privacy" in sl
        or "dialog closed" in sl
        or ("cookie" in sl and "consent" in sl)
        or "onetrust" in sl
        or "accept all" in sl
        # Vietnamese OneTrust / privacy modal (was false-positive alert)
        or "quyền riêng tư" in sl
        or "rieng tu" in sl
        or "hộp thoại" in sl
        or "hop thoai" in sl
        or "cài đặt cookie" in sl
        or "cai dat cookie" in sl
        or "từ chối tất cả" in sl
        or "tu choi tat ca" in sl
        # Resend timer / OTP UI banner (false-positive alert)
        or "didn't receive a code" in sl
        or "did not receive a code" in sl
        or "resend (" in sl
        or "chưa nhận được mã" in sl
        or "gửi lại (" in sl
    )


async def detect_page_error(tab: Any) -> Optional[str]:
    """
    Detect xAI signup errors. Returns "code:exact UI text" when possible
    so logs are never vague (e.g. error_generic:Something went wrong...).
    Ignores privacy/cookie banners (were false-positive 'errors').
    """
    detail = await capture_page_error_exact(tab)
    if not detail:
        return None
    code = str(detail.get("code") or "").strip()
    exact = str(detail.get("exact") or "").strip()
    if not code and not exact:
        return None
    if _is_privacy_noise_error(exact) or _is_privacy_noise_error(code):
        log.debug("Ignoring privacy/cookie UI as non-error: %r", exact or code)
        return None
    # Always attach exact text after code
    if exact and code:
        if exact.lower().startswith(code.lower()):
            out = exact
        else:
            out = f"{code}:{exact}"
    else:
        out = exact or code
    if _is_privacy_noise_error(out):
        return None
    alerts = detail.get("alerts")
    body_hits = detail.get("bodyHits")
    if exact or alerts or body_hits:
        log.info(
            "xAI page error EXACT=%r code=%s href=%s alerts=%s bodyHits=%s",
            exact[:200],
            code,
            str(detail.get("href") or "")[:100],
            alerts,
            body_hits,
        )
    return out[:220]


# ---------------------------------------------------------------------------
# Registration flow
# ---------------------------------------------------------------------------

