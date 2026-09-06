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
from grokreg.browser.jsutil import _exec_js, _unwrap_js_result  # noqa: F401
from grokreg.core.config import load_config
from grokreg.core.helpers import extract_otp, normalize_otp_for_input

async def enable_network_capture(tab: Any) -> None:
    """Enable CDP Network domain + store request/response (like DevTools Network)."""
    global _NET_CAPTURE_ON, _NET_RESPONSES, _NET_REQUESTS
    _NET_RESPONSES = {}
    _NET_REQUESTS = {}
    try:
        if hasattr(tab, "enable_network_events"):
            await tab.enable_network_events()
        else:
            from pydoll.commands.network_commands import NetworkCommands

            await tab._execute_command(NetworkCommands.enable())
        _NET_CAPTURE_ON = True
        log.info("F12 Network capture ON (CDP Network.enable)")
    except Exception as e:
        log.warning("Network.enable failed: %s", e)
        return

    async def _on_request(event: dict) -> None:
        try:
            p = event.get("params") or {}
            rid = str(p.get("requestId") or "")
            req = p.get("request") or {}
            if not rid:
                return
            _NET_REQUESTS[rid] = {
                "url": str(req.get("url") or ""),
                "method": req.get("method"),
                "headers": req.get("headers") or {},
                "postData": (req.get("postData") or "")[:4000],
                "hasPostData": req.get("hasPostData"),
                "type": p.get("type"),
                "timestamp": p.get("timestamp"),
            }
        except Exception:
            pass

    async def _on_response(event: dict) -> None:
        try:
            p = event.get("params") or {}
            rid = str(p.get("requestId") or "")
            resp = p.get("response") or {}
            if not rid:
                return
            _NET_RESPONSES[rid] = {
                "url": resp.get("url"),
                "status": resp.get("status"),
                "statusText": resp.get("statusText"),
                "mimeType": resp.get("mimeType"),
                "headers": resp.get("headers") or {},
                "remoteIPAddress": resp.get("remoteIPAddress"),
                "fromDiskCache": resp.get("fromDiskCache"),
            }
        except Exception:
            pass

    async def _on_loading_failed(event: dict) -> None:
        try:
            p = event.get("params") or {}
            rid = str(p.get("requestId") or "")
            if not rid:
                return
            prev = _NET_RESPONSES.get(rid) or {}
            prev.update(
                {
                    "failed": True,
                    "errorText": p.get("errorText"),
                    "canceled": p.get("canceled"),
                    "type": p.get("type"),
                    "blockedReason": p.get("blockedReason"),
                }
            )
            _NET_RESPONSES[rid] = prev
        except Exception:
            pass

    try:
        await tab.on("Network.requestWillBeSent", _on_request)
        await tab.on("Network.responseReceived", _on_response)
        await tab.on("Network.loadingFailed", _on_loading_failed)
    except Exception as e:
        log.warning("Network event subscribe failed: %s", e)


def _net_url_interesting(url: str) -> bool:
    u = (url or "").lower()
    keys = (
        "x.ai",
        "accounts.",
        "auth0",
        "oauth",
        "signup",
        "sign-up",
        "sign_up",
        "register",
        "verify",
        "validation",
        "graphql",
        "identity",
        "session",
        "credential",
        "challenge",
        "turnstile",
        "cloudflare",
        "/api/",
        "passwordless",
        "otp",
        "email",
    )
    return any(k in u for k in keys)


