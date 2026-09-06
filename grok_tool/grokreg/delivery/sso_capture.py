"""
Capture xAI SSO cookie from a live pydoll/Chrome session after successful reg.

Competitor pattern (grok-register-web):
  - Prefer cookie name ``sso``, then ``sso-rw`` / ``sso_token``
  - Domains: .x.ai, accounts.x.ai, auth.x.ai, .grok.com
  - CDP Network.getCookies / Storage.getCookies (HttpOnly-safe)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("grok_tool")

SSO_NAMES = ("sso", "sso-rw", "sso_token", "sso-token")
SSO_URLS = (
    "https://accounts.x.ai/",
    "https://auth.x.ai/",
    "https://grok.com/",
    "https://x.ai/",
    "https://www.grok.com/",
)


def _unwrap_cdp(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    inner = result
    for _ in range(8):
        if not isinstance(inner, dict):
            break
        if "result" in inner and isinstance(inner["result"], dict):
            # {result: {cookies: [...]}} or nested CDP
            r = inner["result"]
            if "cookies" in r or "value" in r or "result" in r:
                inner = r
                continue
        if "value" in inner and "type" in inner:
            return inner.get("value")
        break
    return inner


def _pick_sso(cookies: list[dict[str, Any]]) -> str:
    """Prefer name order + longest non-empty value."""
    by_name: dict[str, str] = {}
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "").strip()
        if not name or not value:
            continue
        if name.lower() not in {n.lower() for n in SSO_NAMES} and not name.lower().startswith(
            "sso"
        ):
            continue
        prev = by_name.get(name.lower(), "")
        if len(value) > len(prev):
            by_name[name.lower()] = value
    for want in SSO_NAMES:
        if by_name.get(want.lower()):
            return by_name[want.lower()]
    # any sso*
    for k, v in by_name.items():
        if k.startswith("sso") and v:
            return v
    return ""


async def _cdp_get_cookies(tab: Any, urls: list[str] | None = None) -> list[dict[str, Any]]:
    """Network.getCookies or Storage.getCookies via pydoll."""
    cookies: list[dict[str, Any]] = []

    def _extract(raw: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        data = _unwrap_cdp(raw)
        # raw may be full CDP: {result:{cookies:[...]}}
        if isinstance(raw, dict) and "result" in raw:
            r0 = raw.get("result")
            if isinstance(r0, dict) and isinstance(r0.get("cookies"), list):
                out.extend([c for c in r0["cookies"] if isinstance(c, dict)])
        if isinstance(data, dict):
            cl = data.get("cookies")
            if isinstance(cl, list):
                out.extend([c for c in cl if isinstance(c, dict)])
            elif isinstance(data.get("result"), dict):
                cl = data["result"].get("cookies")
                if isinstance(cl, list):
                    out.extend([c for c in cl if isinstance(c, dict)])
        if isinstance(data, list):
            out.extend([c for c in data if isinstance(c, dict)])
        return out

    # 1) Network.getCookies scoped to urls
    try:
        from pydoll.commands.network_commands import NetworkCommands

        cmd = NetworkCommands.get_cookies(urls=list(urls or SSO_URLS))
        raw = await tab._execute_command(cmd)
        cookies.extend(_extract(raw))
    except Exception as e:
        log.debug("[sso] Network.getCookies(urls) failed: %s", e)

    # 2) Network.getCookies all (no url filter)
    if not _pick_sso(cookies):
        try:
            from pydoll.commands.network_commands import NetworkCommands

            cmd = NetworkCommands.get_cookies()
            raw = await tab._execute_command(cmd)
            cookies.extend(_extract(raw))
        except Exception as e:
            log.debug("[sso] Network.getCookies() failed: %s", e)

    # 3) Storage.getCookies (all browser cookies)
    if not _pick_sso(cookies):
        try:
            from pydoll.commands.storage_commands import StorageCommands

            cmd = StorageCommands.get_cookies()
            raw = await tab._execute_command(cmd)
            cookies.extend(_extract(raw))
        except Exception as e:
            log.debug("[sso] Storage.getCookies failed: %s", e)

    # 4) raw CDP dict method if tab exposes it
    if not _pick_sso(cookies):
        for method in ("get_cookies", "get_all_cookies"):
            if hasattr(tab, method):
                try:
                    raw = await getattr(tab, method)()
                    if isinstance(raw, list):
                        cookies.extend([c for c in raw if isinstance(c, dict)])
                    else:
                        cookies.extend(_extract(raw))
                except Exception as e:
                    log.debug("[sso] tab.%s failed: %s", method, e)

    if cookies:
        names = sorted(
            {
                str(c.get("name") or "")
                for c in cookies
                if isinstance(c, dict) and c.get("name")
            }
        )
        sso_like = [n for n in names if "sso" in n.lower() or "token" in n.lower()]
        log.info(
            "[sso] CDP cookies=%s sso_like=%s",
            len(cookies),
            sso_like[:12] if sso_like else names[:8],
        )

    return cookies


get_all_cookies = _cdp_get_cookies


async def _js_document_cookies(tab: Any) -> list[dict[str, Any]]:
    """document.cookie — misses HttpOnly but good fallback."""
    try:
        from grokreg.delivery.sub2api_oauth import js

        raw = await js(
            tab,
            """
            (() => {
              const out = [];
              try {
                for (const p of (document.cookie || '').split(';')) {
                  const eq = p.indexOf('=');
                  if (eq < 0) continue;
                  const name = p.slice(0, eq).trim();
                  let value = p.slice(eq + 1).trim();
                  try { value = decodeURIComponent(value); } catch(e) {}
                  if (name) out.push({name, value, domain: location.hostname});
                }
              } catch(e) {}
              return out;
            })()
            """,
        )
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
        if isinstance(raw, str):
            import json

            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return [c for c in data if isinstance(c, dict)]
            except Exception:
                pass
    except Exception as e:
        log.debug("[sso] document.cookie failed: %s", e)
    return []


async def _js_storage_token(tab: Any) -> str:
    """Scan local/session storage for JWT-like SSO (display / weak fallback)."""
    try:
        from grokreg.delivery.sub2api_oauth import js

        raw = await js(
            tab,
            """
            (() => {
              let token = '';
              const scan = (s) => {
                if (!s || typeof s !== 'string') return;
                if (s.startsWith('eyJ') && s.length > 40 && s.length > token.length)
                  token = s;
              };
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i) || '';
                  const v = localStorage.getItem(k) || '';
                  if (/sso|token|jwt|session|auth/i.test(k)) scan(v);
                  scan(v);
                }
              } catch(e) {}
              try {
                for (let i = 0; i < sessionStorage.length; i++) {
                  const k = sessionStorage.key(i) || '';
                  const v = sessionStorage.getItem(k) || '';
                  if (/sso|token|jwt|session|auth/i.test(k)) scan(v);
                }
              } catch(e) {}
              return token || '';
            })()
            """,
        )
        return str(raw or "").strip()
    except Exception:
        return ""


async def capture_sso_cookie(tab: Any, *, navigate_if_needed: bool = True) -> str:
    """
    Capture xAI SSO cookie from current Chrome tab/session.

    Returns empty string when not found (caller may fall back to browser OAuth).
    """
    if tab is None:
        return ""

    # Best effort: hop to accounts.x.ai so domain cookies are loaded
    if navigate_if_needed:
        try:
            from grokreg.delivery.sub2api_oauth import current_url, sleep

            href = (await current_url(tab) or "").lower()
            if "accounts.x.ai" not in href and "auth.x.ai" not in href and "grok.com" not in href:
                try:
                    await tab.go_to("https://accounts.x.ai/account")
                    await sleep(1.5)
                except Exception:
                    pass
        except Exception:
            pass

    cookies = await _cdp_get_cookies(tab)
    sso = _pick_sso(cookies)
    if sso:
        log.info("[sso] captured via CDP cookies len=%s", len(sso))
        return sso

    cookies2 = await _js_document_cookies(tab)
    sso = _pick_sso(cookies2)
    if sso:
        log.info("[sso] captured via document.cookie len=%s", len(sso))
        return sso

    # Weak: storage JWT (not always the real SSO cookie Sub2API expects)
    tok = await _js_storage_token(tab)
    if tok and tok.startswith("eyJ") and len(tok) > 40:
        log.warning(
            "[sso] only storage JWT found (len=%s) — may not work as Sub2API sso cookie",
            len(tok),
        )
        return tok

    log.warning("[sso] cookie not found (CDP + document.cookie empty)")
    return ""


async def capture_session_display(tab: Any) -> tuple[str, str]:
    """
    (user_id, sso_preview) for style_log success block.
    user_id best-effort UUID from storage; sso is real cookie when available.
    """
    sso = await capture_sso_cookie(tab, navigate_if_needed=False)
    user_id = ""
    try:
        from grokreg.delivery.sub2api_oauth import js

        raw = await js(
            tab,
            """
            (() => {
              let userId = '';
              const re = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
              const scan = (o, depth) => {
                if (!o || depth > 3) return;
                if (typeof o === 'string') {
                  if (!userId && re.test(o)) userId = o;
                  return;
                }
                if (typeof o !== 'object') return;
                for (const [k, v] of Object.entries(o)) {
                  const kl = (k||'').toLowerCase();
                  if (typeof v === 'string' && re.test(v) &&
                      (/user|sub|account|id/.test(kl) || true)) {
                    if (!userId) userId = v;
                  }
                  if (typeof v === 'object') scan(v, depth+1);
                }
              };
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i);
                  let v = localStorage.getItem(k);
                  try { v = JSON.parse(v); } catch(e) {}
                  scan(v, 0);
                }
              } catch(e) {}
              return userId || '';
            })()
            """,
        )
        user_id = str(raw or "").strip()
    except Exception:
        pass
    return user_id, sso


def sso_preview(sso: str, head: int = 12, tail: int = 6) -> str:
    s = (sso or "").strip()
    if not s:
        return "(empty)"
    if len(s) <= head + tail + 3:
        return s[:8] + "…"
    return f"{s[:head]}…{s[-tail:]} (len={len(s)})"


def save_storage_state(
    email: str,
    cookies: list[dict[str, Any]],
    origins: list[dict[str, Any]] | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Save full browser storage state (Playwright format) to data/sessions/<email>.json."""
    import json
    import re
    from grokreg.core.runtime import DATA_DIR

    sessions_dir = output_dir or (DATA_DIR / "sessions")
    sessions_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_.@-]", "_", email.strip()) or "anonymous"
    file_path = sessions_dir / f"{safe_name}.json"

    state = {
        "cookies": cookies or [],
        "origins": origins or [],
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        log.info(f"[storage_state] Đã lưu full session: {file_path.name}")
    except Exception as exc:
        log.warning(f"[storage_state] Không thể lưu file session {file_path}: {exc}")

    return file_path

