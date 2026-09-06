"""
Sub2API Grok OAuth importer — runs after successful Grok registration.

Flow (same as manual UI):
  admin/accounts → Create → name/group/Grok/OAuth → Next
  → Manual Authorization → Generate Auth URL
  → open OAuth URL → Sign in with EMAIL (never Google) → Allow
  → paste code → Complete Authorization → Test connection
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("grok_tool")

ROOT = Path(__file__).resolve().parents[2]
COUNTER_FILE = ROOT / "data" / "sub2api_name_counter.json"
_COUNTER_LOCK = threading.Lock()

LABELS = {
    "create_account": [
        "Create Account",
        "创建账号",
        "创建账户",
        "Tạo tài khoản",
        "新建账号",
    ],
    "next": ["Next", "下一步", "Kế tiếp", "继续"],
    "manual_auth": ["Manual Authorization", "手动授权", "Ủy quyền thủ công"],
    "generate_url": [
        "Generate Auth URL",
        "生成授权 URL",
        "生成授权链接",
        "Tạo URL xác thực",
    ],
    "complete_auth": [
        "Complete Authorization",
        "完成授权",
        "Ủy quyền hoàn chỉnh",
    ],
    "test_connection": [
        "Test Connection",
        "Test Account",
        "测试连接",
        "测试账号",
        "Kiểm tra kết nối",
    ],
    "start_test": ["Start Test", "开始测试", "Bắt đầu kiểm tra", "Retry", "重试"],
    "allow": ["Allow", "允许", "Authorize", "授权", "Approve"],
    "close": ["Close", "关闭", "Đóng", "Cancel", "取消"],
    # EMAIL path only — never social
    "sign_in_email": [
        "Sign in with email",
        "Sign up with email",
        "Continue with email",
        "Use email",
        "Log in with email",
        "邮箱登录",
        "使用邮箱",
        "Đăng nhập bằng email",
    ],
}

_SOCIAL_BLOCK = re.compile(
    r"with\s+(x|google|apple|facebook|microsoft|github|twitter)|"
    r"sign\s*(in|up)\s*with\s+(?!email)|"
    r"continue\s*with\s+(?!email)|"
    r"\bgoogle\b|\bapple\b|\bfacebook\b",
    re.I,
)


@dataclass
class Sub2APIResult:
    ok: bool
    name: str
    stage: str
    message: str


# ---------------------------------------------------------------------------
# JS helpers
# ---------------------------------------------------------------------------


def _unwrap_js(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    try:
        inner = result
        for _ in range(8):
            if not isinstance(inner, dict):
                break
            if "type" in inner and ("value" in inner or inner.get("type") == "undefined"):
                val = inner.get("value")
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


async def js(tab: Any, script: str) -> Any:
    script_stripped = script.strip()
    candidates = [script_stripped]
    if script_stripped.startswith("(()") or script_stripped.startswith("(function"):
        candidates.append(
            f"(() => {{ const __r = ({script_stripped}); "
            f"try {{ return JSON.stringify(__r); }} catch (e) {{ return String(__r); }} }})()"
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
                val = _unwrap_js(raw)
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


async def sleep(sec: float) -> None:
    await asyncio.sleep(sec)


async def current_url(tab: Any) -> str:
    try:
        prop = getattr(tab, "current_url", None)
        if prop is not None:
            # pydoll: current_url may be async property/method
            if callable(prop) and not asyncio.iscoroutine(prop):
                try:
                    prop = prop()
                except TypeError:
                    pass
            if asyncio.iscoroutine(prop) or asyncio.isfuture(prop):
                prop = await prop
            if prop and not asyncio.iscoroutine(prop):
                return str(prop)
    except Exception:
        pass
    val = await js(tab, "location.href")
    return str(val or "")


async def wait_until(pred, timeout: float, interval: float = 0.5, desc: str = "") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await pred():
                return True
        except Exception:
            pass
        await sleep(interval)
    if desc:
        log.warning("[sub2api] timeout: %s (%.0fs)", desc, timeout)
    return False


# ---------------------------------------------------------------------------
# DOM
# ---------------------------------------------------------------------------


async def click_text_safe(
    tab: Any,
    labels: list[str],
    *,
    exclude_social: bool = True,
) -> bool:
    labels_json = json.dumps([x.lower() for x in labels])
    excl = "true" if exclude_social else "false"
    script = f"""
    (() => {{
      const labels = {labels_json};
      const excludeSocial = {excl};
      const socialRe = /with\\s+(x|google|apple|facebook|microsoft|github|twitter)|sign\\s*(in|up)\\s*with\\s+(?!email)|continue\\s*with\\s+(?!email)|\\bgoogle\\b|\\bapple\\b/i;
      const nodes = [...document.querySelectorAll(
        'button, a, [role=button], label, input[type=submit], input[type=button]'
      )];
      const labelOf = (el) => ((el.innerText || el.textContent || el.value
        || el.getAttribute('aria-label') || '') + '').replace(/\\s+/g, ' ').trim().toLowerCase();
      let best = null, bestScore = -1;
      for (const el of nodes) {{
        if (el.disabled) continue;
        const t = labelOf(el);
        if (!t) continue;
        if (excludeSocial && socialRe.test(t) && !t.includes('email')) continue;
        let score = -1;
        for (const lab of labels) {{
          if (t === lab) score = Math.max(score, 100);
          else if (t.includes(lab)) score = Math.max(score, 50 + Math.min(lab.length, 20));
        }}
        if (score > bestScore) {{ bestScore = score; best = el; }}
      }}
      if (!best || bestScore < 0) return false;
      best.scrollIntoView({{block:'center'}});
      best.click();
      return true;
    }})()
    """
    return bool(await js(tab, script))


async def set_value(tab: Any, selector: str, value: str) -> bool:
    script = f"""
    (() => {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return false;
      el.focus();
      const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(el, {json.dumps(value)});
      else el.value = {json.dumps(value)};
      for (const ev of ['input','change','keyup'])
        el.dispatchEvent(new Event(ev, {{bubbles:true}}));
      return true;
    }})()
    """
    return bool(await js(tab, script))


async def fill_first(tab: Any, selectors: list[str], value: str, hints: list[str] | None = None) -> bool:
    for sel in selectors:
        if await set_value(tab, sel, value):
            return True
    if hints:
        script = f"""
        (() => {{
          const hints = {json.dumps([h.lower() for h in hints])};
          const inputs = [...document.querySelectorAll('input, textarea')];
          for (const el of inputs) {{
            const id = el.id || '';
            let lab = '';
            if (id) {{
              const l = document.querySelector('label[for=\"' + CSS.escape(id) + '\"]');
              if (l) lab = (l.innerText || '').toLowerCase();
            }}
            const blob = (lab + ' ' + (el.placeholder||'') + ' ' + (el.name||'') + ' '
              + (el.getAttribute('aria-label')||'') + ' ' + (el.type||'')).toLowerCase();
            if (!hints.some(h => blob.includes(h))) continue;
            // skip hidden
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            el.focus();
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, {json.dumps(value)});
            else el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return true;
          }}
          return false;
        }})()
        """
        if await js(tab, script):
            return True
    return False


async def click_selector(tab: Any, selector: str) -> bool:
    return bool(
        await js(
            tab,
            f"""
            (() => {{
              const el = document.querySelector({json.dumps(selector)});
              if (!el) return false;
              el.scrollIntoView({{block:'center'}});
              el.click();
              return true;
            }})()
            """,
        )
    )


async def click_continue_email_form(tab: Any) -> bool:
    """Continue/Next near email form — never social."""
    script = """
    (() => {
      const bad = /with\\s+(x|google|apple|facebook|microsoft|github|twitter)|sign (in|up) with(?! email)|continue with(?! email)/i;
      const labelOf = (n) => (n.innerText || n.value || n.getAttribute('aria-label') || '')
        .replace(/\\s+/g, ' ').trim();
      const email = document.querySelector(
        'input[type="email"], input[name="email"], input[autocomplete="email"], input[autocomplete="username"]'
      );
      const roots = [];
      if (email) {
        const form = email.closest('form');
        if (form) roots.push(form);
        let p = email.parentElement;
        for (let i = 0; i < 6 && p; i++, p = p.parentElement) roots.push(p);
      }
      roots.push(document);
      const wantRe = /^(continue|next|submit|log\\s*in|sign\\s*in|verify)$/i;
      for (const root of roots) {
        const nodes = [...root.querySelectorAll('button, [role=button], input[type=submit]')];
        for (const n of nodes) {
          const t = labelOf(n);
          if (!t || bad.test(t)) continue;
          const r = n.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) continue;
          if (wantRe.test(t) || n.type === 'submit') {
            n.click();
            return t || 'submit';
          }
        }
      }
      return null;
    })()
    """
    r = await js(tab, script)
    if r:
        log.info("[oauth] clicked continue: %s", r)
        return True
    return False


# ---------------------------------------------------------------------------
# naming counter
# ---------------------------------------------------------------------------


def next_account_name(cfg: dict[str, Any]) -> str:
    prefix = (cfg.get("name_prefix") or "grok free").strip()
    start = int(cfg.get("start_number") or 1)
    with _COUNTER_LOCK:
        n = start
        try:
            if COUNTER_FILE.exists():
                data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
                n = max(int(data.get("next") or start), start)
        except Exception:
            n = start
        name = f"{prefix} {n:03d}"
        try:
            COUNTER_FILE.write_text(json.dumps({"next": n + 1}, indent=2), encoding="utf-8")
        except Exception:
            pass
    return name


def peek_account_name(cfg: dict[str, Any]) -> str:
    prefix = (cfg.get("name_prefix") or "grok free").strip()
    start = int(cfg.get("start_number") or 1)
    n = start
    try:
        if COUNTER_FILE.exists():
            data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
            n = max(int(data.get("next") or start), start)
    except Exception:
        pass
    return f"{prefix} {n:03d}"


# ---------------------------------------------------------------------------
# Sub2API UI
# ---------------------------------------------------------------------------


async def ensure_sub2api_login(tab: Any, cfg: dict[str, Any]) -> None:
    base = (cfg.get("sub2api_url") or "http://localhost:8080").rstrip("/")
    user = (cfg.get("sub2api_user") or "").strip()
    password = (cfg.get("sub2api_pass") or "").strip()

    log.info("[sub2api] open %s/admin/accounts", base)
    await tab.go_to(f"{base}/admin/accounts")
    await sleep(2)

    url = await current_url(tab)
    has_pw = bool(
        await js(tab, "!!document.querySelector('input[type=password], input[name=password]')")
    )
    if "/admin" in url and "/login" not in url and not has_pw:
        log.info("[sub2api] already logged in")
        return

    if not user or not password:
        raise RuntimeError("sub2api_user / sub2api_pass missing in config")

    log.info("[sub2api] login as %s", user)
    await tab.go_to(f"{base}/login")
    await sleep(1.5)
    ok_e = await fill_first(
        tab,
        ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="username"]', 'input[type="text"]'],
        user,
        ["email", "user"],
    )
    ok_p = await fill_first(
        tab,
        ['input[type="password"]', 'input[name="password"]'],
        password,
        ["password"],
    )
    if not ok_e or not ok_p:
        raise RuntimeError("sub2api login form not filled")
    if not await click_text_safe(tab, ["Log in", "Login", "Sign in", "登录", "Đăng nhập"], exclude_social=True):
        await click_selector(tab, 'button[type="submit"]')
    await sleep(2.5)
    await tab.go_to(f"{base}/admin/accounts")
    await sleep(2)
    url = await current_url(tab)
    if "/login" in url:
        raise RuntimeError("sub2api login failed")
    log.info("[sub2api] accounts page ok")


async def open_create_modal(tab: Any) -> None:
    log.info("[sub2api] Create Account")
    if not await click_text_safe(tab, LABELS["create_account"]):
        ok = bool(
            await js(
                tab,
                """
                (() => {
                  const b = [...document.querySelectorAll('button')]
                    .find(x => /create|新建|创建|tạo/i.test(x.innerText||''));
                  if (b) { b.click(); return true; }
                  return false;
                })()
                """,
            )
        )
        if not ok:
            raise RuntimeError("cannot open Create Account")
    await sleep(1.2)
    ok = await wait_until(
        lambda: js(tab, "!!document.querySelector('[data-tour=\"account-form-name\"]')"),
        12,
        desc="create form",
    )
    if not ok:
        raise RuntimeError("create form not visible")


async def fill_step1(tab: Any, name: str, group: str) -> None:
    log.info("[sub2api] step1 name=%s group=%s Grok/OAuth", name, group)
    if not await fill_first(
        tab,
        ['[data-tour="account-form-name"]'],
        name,
        ["account name", "name", "名称"],
    ):
        raise RuntimeError("cannot fill name")

    ok_plat = bool(
        await js(
            tab,
            """
            (() => {
              const root = document.querySelector('[data-tour="account-form-platform"]') || document;
              const b = [...root.querySelectorAll('button')]
                .find(x => /\\bgrok\\b/i.test((x.innerText||'').trim()));
              if (!b) return false;
              b.click();
              return true;
            })()
            """,
        )
    )
    if not ok_plat:
        raise RuntimeError("cannot select Grok platform")
    await sleep(0.4)

    # OAuth radio if present
    await js(
        tab,
        """
        (() => {
          for (const r of document.querySelectorAll('input[type=radio]')) {
            if ((r.value||'').toLowerCase() === 'oauth') { r.click(); return true; }
          }
          return false;
        })()
        """,
    )
    await sleep(0.3)

    # group checkbox
    ok_g = bool(
        await js(
            tab,
            f"""
            (() => {{
              const want = {json.dumps(group)}.toLowerCase();
              const root = document.querySelector('[data-tour="account-form-groups"]') || document;
              const search = root.querySelector('input[type=text], input:not([type])');
              if (search) {{
                search.focus();
                const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (d && d.set) d.set.call(search, {json.dumps(group)});
                else search.value = {json.dumps(group)};
                search.dispatchEvent(new Event('input', {{bubbles:true}}));
              }}
              for (const lab of root.querySelectorAll('label')) {{
                const t = ((lab.innerText||'')+'').toLowerCase();
                if (!t.includes(want)) continue;
                const cb = lab.querySelector('input[type=checkbox]');
                if (cb) {{ if (!cb.checked) cb.click(); return true; }}
                lab.click();
                return true;
              }}
              for (const lab of document.querySelectorAll('label')) {{
                const t = ((lab.innerText||'')+'').toLowerCase();
                if (!t.includes(want)) continue;
                const cb = lab.querySelector('input[type=checkbox]');
                if (cb) {{ if (!cb.checked) cb.click(); return true; }}
              }}
              return false;
            }})()
            """,
        )
    )
    if not ok_g:
        await sleep(0.6)
        ok_g = bool(
            await js(
                tab,
                f"""
                (() => {{
                  const want = {json.dumps(group)}.toLowerCase();
                  for (const lab of document.querySelectorAll('label')) {{
                    const t = ((lab.innerText||'')+'').toLowerCase();
                    if (t.includes(want)) {{
                      const cb = lab.querySelector('input[type=checkbox]');
                      if (cb && !cb.checked) cb.click();
                      else lab.click();
                      return true;
                    }}
                  }}
                  return false;
                }})()
                """,
            )
        )
    if not ok_g:
        raise RuntimeError(f'group not found: "{group}"')

    log.info("[sub2api] Next")
    if not await click_selector(tab, '[data-tour="account-form-submit"]'):
        if not await click_text_safe(tab, LABELS["next"]):
            raise RuntimeError("cannot click Next")
    await sleep(1.5)


async def choose_manual_auth(tab: Any) -> None:
    log.info("[sub2api] Manual Authorization")
    ok = bool(
        await js(
            tab,
            """
            (() => {
              for (const r of document.querySelectorAll('input[type=radio]')) {
                if ((r.value||'').toLowerCase() === 'manual') { r.click(); return true; }
              }
              return false;
            })()
            """,
        )
    )
    if not ok:
        await click_text_safe(tab, LABELS["manual_auth"])
    await sleep(0.5)


async def read_auth_url(tab: Any) -> str:
    val = await js(
        tab,
        """
        (() => {
          for (const el of document.querySelectorAll('input, textarea, a[href]')) {
            const v = (el.value || el.href || el.innerText || '').trim();
            if (/^https?:\\/\\//i.test(v) && /oauth|x\\.ai|auth\\.x\\.ai/i.test(v))
              return v.split(/\\s/)[0];
          }
          return '';
        })()
        """,
    )
    return str(val or "").strip()


async def generate_auth_url(tab: Any, timeout: float = 30) -> str:
    log.info("[sub2api] Generate Auth URL")
    existing = await read_auth_url(tab)
    if existing:
        return existing
    if not await click_text_safe(tab, LABELS["generate_url"]):
        raise RuntimeError("cannot click Generate Auth URL")

    async def _p() -> bool:
        return bool(await read_auth_url(tab))

    ok = await wait_until(_p, timeout, desc="auth url")
    if not ok:
        raise RuntimeError("auth url not generated")
    url = await read_auth_url(tab)
    log.info("[sub2api] auth_url=%s", url[:100] + ("..." if len(url) > 100 else ""))
    return url


def is_callback_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower().replace("%3d", "=").replace("%3f", "?").replace("%26", "&")
    # sub2api / xAI redirect: http://127.0.0.1:56121/callback?code=...&state=...
    if "code=" not in u:
        return False
    if any(x in u for x in ("127.0.0.1", "localhost", "56121", "/callback", "auth/callback")):
        return True
    # bare query with oauth code + state
    if "state=" in u and "code=" in u:
        return True
    return False


def scan_cdp_tabs_for_callback(ports: list[int] | None = None) -> str:
    """Scan all Chrome tabs via CDP HTTP for callback URL (even error pages keep href)."""
    import requests as _req

    ports = ports or [9337, 9336, 9335, 9333, 9340]
    for port in ports:
        try:
            pages = _req.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
        except Exception:
            continue
        if not isinstance(pages, list):
            continue
        for p in pages:
            url = str(p.get("url") or "")
            title = str(p.get("title") or "")
            if is_callback_url(url):
                return url
            # sometimes title holds the failed URL
            if is_callback_url(title):
                return title
            if "code=" in url and ("56121" in url or "callback" in url.lower()):
                return url
    return ""


# xAI device / manual OAuth finish page code (e.g. LtlZno5S6wsKE_7WBFGBlcgakuk5L2bHtliDL1uF6Zh4zlHnh5)
_OAUTH_CODE_RE = re.compile(r"\b([A-Za-z0-9_\-]{20,128})\b")


async def click_allow_buttons(tab: Any) -> str:
    """
    Click Allow / Authorize on consent or 'wants to Access other apps' popup.
    Never clicks Deny/Cancel.
    """
    return str(
        await js(
            tab,
            """
            (() => {
              const nodes = [...document.querySelectorAll(
                'button, [role=button], input[type=submit], a[role=button]'
              )];
              const labelOf = (n) => ((n.innerText || n.value || n.getAttribute('aria-label') || '') + '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const bad = /deny|cancel|reject|decline|not now|拒绝|取消/;
              // exact Allow first
              for (const n of nodes) {
                const t = labelOf(n);
                if (!t || bad.test(t)) continue;
                if (t === 'allow' || t === 'authorize' || t === 'approve' || t === '允许' || t === '授权' || t === 'accept') {
                  n.scrollIntoView({block:'center'});
                  n.click();
                  return t;
                }
              }
              // contains allow + access (popup: wants to Access other apps)
              for (const n of nodes) {
                const t = labelOf(n);
                if (!t || bad.test(t)) continue;
                if (/\\ballow\\b|authorize|approve|允许|授权/.test(t) && t.length < 40) {
                  n.scrollIntoView({block:'center'});
                  n.click();
                  return t;
                }
              }
              return '';
            })()
            """,
        )
        or ""
    )


async def page_shows_finish_code(tab: Any) -> bool:
    text = str(
        await js(tab, "document.body ? (document.body.innerText||'').slice(0,2500) : ''") or ""
    ).lower()
    return bool(
        re.search(
            r"enter this code|finish signing in|copy to clipboard|paste this code|verification code|authorization code",
            text,
            re.I,
        )
    )


async def extract_oauth_finish_code(tab: Any) -> str:
    """
    Read OAuth finish code from xAI page:
      'Enter this code to finish signing in' + Copy to clipboard.
    Prefer input/readonly/code next to Copy button; never use short UI words.
    """
    raw = await js(
        tab,
        """
        (() => {
          const reject = /^(allow|deny|cancel|continue|next|login|sign|copy|clipboard|enter|this|code|finish|signing|access|other|apps|authorize|approve)$/i;
          const looksCode = (s) => {
            s = (s || '').trim();
            if (s.length < 20 || s.length > 128) return false;
            if (!/^[A-Za-z0-9_\\-]+$/.test(s)) return false;
            if (reject.test(s)) return false;
            // must look like opaque token (mix of cases/digits or long underscore)
            if (!/[A-Za-z]/.test(s) || !/[0-9A-Za-z]/.test(s)) return false;
            return true;
          };

          // 1) input / textarea value
          for (const el of document.querySelectorAll('input, textarea, [contenteditable=true]')) {
            const v = (el.value || el.textContent || '').trim();
            if (looksCode(v)) return v;
          }
          // 2) code / pre / mono near "copy"
          const candidates = [];
          for (const el of document.querySelectorAll('code, pre, kbd, span, div, p, button, label')) {
            const t = ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
            if (looksCode(t)) candidates.push({t, score: 10});
            // single child text
            if (t.length > 20 && t.length < 200 && looksCode(t.split(' ').pop())) {
              const last = t.split(' ').pop();
              if (looksCode(last)) candidates.push({t: last, score: 8});
            }
          }
          // 3) button "Copy to clipboard" — code often in data-* or previous sibling
          for (const btn of document.querySelectorAll('button, [role=button], a')) {
            const lab = ((btn.innerText || btn.getAttribute('aria-label') || '') + '').toLowerCase();
            if (!/copy/.test(lab)) continue;
            const attrs = [btn.getAttribute('data-code'), btn.getAttribute('data-clipboard-text'),
              btn.getAttribute('data-value'), btn.getAttribute('value')];
            for (const a of attrs) {
              if (looksCode(a || '')) return (a || '').trim();
            }
            let sib = btn.previousElementSibling;
            for (let i = 0; i < 4 && sib; i++, sib = sib.previousElementSibling) {
              const st = ((sib.innerText || sib.value || '') + '').trim();
              if (looksCode(st)) return st;
              const inp = sib.querySelector && sib.querySelector('input, textarea, code');
              if (inp) {
                const v = (inp.value || inp.innerText || '').trim();
                if (looksCode(v)) return v;
              }
            }
            let par = btn.parentElement;
            for (let i = 0; i < 5 && par; i++, par = par.parentElement) {
              for (const el of par.querySelectorAll('input, textarea, code, pre, span, div')) {
                const v = (el.value || el.innerText || '').replace(/\\s+/g, ' ').trim();
                if (looksCode(v)) return v;
              }
            }
          }
          // 4) body regex longest token
          const body = (document.body && document.body.innerText || '');
          const re = /\\b([A-Za-z0-9_\\-]{24,128})\\b/g;
          let m, best = '';
          while ((m = re.exec(body))) {
            if (looksCode(m[1]) && m[1].length > best.length) best = m[1];
          }
          if (best) return best;
          if (candidates.length) {
            candidates.sort((a, b) => b.t.length - a.t.length);
            return candidates[0].t;
          }
          return '';
        })()
        """,
    )
    code = str(raw or "").strip()
    if code and _OAUTH_CODE_RE.fullmatch(code) and len(code) >= 20:
        return code
    # body fallback
    body = str(await js(tab, "document.body ? (document.body.innerText||'') : ''") or "")
    # line after "Enter this code"
    m = re.search(
        r"enter this code[^\n]{0,80}\n+\s*([A-Za-z0-9_\-]{20,128})",
        body,
        re.I,
    )
    if m:
        return m.group(1).strip()
    for tok in _OAUTH_CODE_RE.findall(body):
        if len(tok) >= 24 and not re.match(
            r"^(Allow|Deny|Continue|Copy|Clipboard|Authorize)$", tok, re.I
        ):
            return tok
    return ""


async def wait_and_extract_oauth_code(tab: Any, timeout: float = 90.0) -> str:
    """
    After consent Allow:
      1) click any remaining Allow (Access other apps popup)
      2) wait for finish-code screen
      3) extract opaque code string OR callback URL
    """
    log.info("[oauth] wait finish-code / callback (up to %.0fs)", timeout)
    deadline = time.time() + timeout
    allow_extra = 0
    while time.time() < deadline:
        # callback URL (legacy redirect)
        u = await current_url(tab)
        if is_callback_url(u):
            log.info("[oauth] callback URL: %s", u[:120])
            return u
        found = scan_cdp_tabs_for_callback()
        if found:
            log.info("[oauth] callback via tab scan")
            return found

        page_text = str(
            await js(tab, "document.body ? (document.body.innerText||'').slice(0,2000) : ''")
            or ""
        )

        # popup: accounts.x.ai wants to Access other apps → Allow
        if re.search(
            r"access other apps|wants to access|requesting access|permission",
            page_text,
            re.I,
        ) or (re.search(r"\ballow\b", page_text, re.I) and "consent" in (u or "").lower()):
            if allow_extra < 5:
                clicked = await click_allow_buttons(tab)
                if clicked:
                    allow_extra += 1
                    log.info("[oauth] Allow popup/consent: %s", clicked)
                    await sleep(1.2)
                    continue

        # finish signing in with visible code
        if await page_shows_finish_code(tab) or re.search(
            r"enter this code|copy to clipboard", page_text, re.I
        ):
            code = await extract_oauth_finish_code(tab)
            if code:
                log.info("[oauth] finish code extracted len=%s", len(code))
                return code
            # try click Copy then read clipboard is unreliable — re-scan DOM
            await js(
                tab,
                """
                (() => {
                  const btn = [...document.querySelectorAll('button,[role=button]')]
                    .find(b => /copy/i.test((b.innerText||b.getAttribute('aria-label')||'')));
                  if (btn) btn.click();
                  return !!btn;
                })()
                """,
            )
            await sleep(0.5)
            code = await extract_oauth_finish_code(tab)
            if code:
                log.info("[oauth] finish code after Copy click len=%s", len(code))
                return code

        # still on consent without code UI → Allow again
        if "consent" in (u or "").lower() and re.search(r"\ballow\b", page_text, re.I):
            if allow_extra < 6:
                clicked = await click_allow_buttons(tab)
                if clicked:
                    allow_extra += 1
                    log.info("[oauth] Allow on consent: %s", clicked)
                    await sleep(1.5)
                    continue

        await sleep(0.8)
    return ""


def extract_code(url_or_code: str) -> str:
    s = (url_or_code or "").strip()
    if not s:
        return ""
    if "://" not in s and "code=" not in s:
        return s
    try:
        qs = parse_qs(urlparse(s).query)
        if qs.get("code"):
            return qs["code"][0]
    except Exception:
        pass
    m = re.search(r"[?&#]code=([^&\s]+)", s)
    return m.group(1) if m else s


# ---------------------------------------------------------------------------
# OAuth: EMAIL only (never Google)
# ---------------------------------------------------------------------------


async def force_email_signin_path(tab: Any) -> bool:
    """
    On xAI sign-in landing: click 'Sign in with email' / 'Continue with email'.
    Never click Google / Apple / X.
    """
    url = (await current_url(tab)).lower()
    # If already on Google — leave and we will reopen auth_url from caller
    if "accounts.google.com" in url or "google.com/o/oauth" in url:
        log.warning("[oauth] landed on Google — need email path instead")
        return False

    # already has email input visible
    has_email = bool(
        await js(
            tab,
            """
            (() => {
              const el = document.querySelector(
                'input[type=email], input[name=email], input[autocomplete=email], input[autocomplete=username]'
              );
              if (!el) return false;
              const r = el.getBoundingClientRect();
              return r.width > 2 && r.height > 2;
            })()
            """,
        )
    )
    if has_email:
        return True

    # Click email-only CTAs
    clicked = bool(
        await js(
            tab,
            """
            (() => {
              const wants = [
                'sign in with email', 'sign up with email', 'continue with email',
                'use email', 'log in with email', 'email', '邮箱'
              ];
              const socialRe = /with\\s+(x|google|apple|facebook|microsoft|github|twitter)/i;
              const nodes = [...document.querySelectorAll('button, a, [role=button], div[role=button]')];
              const labelOf = (n) => ((n.innerText||n.getAttribute('aria-label')||'')+'')
                .replace(/\\s+/g,' ').trim().toLowerCase();

              // pass1: exact email labels
              for (const n of nodes) {
                const t = labelOf(n);
                if (!t) continue;
                if (socialRe.test(t) && !t.includes('email')) continue;
                if (wants.some(w => t === w || t.includes(w))) {
                  // must mention email OR be pure "email"
                  if (t.includes('email') || t.includes('邮箱') || t.includes('e-mail')) {
                    n.click();
                    return t;
                  }
                }
              }
              // pass2: any button containing email and sign/continue/log
              for (const n of nodes) {
                const t = labelOf(n);
                if (!t || t.length > 60) continue;
                if (socialRe.test(t) && !t.includes('email')) continue;
                if ((/email|e-mail|邮箱/.test(t)) && /(sign|log|continue|use|đăng)/i.test(t)) {
                  n.click();
                  return t;
                }
              }
              return null;
            })()
            """,
        )
    )
    if clicked:
        log.info("[oauth] chose email path: %s", clicked)
        await sleep(1.2)
        return True

    # last: still has email field somewhere
    return bool(
        await js(
            tab,
            "!!document.querySelector('input[type=email], input[name=email]')",
        )
    )


async def cloudflare_blocking(tab: Any) -> bool:
    """True if Cloudflare challenge / turnstile still active — DO NOT click login yet."""
    data = await js(
        tab,
        """
        (() => {
          const t = ((document.body && document.body.innerText) || '').toLowerCase();
          const title = (document.title || '').toLowerCase();
          // classic CF interstitial
          if (title.includes('just a moment') || title.includes('attention required')) return true;
          if (t.includes('checking your browser') || t.includes('verify you are human')
              || t.includes('confirm you are human')) return true;
          if (t.includes('needs to review the security') || t.includes('enable javascript and cookies')) return true;
          // turnstile / cf widgets (incl. empty-src iframes inside .cf-turnstile)
          const widget = document.querySelector(
            '#challenge-running, #challenge-stage, .cf-turnstile, [data-sitekey], iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"]'
          );
          const tokEl = document.querySelector(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
          );
          const tok = tokEl ? (tokEl.value || '').trim() : '';
          if (widget && tok.length < 20) return true;
          if (widget) {
            // widget present — still block unless token filled
            if (tok.length < 20) return true;
          }
          const iframes = [...document.querySelectorAll('iframe')].map(f =>
            (f.src||'') + ' ' + (f.id||'') + ' ' + (f.title||'') + ' ' + (f.name||'')
          );
          if (iframes.some(s => /challenge|turnstile|cloudflare|cf-chl|cf-chl-widget/i.test(s))) {
            if (tok.length < 20) return true;
          }
          // overlay blocking inputs
          const email = document.querySelector('input[type=email], input[type=password], input[name=email]');
          if (email) {
            const r = email.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) {
              if (document.querySelector('iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"], .cf-turnstile'))
                return true;
            }
          }
          return false;
        })()
        """,
    )
    return bool(data)


async def wait_cloudflare_clear(tab: Any, timeout: float = 90.0) -> bool:
    """
    Wait until Cloudflare finishes before any login/fill.
    Also enables pydoll auto-solve when available.
    """
    log.info("[oauth] waiting Cloudflare clear (up to %.0fs) — do NOT login yet", timeout)
    try:
        if hasattr(tab, "enable_auto_solve_cloudflare_captcha"):
            await tab.enable_auto_solve_cloudflare_captcha()
            log.info("[oauth] enable_auto_solve_cloudflare_captcha ON")
    except Exception as e:
        log.debug("[oauth] auto-solve CF: %s", e)

    # settle after navigation
    await sleep(2.5)

    deadline = time.time() + timeout
    stable_ok = 0
    while time.time() < deadline:
        if await cloudflare_blocking(tab):
            stable_ok = 0
            log.info("[oauth] Cloudflare still active — waiting...")
            await sleep(2.0)
            continue
        # require form usable OR consent page, stable for 2 polls
        ready = bool(
            await js(
                tab,
                """
                (() => {
                  const t = ((document.body&&document.body.innerText)||'').toLowerCase();
                  // consent already
                  if (/\\ballow\\b|authorize|consent/.test(t) && document.querySelector('button')) return true;
                  const email = document.querySelector(
                    'input[type=email], input[name=email], input[autocomplete=email], input[autocomplete=username]'
                  );
                  const pw = document.querySelector('input[type=password]');
                  const el = email || pw;
                  if (!el) {
                    // email=true path may still be loading
                    return false;
                  }
                  const r = el.getBoundingClientRect();
                  return r.width > 5 && r.height > 5 && !el.disabled;
                })()
                """,
            )
        )
        if ready:
            stable_ok += 1
            if stable_ok >= 2:
                # extra buffer so CF cookies settle
                await sleep(1.5)
                if not await cloudflare_blocking(tab):
                    log.info("[oauth] Cloudflare clear — form ready")
                    return True
                stable_ok = 0
        else:
            stable_ok = 0
        await sleep(1.2)

    log.warning("[oauth] Cloudflare wait timeout — continuing cautiously")
    return not await cloudflare_blocking(tab)


async def _force_cf_checkbox(tab: Any, wait_sec: float = 40.0) -> bool:
    """
    Tick CF Turnstile checkbox. pydoll shadow-root path often fails on xAI —
    use main.click_turnstile_checkbox_robust (coord + pydoll multi-strategy).
    """
    try:
        from grokreg.browser.chrome import click_turnstile_checkbox_robust

        ok = await click_turnstile_checkbox_robust(
            tab, wait_sec=max(25.0, float(wait_sec)), reason="oauth"
        )
        if ok:
            log.info("[oauth] CF/Turnstile robust click OK")
            return True
        log.warning("[oauth] robust CF click returned False — check blocking")
    except Exception as e:
        log.warning("[oauth] robust CF import/run failed: %s — fallback pydoll", e)
        try:
            if hasattr(tab, "enable_auto_solve_cloudflare_captcha"):
                await tab.enable_auto_solve_cloudflare_captcha(
                    time_to_wait_captcha=wait_sec
                )
            if hasattr(tab, "_bypass_cloudflare"):
                await tab._bypass_cloudflare({}, time_to_wait_captcha=min(20.0, wait_sec))
        except Exception as e2:
            log.debug("[oauth] pydoll CF fallback: %s", e2)
    # final: wait until not blocking
    deadline = time.time() + min(30.0, wait_sec)
    while time.time() < deadline:
        if not await cloudflare_blocking(tab):
            log.info("[oauth] CF clear after wait")
            return True
        await sleep(1.2)
    return not await cloudflare_blocking(tab)


async def navigate_oauth_with_cf(tab: Any, url: str) -> None:
    """Navigate OAuth/sign-in URL and actively tick Cloudflare checkbox."""
    navigated = False
    wait_captcha = 28.0
    try:
        if hasattr(tab, "expect_and_bypass_cloudflare_captcha"):
            log.info("[oauth] navigate with expect_and_bypass_cloudflare_captcha (wait=%.0fs)", wait_captcha)
            try:
                async with tab.expect_and_bypass_cloudflare_captcha(
                    time_to_wait_captcha=wait_captcha
                ):
                    await tab.go_to(url)
                    navigated = True
            except TypeError:
                async with tab.expect_and_bypass_cloudflare_captcha():
                    await tab.go_to(url)
                    navigated = True
            await sleep(2)
    except Exception as e:
        log.warning("[oauth] CF bypass helper: %s — plain goto", e)
    if not navigated:
        await tab.go_to(url)
        await sleep(3)
    # Explicit checkbox tick — default 5s is too short, Turnstile loads late
    log.info("[oauth] force CF checkbox click...")
    ok = await _force_cf_checkbox(tab, wait_sec=wait_captcha)
    if not ok:
        log.warning("[oauth] CF may still block — wait_cloudflare_clear fallback")
        await wait_cloudflare_clear(tab, timeout=60)
    else:
        await sleep(random.uniform(1.0, 2.0))


async def grok_oauth_email_login(
    browser: Any,
    auth_url: str,
    email: str,
    password: str,
    timeout: float,
) -> str:
    """
    Open OAuth URL, wait Cloudflare, force Sign in with email, login, Allow, return callback.
    CRITICAL: never fill/login while Cloudflare challenge is active.
    """
    log.info("[oauth] open OAuth in sibling tab (same profile, no new Chrome) for %s", email)
    # ALWAYS sibling tab of existing browser — shares cookies with reg session
    tab = await open_sibling_tab(browser, "about:blank")
    await navigate_oauth_with_cf(tab, auth_url)
    deadline = time.time() + timeout
    email_path_ok = False
    email_filled = False
    password_filled = False
    login_clicked = False
    login_retries = 0
    max_login_retries = 8
    allow_clicked = False
    last_url = ""
    google_retries = 0

    async def _has_visible(selector: str) -> bool:
        return bool(
            await js(
                tab,
                f"""
                (() => {{
                  const el = document.querySelector({json.dumps(selector)});
                  if (!el) return false;
                  const r = el.getBoundingClientRect();
                  return r.width > 2 && r.height > 2 && !el.disabled;
                }})()
                """,
            )
        )

    async def _still_on_signin() -> bool:
        u = (await current_url(tab)).lower()
        if is_callback_url(u):
            return False
        if "consent" in u and "sign-in" not in u:
            return False
        return "sign-in" in u or "signin" in u or await _has_visible('input[type="password"]')

    async def _click_login_button() -> bool:
        """Click Login/Continue on the SAME password form — never social."""
        clicked = await click_continue_email_form(tab)
        if clicked:
            return True
        return bool(
            await js(
                tab,
                """
                (() => {
                  const bad = /with\\s+(x|google|apple|facebook|microsoft)|sign (in|up) with(?! email)/i;
                  const nodes = [...document.querySelectorAll('button, [role=button], input[type=submit]')];
                  const labelOf = (n) => ((n.innerText||n.value||n.getAttribute('aria-label')||'')+'')
                    .replace(/\\s+/g,' ').trim().toLowerCase();
                  for (const n of nodes) {
                    const t = labelOf(n);
                    if (!t || bad.test(t)) continue;
                    if (/^(log\\s*in|sign\\s*in|continue|next|submit)$/i.test(t) || t === 'login') {
                      n.click(); return t;
                    }
                  }
                  // password form submit
                  const pw = document.querySelector('input[type=password]');
                  if (pw) {
                    const form = pw.closest('form');
                    if (form) {
                      const btn = form.querySelector('button[type=submit], input[type=submit], button');
                      if (btn) { btn.click(); return 'form-submit'; }
                    }
                  }
                  return null;
                })()
                """,
            )
        )

    async def _ensure_password_value() -> bool:
        """Keep password filled on same form (do not abandon form)."""
        has = await js(
            tab,
            f"""
            (() => {{
              const el = document.querySelector('input[type=password]');
              if (!el) return 'no';
              if ((el.value||'').length >= 4) return 'ok';
              return 'empty';
            }})()
            """,
        )
        if has == "ok":
            return True
        if has == "no":
            return False
        return await fill_first(
            tab,
            ['input[type="password"]', 'input[name="password"]'],
            password,
            ["password"],
        )

    while time.time() < deadline:
        url = await current_url(tab)
        if url != last_url:
            log.info("[oauth] url=%s", url[:150])
            last_url = url
            # re-check CF on every navigation (esp. sign-in?email=true)
            if "accounts.x.ai" in url.lower() or "auth.x.ai" in url.lower():
                if await cloudflare_blocking(tab):
                    log.info("[oauth] CF detected after navigation — waiting before any input")
                    await wait_cloudflare_clear(tab, timeout=90)
                else:
                    # short settle even if no CF widget detected
                    await sleep(1.5)

        # CALLBACK
        if is_callback_url(url):
            log.info("[oauth] got callback")
            full = url
            try:
                if hasattr(tab, "close"):
                    await tab.close()
            except Exception:
                pass
            return full

        # HARD STOP: never interact while CF active
        if await cloudflare_blocking(tab):
            log.info("[oauth] blocked by Cloudflare — wait (no login click)")
            await wait_cloudflare_clear(tab, timeout=60)
            # if we already tried login too early, stay on same page and retry Login after CF
            if login_clicked and await _still_on_signin():
                log.info("[oauth] CF cleared after early Login — will retry Login on SAME form")
            continue

        # If Google — go back to auth_url and force email
        if "accounts.google.com" in url.lower() or (
            "google.com" in url.lower() and "signin" in url.lower()
        ):
            google_retries += 1
            log.warning("[oauth] Google redirect #%s — reopen auth URL + email path", google_retries)
            if google_retries > 3:
                raise RuntimeError("stuck on Google login — email path failed")
            await navigate_oauth_with_cf(tab, auth_url)
            email_path_ok = False
            email_filled = False
            password_filled = False
            login_clicked = False
            login_retries = 0
            continue

        # Already on email=true sign-in — email path ready
        if "email=true" in url.lower() or "email%3dtrue" in url.lower():
            email_path_ok = True

        # Step 0: email path CTA
        if not email_path_ok:
            email_path_ok = await force_email_signin_path(tab)
            if not email_path_ok:
                # maybe already logged in session → consent
                page_text = str(
                    await js(tab, "document.body?(document.body.innerText||'').slice(0,1500):''")
                    or ""
                )
                if re.search(r"allow|authorize|consent|permission|允许|授权", page_text, re.I):
                    email_path_ok = True  # skip login
                else:
                    await sleep(0.8)
                    continue
            else:
                # after clicking "sign in with email", CF may reappear
                await sleep(1.5)
                if await cloudflare_blocking(tab):
                    await wait_cloudflare_clear(tab, timeout=90)

        # Step 1: email field — only after CF clear
        if not email_filled:
            if await cloudflare_blocking(tab):
                await wait_cloudflare_clear(tab, timeout=60)
                continue
            has_email_inp = await _has_visible(
                'input[type=email], input[name=email], input[autocomplete=email], input[autocomplete=username]'
            )
            # also try single selector fallbacks
            if not has_email_inp:
                has_email_inp = await _has_visible('input[type="email"]')
            if has_email_inp:
                email_filled = await fill_first(
                    tab,
                    [
                        'input[type="email"]',
                        'input[name="email"]',
                        'input[autocomplete="email"]',
                        'input[autocomplete="username"]',
                    ],
                    email,
                    ["email"],
                )
                if email_filled:
                    log.info("[oauth] email filled (email path) — wait before Next")
                    await sleep(1.0)
                    if await cloudflare_blocking(tab):
                        log.info("[oauth] CF after email fill — wait before Next")
                        await wait_cloudflare_clear(tab, timeout=60)
                        continue
                    await click_continue_email_form(tab)
                    await sleep(2.0)
                    # CF often after Next
                    if await cloudflare_blocking(tab):
                        await wait_cloudflare_clear(tab, timeout=90)

        # Step 2: password on SAME form
        if not password_filled:
            if "google.com" in (await current_url(tab)).lower():
                continue
            if await cloudflare_blocking(tab):
                await wait_cloudflare_clear(tab, timeout=60)
                continue
            if await _has_visible('input[type="password"]'):
                password_filled = await _ensure_password_value()
                if password_filled:
                    log.info("[oauth] password filled on form")

        # Step 3: Login — if too fast / CF, WAIT then click Login AGAIN on same page
        if password_filled and not allow_clicked:
            if "google.com" in (await current_url(tab)).lower():
                continue

            # still on sign-in → need (re)click Login
            if await _still_on_signin():
                if await cloudflare_blocking(tab):
                    log.info("[oauth] CF active before Login — wait, stay on same form")
                    await wait_cloudflare_clear(tab, timeout=90)
                    continue

                # ensure password still there (CF refresh may clear)
                if not await _ensure_password_value():
                    log.warning("[oauth] password field missing — wait on same page")
                    await sleep(1.5)
                    continue

                if login_clicked and login_retries < max_login_retries:
                    # previous Login too early — wait a bit then retry SAME Login
                    login_retries += 1
                    wait_s = min(3.0 + login_retries * 1.5, 12.0)
                    log.info(
                        "[oauth] Login may have been too early — wait %.1fs then retry Login #%s (same form)",
                        wait_s,
                        login_retries,
                    )
                    await sleep(wait_s)
                    if await cloudflare_blocking(tab):
                        await wait_cloudflare_clear(tab, timeout=60)
                    if not await _still_on_signin():
                        # left sign-in during wait — good
                        continue
                    await _ensure_password_value()
                    log.info("[oauth] retry Login on SAME form (#%s)", login_retries)
                    await _click_login_button()
                    await sleep(2.5)
                    continue

                if not login_clicked:
                    await sleep(1.5)
                    if await cloudflare_blocking(tab):
                        log.info("[oauth] CF before first Login — wait (do NOT abandon form)")
                        await wait_cloudflare_clear(tab, timeout=90)
                        continue
                    log.info("[oauth] CF clear → click Login (first)")
                    # also press Enter on password (often more reliable than button)
                    await js(
                        tab,
                        """
                        (() => {
                          const pw = document.querySelector('input[type=password]');
                          if (!pw) return false;
                          pw.focus();
                          for (const t of ['keydown','keypress','keyup']) {
                            pw.dispatchEvent(new KeyboardEvent(t, {
                              key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true
                            }));
                          }
                          const form = pw.closest('form');
                          if (form && form.requestSubmit) try { form.requestSubmit(); } catch(e) {}
                          return true;
                        })()
                        """,
                    )
                    await _click_login_button()
                    login_clicked = True
                    await sleep(2.5)
                    # page error?
                    err = str(
                        await js(
                            tab,
                            "document.body?(document.body.innerText||'').slice(0,400):''",
                        )
                        or ""
                    )
                    if re.search(
                        r"incorrect|invalid|wrong password|couldn.?t find|try again",
                        err,
                        re.I,
                    ):
                        log.error("[oauth] login error text: %s", err[:160])
                        raise RuntimeError(f"oauth login rejected: {err[:120]}")
                    # if still on sign-in, next loop will retry Login after wait
                    if await _still_on_signin():
                        log.info("[oauth] still on sign-in after Login — will wait & retry same Login")
                    continue

                if login_retries >= max_login_retries:
                    log.error(
                        "[oauth] Login retry exhausted on same form (%s) — abort OAuth",
                        max_login_retries,
                    )
                    raise RuntimeError(
                        f"oauth sign-in stuck after {max_login_retries} Login retries for {email}"
                    )
            else:
                # left sign-in — progress (consent / callback)
                login_clicked = True

        # Step 3: Allow consent / Access other apps → then finish-code screen
        page_text = str(
            await js(tab, "document.body?(document.body.innerText||'').slice(0,2500):''") or ""
        )
        on_consent = "consent" in url.lower() or bool(
            re.search(r"allow|authorize|access other apps|requesting access", page_text, re.I)
        )
        on_finish_code = await page_shows_finish_code(tab) or bool(
            re.search(r"enter this code|copy to clipboard|finish signing in", page_text, re.I)
        )

        if on_finish_code:
            code = await extract_oauth_finish_code(tab)
            if code:
                log.info("[oauth] finish code OK len=%s prefix=%s...", len(code), code[:8])
                try:
                    if hasattr(tab, "close"):
                        await tab.close()
                except Exception:
                    pass
                return code

        if on_consent or (not allow_clicked and re.search(r"\ballow\b", page_text, re.I)):
            # anti-flag: pause before Allow (human read consent)
            await sleep(random.uniform(1.8, 4.2))
            clicked = await click_allow_buttons(tab)
            if clicked:
                allow_clicked = True
                log.info("[oauth] clicked Allow: %s", clicked)
                await sleep(random.uniform(1.5, 3.5))
                # primary path: wait for "Enter this code..." UI or callback URL
                code_or_url = await wait_and_extract_oauth_code(tab, timeout=90)
                if code_or_url:
                    log.info(
                        "[oauth] got code/url after Allow (len=%s)",
                        len(code_or_url),
                    )
                    try:
                        if hasattr(tab, "close"):
                            await tab.close()
                    except Exception:
                        pass
                    return code_or_url
                log.warning("[oauth] Allow done but no code yet — keep polling")

        # always try extract if finish UI partially loaded
        code = await extract_oauth_finish_code(tab)
        if code and len(code) >= 20:
            log.info("[oauth] opportunistic code extract len=%s", len(code))
            try:
                if hasattr(tab, "close"):
                    await tab.close()
            except Exception:
                pass
            return code

        # scan all tabs every loop (callback may land anywhere)
        found = scan_cdp_tabs_for_callback()
        if found:
            log.info("[oauth] got callback via CDP tab scan (loop)")
            try:
                if hasattr(tab, "close"):
                    await tab.close()
            except Exception:
                pass
            return found

        await sleep(0.7)

    # last chance
    code = await extract_oauth_finish_code(tab)
    if code:
        return code
    found = scan_cdp_tabs_for_callback()
    if found:
        log.info("[oauth] got callback via final CDP scan")
        return found
    # one more wait on current page
    code_or_url = await wait_and_extract_oauth_code(tab, timeout=30)
    if code_or_url:
        return code_or_url
    raise RuntimeError(f"oauth timeout for {email}")

async def paste_and_complete(tab: Any, callback_or_code: str) -> None:
    log.info("[sub2api] paste code + Complete Authorization")
    # anti-flag: short pause before pasting OAuth code
    await sleep(random.uniform(1.2, 3.0))
    ok = await fill_first(
        tab,
        ["textarea.input", "textarea.font-mono", "textarea"],
        callback_or_code,
        ["authorization", "auth code", "code", "callback", "授权", "验证"],
    )
    if not ok:
        ok = bool(
            await js(
                tab,
                f"""
                (() => {{
                  const areas = [...document.querySelectorAll('textarea')];
                  if (!areas.length) return false;
                  const el = areas[areas.length-1];
                  el.focus();
                  const d = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');
                  if (d && d.set) d.set.call(el, {json.dumps(callback_or_code)});
                  else el.value = {json.dumps(callback_or_code)};
                  el.dispatchEvent(new Event('input',{{bubbles:true}}));
                  el.dispatchEvent(new Event('change',{{bubbles:true}}));
                  return true;
                }})()
                """,
            )
        )
    if not ok:
        raise RuntimeError("cannot fill auth code")
    await sleep(0.4)
    if not await click_text_safe(tab, LABELS["complete_auth"]):
        ok = bool(
            await js(
                tab,
                """
                (() => {
                  const re = /complete|authorization|完成授权|ủy quyền/i;
                  const b = [...document.querySelectorAll('button')]
                    .find(x => re.test((x.innerText||'').trim()) && !x.disabled);
                  if (b) { b.click(); return true; }
                  return false;
                })()
                """,
            )
        )
        if not ok:
            raise RuntimeError("cannot click Complete Authorization")

    async def _done() -> bool:
        has = await js(tab, "!!document.querySelector('[data-tour=\"account-form-name\"]')")
        if not has:
            # also check textarea gone / modal closed
            ta = await js(tab, "!!document.querySelector('textarea.font-mono, textarea.input')")
            if not ta:
                return True
        err = await js(
            tab,
            """
            (() => {
              const el = document.querySelector('.text-red-600, .text-red-400');
              return el ? (el.innerText||'').trim().slice(0,200) : '';
            })()
            """,
        )
        if err and len(str(err)) > 8:
            raise RuntimeError(f"complete auth error: {err}")
        return False

    if not await wait_until(_done, 60, desc="account created"):
        raise RuntimeError("timeout after Complete Authorization")
    log.info("[sub2api] account created")


async def test_connection(tab: Any, account_name: str, model_name: str, timeout: float) -> tuple[bool, str]:
    log.info("[test] %s model=%s", account_name, model_name)
    await fill_first(
        tab,
        ['input[type="search"]', 'input[placeholder*="Search"]', 'input[placeholder*="搜索"]'],
        account_name,
        ["search"],
    )
    await sleep(1.0)

    res = await js(
        tab,
        f"""
        (() => {{
          const want = {json.dumps(account_name)}.toLowerCase();
          const rows = [...document.querySelectorAll('tr, [class*="card"]')];
          let row = null;
          for (const r of rows) {{
            const t = ((r.innerText||'')+'').replace(/\\s+/g,' ').trim();
            if (t.toLowerCase().includes(want) && t.length < 400) {{ row = r; break; }}
          }}
          if (!row) return 'row_not_found';
          const buttons = [...row.querySelectorAll('button, a')];
          for (const b of buttons) {{
            const tx = (b.innerText || b.getAttribute('title') || b.getAttribute('aria-label') || '');
            if (/test|测试|kiểm tra|连接/i.test(tx)) {{ b.click(); return 'clicked_test'; }}
          }}
          if (buttons.length) {{ buttons[buttons.length-1].click(); return 'opened_menu'; }}
          return 'no_action';
        }})()
        """,
    )
    log.info("[test] row: %s", res)
    await sleep(0.8)
    if res == "opened_menu":
        await click_text_safe(tab, LABELS["test_connection"])
        await sleep(1.0)
    elif res == "row_not_found":
        return False, f"row not found: {account_name}"

    await sleep(0.8)
    # open model select + pick Grok 4.5
    await js(
        tab,
        """
        (() => {
          const triggers = [...document.querySelectorAll('button, [role=combobox]')];
          for (const tr of triggers) {
            const t = (tr.innerText||'').toLowerCase();
            if (t.includes('model') || t.includes('模型') || t.includes('select') || t.includes('选择')) {
              tr.click();
            }
          }
          return true;
        })()
        """,
    )
    await sleep(0.5)
    await js(
        tab,
        f"""
        (() => {{
          const want = {json.dumps(model_name)}.toLowerCase();
          let best=null, bestScore=-1;
          for (const el of document.querySelectorAll('[role=option], li, button, div, span')) {{
            const t = ((el.innerText||'')+'').replace(/\\s+/g,' ').trim();
            if (!t || t.length > 80) continue;
            const low = t.toLowerCase();
            let s = -1;
            if (low === want) s = 100;
            else if (low.includes(want)) s = 80;
            else if (low.includes('grok') && low.includes('4.5')) s = 70;
            else if (low.includes('grok-4') || low.includes('grok 4')) s = 50;
            if (s > bestScore) {{ bestScore = s; best = el; }}
          }}
          if (best && bestScore >= 50) {{ best.click(); return best.innerText.trim(); }}
          return '';
        }})()
        """,
    )
    await sleep(0.4)
    if not await click_text_safe(tab, LABELS["start_test"]):
        await click_selector(tab, "button.bg-primary-500, button.btn-primary")

    deadline = time.time() + timeout
    while time.time() < deadline:
        data = await js(
            tab,
            """
            (() => {
              const term = document.querySelector('.bg-gray-900, .font-mono');
              const termText = term ? (term.innerText||'') : '';
              if (/test completed successfully|测试完成/i.test(termText))
                return JSON.stringify({ok:true, msg: termText.slice(-200)});
              if (/error:|failed|失败|HTTP error/i.test(termText) && termText.length > 20)
                return JSON.stringify({ok:false, msg: termText.slice(-200)});
              const green = document.querySelector('.text-green-400');
              if (green && /completed|success|完成/i.test(green.innerText||''))
                return JSON.stringify({ok:true, msg: (green.innerText||'').trim()});
              const red = document.querySelector('.text-red-400, .text-red-500');
              if (red && (red.innerText||'').trim().length > 5)
                return JSON.stringify({ok:false, msg: (red.innerText||'').trim().slice(0,200)});
              return '';
            })()
            """,
        )
        if data:
            try:
                obj = json.loads(data) if isinstance(data, str) else data
                if isinstance(obj, dict) and "ok" in obj:
                    await click_text_safe(tab, LABELS["close"])
                    return bool(obj["ok"]), str(obj.get("msg") or "")
            except Exception:
                pass
        await sleep(1.0)

    await click_text_safe(tab, LABELS["close"])
    return False, f"test timeout {timeout}s"


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def sub2api_cfg(config: dict[str, Any]) -> dict[str, Any]:
    """Merge nested config.sub2api + top-level keys."""
    nested = dict(config.get("sub2api") or {})
    # top-level overrides for convenience
    for k in (
        "sub2api_url",
        "sub2api_user",
        "sub2api_pass",
        "name_prefix",
        "start_number",
        "group",
        "model_test",
        "timeout_oauth_sec",
        "timeout_test_sec",
    ):
        if k in config and config[k] not in (None, ""):
            nested[k] = config[k]
    nested.setdefault("sub2api_url", "http://localhost:8080")
    nested.setdefault("name_prefix", "grok free")
    nested.setdefault("start_number", 1)
    nested.setdefault("group", "grok free")
    nested.setdefault("model_test", "Grok 4.5")
    nested.setdefault("timeout_oauth_sec", 180)
    nested.setdefault("timeout_test_sec", 120)
    nested.setdefault("enabled", True)
    return nested


async def open_sibling_tab(browser: Any, url: str = "about:blank") -> Any:
    """
    Open ONE new tab in the SAME browser process (same profile / cookies / RAM).
    Never starts a second Chrome profile.
    """
    if not hasattr(browser, "new_tab"):
        raise RuntimeError("browser has no new_tab — cannot open sibling tab")
    log.info("[browser] open sibling tab (same profile) → %s", url[:100] if url else "blank")
    try:
        t = await browser.new_tab(url)
    except TypeError:
        t = await browser.new_tab()
        if url and url != "about:blank":
            await t.go_to(url)
    if t is None:
        raise RuntimeError("new_tab returned None")
    return t


async def add_grok_via_sso_api(
    tab: Any,
    config: dict[str, Any],
    email: str,
    *,
    account_name: str | None = None,
    sso_cookie: str | None = None,
) -> Sub2APIResult:
    """
    Competitor-style path: capture xAI SSO cookie → POST Sub2API sso-to-oauth.
    No browser OAuth UI. Fast and reliable when SSO cookie is present.
    """
    cfg = sub2api_cfg(config)
    if cfg.get("enabled") is False:
        return Sub2APIResult(True, "", "skip", "sub2api disabled")

    name = account_name or next_account_name(cfg)
    stage = "sso_capture"
    try:
        sso = (sso_cookie or "").strip()
        if not sso:
            from grokreg.delivery.sso_capture import capture_sso_cookie

            sso = await capture_sso_cookie(tab, navigate_if_needed=True)
        if not sso:
            return Sub2APIResult(False, name, stage, "SSO cookie not found")

        stage = "sso_api"
        from grokreg.delivery.sub2api_client import Sub2APIError, export_sso_to_sub2api

        log.info(
            "[sub2api] SSO→API import name=%s email=%s sso_len=%s",
            name,
            email,
            len(sso),
        )
        # run sync HTTP in thread so we don't block the event loop for minutes
        result = await asyncio.to_thread(
            export_sso_to_sub2api,
            cfg,
            sso,
            email=email,
            name=name,
        )
        created_id = result.get("account_id") or ""
        created_name = str(result.get("name") or name)
        msg = f"sso_api ok id={created_id} search_name={created_name!r}"
        log.info(
            "[sub2api] SSO imported — Sub2API id=%s name=%s. Admin search is NAME-only; do not search email.",
            created_id,
            created_name,
        )
        return Sub2APIResult(True, created_name, "sso_api", msg)
    except Exception as e:
        log.warning("[sub2api] SSO API failed name=%s stage=%s: %s", name, stage, e)
        return Sub2APIResult(False, name, stage, str(e)[:200])


async def add_grok_via_browser_oauth(
    browser: Any,
    tab: Any,
    config: dict[str, Any],
    email: str,
    password: str,
    *,
    account_name: str | None = None,
) -> Sub2APIResult:
    """
    Classic path: Sub2API admin UI + Manual Authorization + xAI email OAuth.
    Uses EXISTING pydoll browser only (sibling tabs, same profile).
    """
    cfg = sub2api_cfg(config)
    if cfg.get("enabled") is False:
        return Sub2APIResult(True, "", "skip", "sub2api disabled")

    name = account_name or next_account_name(cfg)
    stage = "start"
    account_tab = tab
    admin_tab: Any = None
    try:
        base = (cfg.get("sub2api_url") or "http://localhost:8080").rstrip("/")

        stage = "admin_tab"
        log.info("[sub2api] open admin in sibling tab (same Chrome profile)")
        admin_tab = await open_sibling_tab(browser, f"{base}/admin/accounts")
        await sleep(1.5)

        stage = "login"
        await ensure_sub2api_login(admin_tab, cfg)

        stage = "create"
        await open_create_modal(admin_tab)
        await fill_step1(admin_tab, name, cfg.get("group") or "grok free")
        await choose_manual_auth(admin_tab)

        stage = "auth_url"
        auth_url = await generate_auth_url(admin_tab)

        stage = "oauth"
        callback = await grok_oauth_email_login(
            browser,
            auth_url,
            email,
            password,
            timeout=float(cfg.get("timeout_oauth_sec") or 180),
        )
        log.info("[oauth] code/callback received")

        stage = "complete"
        await sleep(random.uniform(1.0, 2.5))
        await paste_and_complete(admin_tab, callback)

        run_test = cfg.get("run_test")
        if run_test is None:
            run_test = False
        if not run_test:
            log.info("[sub2api] skip Test Connection (sub2api.run_test=false)")
            if cfg.get("refresh_usage_after_import", True):
                await asyncio.to_thread(_refresh_usage_by_name, cfg, name)
            return Sub2APIResult(True, name, "complete", "created (test skipped)")

        stage = "test"
        await admin_tab.go_to(f"{base}/admin/accounts")
        await sleep(random.uniform(1.2, 2.5))
        ok, msg = await test_connection(
            admin_tab,
            name,
            cfg.get("model_test") or "Grok 4.5",
            timeout=float(cfg.get("timeout_test_sec") or 45),
        )
        if not ok:
            return Sub2APIResult(False, name, stage, f"created but test failed: {msg}")
        return Sub2APIResult(True, name, stage, msg or "ok")

    except Exception as e:
        log.exception("[sub2api] browser OAuth failed stage=%s: %s", stage, e)
        try:
            if admin_tab is not None:
                await click_text_safe(admin_tab, LABELS["close"] + ["Cancel", "取消"])
                await js(
                    admin_tab,
                    "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))",
                )
            else:
                await click_text_safe(account_tab, LABELS["close"] + ["Cancel", "取消"])
                await js(
                    account_tab,
                    "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))",
                )
        except Exception:
            pass
        return Sub2APIResult(False, name, stage, str(e))


async def add_grok_to_sub2api(
    browser: Any,
    tab: Any,
    config: dict[str, Any],
    email: str,
    password: str,
    *,
    account_name: str | None = None,
    sso_cookie: str | None = None,
) -> Sub2APIResult:
    """
    Sub2API import after reg success — learn from grok-register-web:

      mode=sso_api   → SSO cookie + admin API only
      mode=browser_oauth → UI OAuth only (legacy)
      mode=auto (default) → SSO API first, then browser OAuth fallback

    On total failure: enqueue durable retry (SSO) so reg is not lost.
    Naming: grok free NNN + group from config (default ``grok free``).
    """
    cfg = sub2api_cfg(config)
    if cfg.get("enabled") is False:
        return Sub2APIResult(True, "", "skip", "sub2api disabled")

    # Reserve name once so SSO + OAuth fallback share the same number
    name = account_name or next_account_name(cfg)
    mode = str(cfg.get("mode") or "auto").strip().lower()
    if mode in ("api", "sso", "sso_api", "sso-api"):
        mode = "sso_api"
    elif mode in ("oauth", "browser", "ui", "browser_oauth"):
        mode = "browser_oauth"
    else:
        mode = "auto"

    sso = (sso_cookie or "").strip()
    if not sso and mode in ("auto", "sso_api"):
        try:
            from grokreg.delivery.sso_capture import capture_sso_cookie

            sso = await capture_sso_cookie(tab, navigate_if_needed=True)
        except Exception as e:
            log.warning("[sub2api] SSO capture error: %s", e)
            sso = ""

    result: Sub2APIResult | None = None

    # --- Path A: SSO → Sub2API API (competitor best practice) ---
    if mode in ("auto", "sso_api") and sso:
        log.info("[sub2api] mode=%s → try SSO API first name=%s", mode, name)
        result = await add_grok_via_sso_api(
            tab,
            config,
            email,
            account_name=name,
            sso_cookie=sso,
        )
        if result.ok:
            return result
        log.warning(
            "[sub2api] SSO API fail (%s) — %s",
            result.stage,
            (result.message or "")[:120],
        )
        if mode == "sso_api":
            # durable queue then return fail (caller may still mark reg success)
            _maybe_enqueue_delivery(cfg, email, password, sso, name, result.message)
            return result
    elif mode == "sso_api" and not sso:
        result = Sub2APIResult(False, name, "sso_capture", "SSO cookie not found")
        _maybe_enqueue_delivery(cfg, email, password, "", name, result.message)
        return result

    # --- Path B: browser OAuth fallback ---
    fallback = cfg.get("fallback_browser_oauth")
    if fallback is None:
        fallback = True
    if browser is not None and (mode == "browser_oauth" or (mode == "auto" and fallback)):
        log.info("[sub2api] mode=%s → browser OAuth name=%s", mode, name)
        # If SSO path already consumed the name counter, reuse same name
        result = await add_grok_via_browser_oauth(
            browser,
            tab,
            config,
            email,
            password,
            account_name=name,
        )
        if result.ok:
            return result
        # both paths failed — queue SSO for later if we have it
        _maybe_enqueue_delivery(
            cfg,
            email,
            password,
            sso,
            name,
            result.message if result else "unknown",
        )
        return result or Sub2APIResult(False, name, "oauth", "browser oauth failed")

    # auto without fallback and SSO failed
    if result is not None:
        _maybe_enqueue_delivery(cfg, email, password, sso, name, result.message)
        return result
    return Sub2APIResult(False, name, "skip", f"mode={mode} no path taken")


def _refresh_usage_by_name(cfg: dict[str, Any], name: str) -> None:
    """After browser-OAuth create, probe quota so admin shows usage instead of Cấm."""
    if not (name or "").strip():
        return
    try:
        from grokreg.delivery.sub2api_client import client_from_cfg

        client = client_from_cfg(cfg)
        acc = client.find_account_by_name(name)
        aid = 0
        if isinstance(acc, dict):
            try:
                aid = int(acc.get("id") or 0)
            except (TypeError, ValueError):
                aid = 0
        if aid <= 0:
            log.warning("[sub2api] usage refresh: account %r not found", name)
            return
        budget = 20
        try:
            nested = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
            budget = int(cfg.get("usage_refresh_sec") or nested.get("usage_refresh_sec") or 20)
        except (TypeError, ValueError):
            budget = 20
        usage = client.ensure_usage_visible(aid, budget_sec=budget)
        log.info(
            "[sub2api] usage refresh name=%s id=%s ok=%s code=%s",
            name,
            aid,
            usage.get("ok"),
            usage.get("status_code"),
        )
    except Exception as e:
        log.warning("[sub2api] usage refresh failed name=%s: %s", name, e)


def _maybe_enqueue_delivery(
    cfg: dict[str, Any],
    email: str,
    password: str,
    sso: str,
    name: str,
    error: str = "",
) -> None:
    """Queue SSO for durable retry when upload failed but reg succeeded."""
    if cfg.get("durable_retry") is False:
        return
    if not (sso or "").strip():
        log.info("[sub2api] no SSO to enqueue for durable retry (%s)", email)
        return
    try:
        from grokreg.delivery.delivery_retry import enqueue_sub2api

        enqueue_sub2api(
            email=email,
            sso=sso,
            name=name,
            password=password,
            group=str(cfg.get("group") or "grok free"),
            sub_cfg=cfg,
            error=error or "",
        )
    except Exception as e:
        log.warning("[sub2api] enqueue durable failed: %s", e)