async def dump_xai_network_capture(
    tab: Any, *, label: str = "email_submit", email: str = ""
) -> list[dict[str, Any]]:
    """Dump F12-style network rows for xAI/auth traffic after failure."""
    entries: list[dict[str, Any]] = []
    req_map = dict(_NET_REQUESTS)
    try:
        if hasattr(tab, "get_network_logs"):
            for ev in await tab.get_network_logs() or []:
                p = (ev or {}).get("params") or {}
                rid = str(p.get("requestId") or "")
                req = p.get("request") or {}
                url = str(req.get("url") or "")
                if rid and rid not in req_map:
                    req_map[rid] = {
                        "url": url,
                        "method": req.get("method"),
                        "headers": req.get("headers") or {},
                        "postData": (req.get("postData") or "")[:4000],
                        "type": p.get("type"),
                    }
    except Exception as e:
        log.debug("get_network_logs: %s", e)

    candidates: list[tuple[str, dict]] = []
    for rid, req in req_map.items():
        url = str(req.get("url") or "")
        if _net_url_interesting(url):
            candidates.append((rid, req))
    if not candidates:
        for rid, req in list(req_map.items())[-50:]:
            candidates.append((rid, req))

    for rid, req in candidates[-80:]:
        url = str(req.get("url") or "")
        resp = _NET_RESPONSES.get(rid) or {}
        row: dict[str, Any] = {
            "requestId": rid,
            "method": req.get("method"),
            "url": url[:500],
            "type": req.get("type"),
            "status": resp.get("status"),
            "statusText": resp.get("statusText"),
            "mimeType": resp.get("mimeType"),
            "failed": resp.get("failed"),
            "errorText": resp.get("errorText"),
            "remoteIP": resp.get("remoteIPAddress"),
            "reqContentType": (req.get("headers") or {}).get("Content-Type")
            or (req.get("headers") or {}).get("content-type"),
            "postData": (req.get("postData") or "")[:2000],
        }
        body = ""
        try:
            if hasattr(tab, "get_network_response_body") and rid:
                await asyncio.sleep(0.05)
                body = await tab.get_network_response_body(rid)
        except Exception as e:
            row["bodyError"] = str(e)[:200]
        if body:
            b = str(body)
            if len(b) > 4000:
                b = b[:4000] + "...(truncated)"
            row["responseBody"] = b
        entries.append(row)

    def _score(r: dict) -> tuple:
        st = r.get("status") or 0
        try:
            st = int(st)
        except Exception:
            st = 0
        fail = 1 if r.get("failed") or st >= 400 else 0
        interesting = 1 if _net_url_interesting(str(r.get("url") or "")) else 0
        has_body = 1 if r.get("responseBody") else 0
        return (-fail, -interesting, -has_body, -st)

    entries.sort(key=_score)

    payload = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
        "email": email,
        "total_requests_tracked": len(req_map),
        "total_responses_tracked": len(_NET_RESPONSES),
        "entries": entries,
    }
    try:
        p1 = ROOT / "data" / "network_capture_latest.json"
        p2 = ROOT / "data" / f"network_capture_{int(time.time())}.json"
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        p1.write_text(text, encoding="utf-8")
        p2.write_text(text, encoding="utf-8")
        log.info(
            "F12 Network dump → %s (%s rows, %s req tracked)",
            p1.name,
            len(entries),
            len(req_map),
        )
        shown = 0
        for r in entries:
            st = r.get("status")
            url = str(r.get("url") or "")
            if not (
                r.get("failed")
                or (isinstance(st, int) and st >= 400)
                or _net_url_interesting(url)
            ):
                continue
            body_preview = str(r.get("responseBody") or r.get("bodyError") or "")[:220]
            body_preview = body_preview.replace("\n", " ")
            log.info(
                "NET %s %s %s | body=%s",
                r.get("method"),
                st if st is not None else (r.get("errorText") or "?"),
                url[:140],
                body_preview,
            )
            shown += 1
            if shown >= 15:
                break
    except Exception as e:
        log.warning("network dump write failed: %s", e)
    return entries


