"""
External Turnstile solver client — ported from grok-register-web
(core/registration/turnstile.py ExternalTurnstileProvider).

Solves Cloudflare Turnstile via:
  A) Local Camoufox HTTP solver  http://127.0.0.1:5072  (services/turnstile_solver)
  B) YesCaptcha API (optional key)

Use for browser reg when pydoll click fails — inject token into page fields.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

log = logging.getLogger("grok_tool")

DEFAULT_SOLVER_URL = "http://127.0.0.1:5072"
# Common xAI accounts sitekey (fallback if page scrape fails)
DEFAULT_XAI_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"


class TurnstileSolveError(RuntimeError):
    pass


def probe_solver(solver_url: str = DEFAULT_SOLVER_URL, timeout: float = 2.0) -> dict[str, Any]:
    url = (solver_url or DEFAULT_SOLVER_URL).rstrip("/")
    session = requests.Session()
    session.trust_env = False  # don't ride system HTTP_PROXY for loopback
    t0 = time.perf_counter()
    try:
        r = session.get(f"{url}/", timeout=timeout, allow_redirects=False)
        ms = int((time.perf_counter() - t0) * 1000)
        return {
            "online": r.status_code < 500,
            "status_code": r.status_code,
            "latency_ms": ms,
            "url": url,
        }
    except Exception as e:
        return {
            "online": False,
            "error": str(e)[:120],
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "url": url,
        }


class ExternalTurnstileSolver:
    def __init__(
        self,
        *,
        solver_url: str = DEFAULT_SOLVER_URL,
        yescaptcha_key: str = "",
        proxy: str = "",
        timeout: int = 90,
        poll_interval: float = 2.0,
    ):
        self.solver_url = (solver_url or DEFAULT_SOLVER_URL).rstrip("/")
        self.yescaptcha_key = (yescaptcha_key or "").strip()
        self.proxy = (proxy or "").strip()
        self.timeout = max(20, int(timeout or 90))
        self.poll_interval = max(0.5, float(poll_interval or 2.0))
        self._http = requests.Session()
        self._http.trust_env = False

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> "ExternalTurnstileSolver":
        cfg = config or {}
        ts = dict(cfg.get("turnstile") or cfg.get("turnstile_solver") or {})
        return cls(
            solver_url=str(
                ts.get("solver_url")
                or cfg.get("turnstile_solver_url")
                or DEFAULT_SOLVER_URL
            ),
            yescaptcha_key=str(
                ts.get("yescaptcha_key") or cfg.get("yescaptcha_key") or ""
            ),
            proxy=str(ts.get("proxy") or cfg.get("proxy") or ""),
            timeout=int(ts.get("timeout_sec") or 90),
        )

    def available(self) -> bool:
        if self.yescaptcha_key:
            return True
        return bool(probe_solver(self.solver_url).get("online"))

    def solve(self, *, url: str, site_key: str) -> str:
        if not site_key:
            raise TurnstileSolveError("missing site_key")
        website = (url or "https://accounts.x.ai").strip()
        if self.yescaptcha_key:
            token = self._solve_yescaptcha(website, site_key)
            name = "yescaptcha"
        else:
            token = self._solve_local(website, site_key)
            name = "local_solver"
        if not token or token == "CAPTCHA_FAIL":
            raise TurnstileSolveError(f"{name} empty token")
        log.info(
            "[turnstile] solved via %s len=%s url=%s",
            name,
            len(token),
            website[:60],
        )
        return token

    def _solve_local(self, website: str, site_key: str) -> str:
        create_url = (
            f"{self.solver_url}/turnstile"
            f"?url={quote(website, safe='')}"
            f"&sitekey={quote(site_key, safe='')}"
        )
        if self.proxy:
            create_url += f"&proxy={quote(self.proxy, safe='')}"
        try:
            create = self._http.get(create_url, timeout=30)
            create.raise_for_status()
            task_id = (create.json() or {}).get("taskId")
        except Exception as exc:
            raise TurnstileSolveError(f"local solver create failed: {exc}") from exc
        if not task_id:
            raise TurnstileSolveError("local solver returned no taskId")
        log.info("[turnstile] local solver taskId=%s — polling…", task_id)

        # Use a monotonic deadline and keep each HTTP poll shorter than the
        # remaining budget.  Previously a 20s request could overrun the
        # deadline and leave the registration worker apparently frozen.
        deadline = time.monotonic() + self.timeout
        time.sleep(min(4.0, self.timeout / 4))
        poll_no = 0
        while time.monotonic() < deadline:
            poll_no += 1
            remaining = max(0.1, deadline - time.monotonic())
            try:
                result = self._http.get(
                    f"{self.solver_url}/result?id={quote(str(task_id), safe='')}",
                    timeout=min(5.0, remaining),
                )
                result.raise_for_status()
                payload = result.json() or {}
                token = (payload.get("solution") or {}).get("token")
                if token:
                    return str(token).strip()
                # some solvers return value directly
                if payload.get("value") and len(str(payload["value"])) > 40:
                    return str(payload["value"]).strip()
                if payload.get("status") in ("error", "failed"):
                    raise TurnstileSolveError(f"solver failed: {payload}")
            except TurnstileSolveError:
                raise
            except Exception as exc:
                log.warning("[turnstile] poll %s error: %s", poll_no, exc)
            else:
                if poll_no == 1 or poll_no % 5 == 0:
                    log.info(
                        "[turnstile] polling task=%s poll=%s remaining=%.1fs",
                        task_id,
                        poll_no,
                        max(0.0, deadline - time.monotonic()),
                    )
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise TurnstileSolveError(f"local solver timed out after {self.timeout}s")

    def _solve_yescaptcha(self, website: str, site_key: str) -> str:
        task: dict[str, Any] = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": website,
            "websiteKey": site_key,
        }
        if self.proxy:
            # minimal proxy parse
            try:
                from urllib.parse import urlparse

                p = urlparse(self.proxy)
                if p.hostname:
                    task = {
                        "type": "TurnstileTask",
                        "websiteURL": website,
                        "websiteKey": site_key,
                        "proxyType": "http",
                        "proxyAddress": p.hostname,
                        "proxyPort": str(p.port or 80),
                    }
                    if p.username:
                        task["proxyLogin"] = p.username
                    if p.password:
                        task["proxyPassword"] = p.password
            except Exception:
                pass
        create = self._http.post(
            "https://api.yescaptcha.com/createTask",
            json={"clientKey": self.yescaptcha_key, "task": task},
            timeout=30,
        )
        create.raise_for_status()
        data = create.json()
        if data.get("errorId") not in (0, None):
            raise TurnstileSolveError(
                f"YesCaptcha create: {data.get('errorDescription') or data}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise TurnstileSolveError("YesCaptcha no taskId")
        deadline = time.time() + self.timeout
        time.sleep(min(5.0, self.timeout / 3))
        while time.time() < deadline:
            result = self._http.post(
                "https://api.yescaptcha.com/getTaskResult",
                json={"clientKey": self.yescaptcha_key, "taskId": task_id},
                timeout=30,
            )
            result.raise_for_status()
            payload = result.json()
            if payload.get("errorId") not in (0, None):
                raise TurnstileSolveError(
                    f"YesCaptcha result: {payload.get('errorDescription') or payload}"
                )
            if payload.get("status") == "ready":
                return str((payload.get("solution") or {}).get("token") or "").strip()
            time.sleep(self.poll_interval)
        raise TurnstileSolveError(f"YesCaptcha timeout {self.timeout}s")


_SITEKEY_RE = re.compile(r"(0x4[A-Za-z0-9_-]{10,})", re.I)


async def extract_sitekey_from_tab(tab: Any) -> str:
    """Read Turnstile sitekey from live page (DOM + HTML)."""
    try:
        from grokreg.delivery.sub2api_oauth import js
    except Exception:
        js = None

    if js:
        raw = await js(
            tab,
            """
            (() => {
              const el = document.querySelector('[data-sitekey]');
              if (el) return el.getAttribute('data-sitekey') || '';
              const inp = document.querySelector('.cf-turnstile, [class*="turnstile"]');
              if (inp && inp.dataset && inp.dataset.sitekey) return inp.dataset.sitekey;
              // iframe src
              for (const f of document.querySelectorAll('iframe')) {
                const s = f.src || '';
                const m = s.match(/[?&]sitekey=([^&]+)/i);
                if (m) return decodeURIComponent(m[1]);
              }
              const html = document.documentElement ? document.documentElement.innerHTML : '';
              const m2 = html.match(/0x4[A-Za-z0-9_-]{10,}/);
              return m2 ? m2[0] : '';
            })()
            """,
        )
        sk = str(raw or "").strip()
        if sk.startswith("0x4"):
            return sk
        m = _SITEKEY_RE.search(sk)
        if m:
            return m.group(1)
    return ""


async def inject_turnstile_token(tab: Any, token: str) -> bool:
    """Write solved token into page fields so form submit accepts CF."""
    token = (token or "").strip()
    if not token or len(token) < 20:
        return False
    try:
        from grokreg.delivery.sub2api_oauth import js
    except Exception:
        return False
    raw = await js(
        tab,
        f"""
        (() => {{
          const token = {token!r};
          let n = 0;
          const setVal = (el) => {{
            if (!el) return;
            try {{
              const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              const prev = el.value;
              if (desc && desc.set) desc.set.call(el, token);
              else el.value = token;
              if (el._valueTracker) try {{ el._valueTracker.setValue(prev); }} catch(e) {{}}
              el.dispatchEvent(new Event('input', {{bubbles:true}}));
              el.dispatchEvent(new Event('change', {{bubbles:true}}));
              n++;
            }} catch(e) {{}}
          }};
          document.querySelectorAll(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="cf_challenge_response"]'
          ).forEach(setVal);
          // hidden fields often created by widget
          document.querySelectorAll('input[type="hidden"]').forEach(el => {{
            if (/turnstile|cf-chl|cf_clearance/i.test(el.name||'')) setVal(el);
          }});
          try {{
            if (window.turnstile && typeof turnstile.getResponse === 'function') {{
              // cannot set getResponse easily; callback via custom event
            }}
          }} catch(e) {{}}
          try {{
            window.dispatchEvent(new CustomEvent('cf-turnstile-response', {{detail: token}}));
          }} catch(e) {{}}
          // mark success for our waiters
          window.__grokTurnstileToken = token;
          return n;
        }})()
        """,
    )
    n = int(raw or 0) if not isinstance(raw, bool) else 0
    log.info("[turnstile] inject token into %s field(s) len=%s", n, len(token))
    return n > 0 or bool(token)


async def solve_and_inject_turnstile(
    tab: Any,
    config: dict[str, Any] | None = None,
    *,
    page_url: str = "",
    reason: str = "",
) -> bool:
    """
    Prefer external Camoufox/YesCaptcha solver, inject token into current page.
    Returns True if token ready on page.
    """
    cfg = config or {}
    ts = dict(cfg.get("turnstile") or {})
    mode = str(ts.get("mode") or cfg.get("turnstile_provider") or "auto").lower()
    if mode in ("browser", "none", "off", "disabled", "pydoll"):
        return False

    solver = ExternalTurnstileSolver.from_config(cfg)
    if not solver.available():
        log.warning(
            "[turnstile] external solver offline (%s) — fallback browser click%s",
            solver.solver_url,
            f" [{reason}]" if reason else "",
        )
        return False

    try:
        from grokreg.delivery.sub2api_oauth import current_url
    except Exception:
        current_url = None

    url = page_url
    if not url and current_url:
        try:
            url = await current_url(tab)
        except Exception:
            url = ""
    url = url or "https://accounts.x.ai/sign-up"

    site_key = await extract_sitekey_from_tab(tab)
    if not site_key:
        site_key = str(ts.get("sitekey") or DEFAULT_XAI_SITEKEY)
        log.warning("[turnstile] sitekey not in DOM — using fallback %s", site_key[:20])

    log.info(
        "[turnstile] external solve start%s sitekey=%s url=%s",
        f" [{reason}]" if reason else "",
        site_key[:24],
        url[:70],
    )
    try:
        # run sync HTTP in thread
        import asyncio

        token = await asyncio.to_thread(solver.solve, url=url, site_key=site_key)
    except Exception as e:
        log.error("[turnstile] external solve failed: %s", e)
        return False

    ok = await inject_turnstile_token(tab, token)
    # verify field
    try:
        from grokreg.delivery.sub2api_oauth import js

        ready = await js(
            tab,
            """
            (() => {
              const el = document.querySelector(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
              );
              const v = el ? (el.value||'') : (window.__grokTurnstileToken||'');
              return v.length > 40;
            })()
            """,
        )
        if ready:
            log.info("[turnstile] token verified on page%s", f" [{reason}]" if reason else "")
            return True
    except Exception:
        pass
    return ok