async def wait_turnstile_token(
    tab: Any, timeout: float = 20.0, *, config: dict[str, Any] | None = None
) -> bool:
    """
    Wait until Cloudflare Turnstile has a non-empty response token (if widget present).
    If no turnstile on page → True (nothing to wait).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = await _exec_js(
            tab,
            """
            (() => {
              const inputs = [...document.querySelectorAll(
                'input[name="cf-turnstile-response"], input[name="cf_challenge_response"], textarea[name="cf-turnstile-response"]'
              )];
              if (!inputs.length) {
                // no explicit field — check iframe only
                const iframe = document.querySelector('iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"], .cf-turnstile');
                return iframe ? 'waiting' : 'none';
              }
              for (const el of inputs) {
                if ((el.value || '').trim().length > 20) return 'ok';
              }
              return 'waiting';
            })()
            """,
        )
        if state == "none" or state == "ok":
            if state == "ok":
                log.info("Turnstile token ready")
            return True
        # external solver mid-wait (once)
        if not getattr(wait_turnstile_token, "_ext_tried", False):
            wait_turnstile_token._ext_tried = True  # type: ignore[attr-defined]
            try:
                from grokreg.captcha.turnstile_solver_client import solve_and_inject_turnstile

                cfg = config or getattr(click_turnstile_checkbox_robust, "_cfg", {}) or {}
                if await solve_and_inject_turnstile(tab, cfg, reason="wait_token"):
                    return True
            except Exception as e:
                log.debug("wait_token external: %s", e)
        # try one soft checkbox tick while waiting
        try:
            if hasattr(tab, "_bypass_cloudflare"):
                await tab._bypass_cloudflare({}, time_to_wait_captcha=2.0)
        except Exception:
            pass
        await asyncio.sleep(1.0)
    log.warning("Turnstile token not ready after %.0fs", timeout)
    wait_turnstile_token._ext_tried = False  # type: ignore[attr-defined]
    return False


async def castle_probe_js(tab: Any) -> dict[str, Any]:
    """Probe Castle / bot SDK globals on the page."""
    raw = await _exec_js(
        tab,
        """
        (() => {
          const keys = Object.keys(window).filter(k =>
            /castle|_castle|Castle|__CASTLE|fingerprint|Fingerprint/i.test(k)
          ).slice(0, 30);
          const c = window.Castle || window._castle || window.castle || window.__castle
            || (window._castleq ? {_castleq: true} : null);
          let tokenHint = null;
          try {
            // common hidden inputs used by Castle / risk SDKs
            const hid = document.querySelector(
              'input[name*="castle"], input[name*="request_token"], input[name*="device_token"], input[id*="castle"]'
            );
            if (hid && hid.value) tokenHint = {name: hid.name || hid.id, len: (hid.value||'').length};
          } catch (e) {}
          // scripts
          const scripts = [...document.scripts].map(s => s.src || '').filter(s =>
            /castle|fingerprint|cdn\\.castle/i.test(s)
          ).slice(0, 8);
          return {
            hasCastleGlobal: !!c,
            castleType: c ? typeof c : null,
            castleKeys: c && typeof c === 'object' ? Object.keys(c).slice(0, 20) : [],
            windowHits: keys,
            tokenHint: tokenHint,
            scripts: scripts,
            visibility: document.visibilityState,
            readyState: document.readyState
          };
        })()
        """,
    )
    return raw if isinstance(raw, dict) else {"raw": str(raw)[:200]}


async def castle_try_create_token(tab: Any) -> dict[str, Any]:
    """
    Try to mint a Castle request token via common JS APIs.
    Returns {ok, tokenLen, method, error}.
    """
    raw = await _exec_js(
        tab,
        """
        (async () => {
          const out = {ok:false, tokenLen:0, method:null, error:null};
          try {
            const c = window.Castle || window._castle || window.castle;
            if (!c) { out.error = 'no_castle_global'; return out; }
            // Castle.createRequestToken() (modern)
            if (typeof c.createRequestToken === 'function') {
              const t = await Promise.race([
                Promise.resolve(c.createRequestToken()),
                new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 12000))
              ]);
              const s = (t && (t.token || t.requestToken || t)) || '';
              const str = typeof s === 'string' ? s : (s && s.toString ? s.toString() : '');
              out.ok = !!(str && str.length > 10);
              out.tokenLen = str ? str.length : 0;
              out.method = 'createRequestToken';
              return out;
            }
            // Castle.configure + getToken variants
            for (const name of ['getToken', 'getRequestToken', 'mint', 'createToken']) {
              if (typeof c[name] === 'function') {
                try {
                  const t = await Promise.resolve(c[name]());
                  const str = typeof t === 'string' ? t : (t && (t.token || t.requestToken) || '');
                  if (str && String(str).length > 10) {
                    out.ok = true; out.tokenLen = String(str).length; out.method = name;
                    return out;
                  }
                } catch (e) { out.error = String(e).slice(0,120); }
              }
            }
            // _castle('createRequestToken', cb) legacy
            if (typeof window._castle === 'function') {
              const token = await new Promise((resolve) => {
                let done = false;
                try {
                  window._castle('createRequestToken', function(err, t) {
                    done = true;
                    resolve(err ? null : t);
                  });
                } catch (e) { resolve(null); return; }
                setTimeout(() => { if (!done) resolve(null); }, 10000);
              });
              if (token && String(token).length > 10) {
                out.ok = true; out.tokenLen = String(token).length; out.method = '_castle_cb';
                return out;
              }
            }
            out.error = out.error || 'no_mint_api';
            out.castleKeys = c && typeof c === 'object' ? Object.keys(c).slice(0, 15) : [];
          } catch (e) {
            out.error = String(e).slice(0, 160);
          }
          return out;
        })()
        """,
    )
    # _exec_js may not await async IIFE properly — handle both
    if isinstance(raw, dict):
        return raw
    # try wait a bit and re-probe hidden field
    return {"ok": False, "error": f"unexpected:{str(raw)[:80]}", "tokenLen": 0}


async def castle_human_warmup(tab: Any, config: dict[str, Any]) -> dict[str, Any]:
    """
    Castle needs real browser signals before mint works:
      - page visible, scripts loaded
      - mouse move / scroll / idle time
      - do NOT wipe cookies/storage before this
    """
    # Competitor protocol path ~28s/acc — browser path should not burn 40s on Castle alone.
    speed = str(config.get("reg_speed") or "fast").strip().lower()
    default_warm = 3.0 if speed == "fast" else 12.0
    default_tok = 10.0 if speed == "fast" else 28.0
    warmup = float(config.get("castle_warmup_sec") or default_warm)
    wait_tok = float(config.get("castle_wait_token_sec") or default_tok)
    if speed == "fast":
        warmup = min(warmup, 5.0)
        wait_tok = min(wait_tok, 12.0)
    log.info(
        "Castle warmup start (%.1fs interact + wait token ≤%.0fs) speed=%s",
        warmup,
        wait_tok,
        speed,
    )

    # Do not steal OS focus when chrome_steal_focus=false / offscreen
    from grokreg.browser.chrome import maybe_bring_to_front

    await maybe_bring_to_front(tab, config)

    # Human-like motion: scroll + mouse jiggle via JS (works even if pydoll mouse limited)
    t0 = time.time()
    motion_budget = max(0.8, warmup * (0.45 if speed == "fast" else 0.55))
    while time.time() - t0 < motion_budget:
        try:
            await _exec_js(
                tab,
                f"""
                (() => {{
                  const y = {int(random.uniform(40, 280))};
                  window.scrollBy(0, y);
                  const x = {int(random.uniform(80, 700))};
                  const yy = {int(random.uniform(80, 500))};
                  const ev = new MouseEvent('mousemove', {{
                    bubbles:true, clientX:x, clientY:yy, view:window
                  }});
                  document.dispatchEvent(ev);
                  // tiny pointer activity Castle likes
                  document.dispatchEvent(new Event('pointermove', {{bubbles:true}}));
                  document.dispatchEvent(new Event('scroll', {{bubbles:true}}));
                  return {{x, yy, scrollY: window.scrollY}};
                }})()
                """,
            )
            if hasattr(tab, "mouse") and hasattr(tab.mouse, "move"):
                try:
                    await tab.mouse.move(
                        random.randint(100, 800),
                        random.randint(100, 500),
                        humanize=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            log.debug("castle motion: %s", e)
        await asyncio.sleep(random.uniform(0.2, 0.55) if speed == "fast" else random.uniform(0.35, 0.9))

    # Idle dwell (short in fast mode — competitor browser sleeps ~0.5–1.3s)
    await asyncio.sleep(
        random.uniform(0.35, 0.8) if speed == "fast" else random.uniform(1.2, 2.5)
    )
    try:
        await _exec_js(
            tab,
            "(() => { window.scrollTo({top:0, behavior:'smooth'}); return window.scrollY; })()",
        )
    except Exception:
        pass

    probe = await castle_probe_js(tab)
    log.info(
        "Castle probe: hasGlobal=%s scripts=%s visibility=%s hits=%s",
        probe.get("hasCastleGlobal"),
        probe.get("scripts"),
        probe.get("visibility"),
        (probe.get("windowHits") or [])[:8],
    )

    # Wait for Castle token mintability
    deadline = time.time() + wait_tok
    best: dict[str, Any] = {"ok": False}
    while time.time() < deadline:
        # network signal: castle warmup success vs fail in our capture
        mint_fail = any(
            "mint_failed" in str((r or {}).get("postData") or "")
            or "warmup_failed" in str((r or {}).get("postData") or "")
            for r in _NET_REQUESTS.values()
        )
        # try mint
        best = await castle_try_create_token(tab)
        if best.get("ok"):
            log.info(
                "Castle token OK method=%s len=%s",
                best.get("method"),
                best.get("tokenLen"),
            )
            return {"ok": True, "probe": probe, "token": best, "mint_fail_seen": mint_fail}
        # also check hidden input grew
        probe2 = await castle_probe_js(tab)
        th = probe2.get("tokenHint") or {}
        if isinstance(th, dict) and int(th.get("len") or 0) > 20:
            log.info("Castle hidden token field ready len=%s", th.get("len"))
            return {"ok": True, "probe": probe2, "token": {"ok": True, "method": "hidden_input", "tokenLen": th.get("len")}, "mint_fail_seen": mint_fail}
        await asyncio.sleep(1.0)
        # light mouse again every few seconds
        if int(time.time()) % 3 == 0:
            try:
                await _exec_js(
                    tab,
                    f"""
                    (() => {{
                      document.dispatchEvent(new MouseEvent('mousemove', {{
                        bubbles:true, clientX:{random.randint(50,900)}, clientY:{random.randint(50,600)}
                      }}));
                      return 1;
                    }})()
                    """,
                )
            except Exception:
                pass

    log.warning(
        "Castle token not ready after wait (last=%s) — submit may error_generic",
        best,
    )
    return {"ok": False, "probe": probe, "token": best, "mint_fail_seen": False}


def castle_mint_failed_in_network() -> bool:
    """True if F12 capture saw [castle] mint_failed / warmup_failed recently."""
    for req in list(_NET_REQUESTS.values())[-40:]:
        pd = str(req.get("postData") or "")
        url = str(req.get("url") or "")
        if "api/log" in url and ("mint_failed" in pd or "warmup_failed" in pd):
            return True
        if "CastleTokenError" in pd or "mint_failed" in pd:
            return True
    return False


async def dump_email_form_diag(tab: Any) -> dict[str, Any]:
    """Snapshot email form / Turnstile state for debugging xAI error_generic."""
    raw = await _exec_js(
        tab,
        """
        (() => {
          const email = document.querySelector(
            'input[type="email"], input[name="email"], input[autocomplete="email"]'
          );
          const ts = document.querySelector(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
          );
          const iframe = document.querySelector(
            'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"], .cf-turnstile'
          );
          const btns = Array.from(document.querySelectorAll(
            'button, [role="button"], input[type="submit"]'
          )).slice(0, 10).map(b => ({
            t: ((b.innerText || b.value || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 40),
            dis: !!b.disabled,
            aria: b.getAttribute('aria-disabled') || ''
          }));
          const alerts = Array.from(document.querySelectorAll(
            '[role="alert"], .error, [class*="error"], [class*="Error"]'
          )).map(n => (n.innerText || '').trim().slice(0, 120)).filter(Boolean).slice(0, 5);
          return {
            href: location.href,
            email: email ? (email.value || '') : null,
            emailDisabled: email ? !!email.disabled : null,
            turnstileLen: ts ? ((ts.value || '').trim().length) : null,
            turnstilePresent: !!(ts || iframe),
            buttons: btns,
            alerts: alerts,
            title: (document.title || '').slice(0, 80)
          };
        })()
        """,
    )
    if isinstance(raw, dict):
        return raw
    return {"raw": str(raw)[:300]}


async def install_xai_fetch_sniffer(tab: Any) -> None:
    """Capture last xAI API response body (helps decode error_generic)."""
    await _exec_js(
        tab,
        """
        (() => {
          if (window.__xaiSnifferInstalled) return 'already';
          window.__xaiSnifferInstalled = true;
          window.__xai_last_fetch = null;
          window.__xai_last_xhr = null;
          const wrap = (url, status, body) => {
            const u = String(url || '');
            if (!/x\\.ai|spacexai|auth0|oauth|signup|sign-up|register|verify/i.test(u)
                && !/api\\./i.test(u)) return;
            const entry = {
              url: u.slice(0, 220),
              status: status,
              body: String(body || '').slice(0, 800),
              t: Date.now()
            };
            window.__xai_last_fetch = entry;
            try {
              window.__xai_fetches = window.__xai_fetches || [];
              window.__xai_fetches.push(entry);
              if (window.__xai_fetches.length > 12) window.__xai_fetches.shift();
            } catch (e) {}
          };
          const ofetch = window.fetch;
          if (ofetch) {
            window.fetch = async function(...args) {
              const res = await ofetch.apply(this, args);
              try {
                const url = (args[0] && args[0].url) ? args[0].url : args[0];
                const clone = res.clone();
                const text = await clone.text();
                wrap(url, res.status, text);
              } catch (e) {}
              return res;
            };
          }
          const XO = XMLHttpRequest.prototype.open;
          const XS = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function(method, url) {
            this.__xai_url = url;
            return XO.apply(this, arguments);
          };
          XMLHttpRequest.prototype.send = function() {
            this.addEventListener('load', function() {
              try { wrap(this.__xai_url, this.status, this.responseText); } catch (e) {}
            });
            return XS.apply(this, arguments);
          };
          return 'ok';
        })()
        """,
    )


async def read_xai_fetch_sniffer(tab: Any) -> dict[str, Any]:
    raw = await _exec_js(
        tab,
        """
        (() => ({
          last: window.__xai_last_fetch || null,
          all: (window.__xai_fetches || []).slice(-6)
        }))()
        """,
    )
    return raw if isinstance(raw, dict) else {"raw": str(raw)[:400]}



