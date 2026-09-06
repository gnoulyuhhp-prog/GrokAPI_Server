"""Mint a Castle request token for HTTP CreateEmailValidationCode.

xAI signup (`improvedCastleFlow`) sends protobuf field 3 ≈ 17KB starting
with ``IBYI…``. Without it, CreateEmail returns 200 but no mail is issued.

The public CDN script ``cdn.castle.io/v2/castle.js`` cannot mint tokens
(docs: tracking-only). Tokens come from the NPM build
``@castleio/castle-js/dist/castle.browser.js``. Off-screen Chrome also
reports ``visibilityState=hidden``, which makes xAI's own warmup time out
— we spoof visibility before mint.

If SDK mint fails, fall back to the page's native email form so their
bundled Castle flow sends CreateEmail itself.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grokreg.core.runtime import ROOT, log
import grokreg.core.style_log as slog

SIGNUP_URL = "https://accounts.x.ai/sign-up"
CASTLE_PK = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
_VENDOR = Path(__file__).resolve().parent / "vendor" / "castle.browser.js"
_CACHE = ROOT / "data" / "castle.browser.js"
_SDK_URLS = (
    "https://unpkg.com/@castleio/castle-js@2.8.5/dist/castle.browser.js",
    "https://cdn.jsdelivr.net/npm/@castleio/castle-js@2.8.5/dist/castle.browser.js",
)


@dataclass
class MintResult:
    token: str = ""
    email_sent: bool = False
    method: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.token) or self.email_sent


_VIS_SPOOF_JS = r"""
(() => {
  try {
    Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
    Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
    if (typeof document.hasFocus === 'function') {
      document.hasFocus = () => true;
    }
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  } catch (e) {}
  return document.visibilityState;
})()
"""

_HOOK_JS = r"""
(() => {
  if (window.__gtCastleHook) return 'already';
  window.__gtCastleHook = true;
  window.__gtCastleCap = {token:'', emailSent:false, status:0, url:''};
  const takeTok = (s) => {
    if (!s) return '';
    const str = String(s);
    const i = str.indexOf('IBYI');
    if (i >= 0) return str.slice(i);
    return '';
  };
  const fromBytes = (buf) => {
    try {
      const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      if (!u8 || !u8.length) return '';
      let i = (u8.length > 5 && u8[0] === 0) ? 5 : 0;
      while (i < u8.length) {
        const key = u8[i++];
        const field = key >> 3;
        const wt = key & 7;
        if (wt === 2) {
          let len = 0, shift = 0, b;
          do {
            if (i >= u8.length) return '';
            b = u8[i++];
            len |= (b & 0x7f) << shift;
            shift += 7;
          } while (b & 0x80);
          if (field === 3 && len > 40 && i + len <= u8.length) {
            const s = new TextDecoder('utf-8', {fatal:false}).decode(u8.subarray(i, i + len));
            if (s.indexOf('IBYI') === 0 || s.length > 200) return s;
          }
          i += len;
        } else if (wt === 0) {
          let b;
          do { if (i >= u8.length) return ''; b = u8[i++]; } while (b & 0x80);
        } else {
          break;
        }
      }
      const asText = new TextDecoder('latin1').decode(u8);
      return takeTok(asText);
    } catch (e) { return ''; }
  };
  const note = (url, body, status) => {
    const u = String(url || '');
    const hit = /CreateEmailValidationCode/i.test(u);
    let tok = '';
    if (typeof body === 'string') tok = takeTok(body);
    else if (body) tok = fromBytes(body);
    if (tok && tok.length > (window.__gtCastleCap.token || '').length) {
      window.__gtCastleCap.token = tok;
    }
    if (hit) {
      window.__gtCastleCap.url = u.slice(0, 180);
      if (status) window.__gtCastleCap.status = status;
      if (status && status >= 200 && status < 300) window.__gtCastleCap.emailSent = true;
    }
  };
  const ofetch = window.fetch;
  if (ofetch) {
    window.fetch = async function(...args) {
      const req = args[0];
      const url = (req && req.url) ? req.url : req;
      let body = null;
      try {
        if (req && typeof req.clone === 'function') {
          const c = req.clone();
          body = await c.arrayBuffer();
        } else if (args[1] && args[1].body) {
          body = args[1].body;
        }
      } catch (e) {}
      const res = await ofetch.apply(this, args);
      try { note(url, body, res.status); } catch (e) {}
      return res;
    };
  }
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__gtUrl = url;
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const url = this.__gtUrl;
    this.addEventListener('load', function() {
      try { note(url, body, this.status); } catch (e) {}
    });
    return XS.apply(this, arguments);
  };
  return 'ok';
})()
"""

_START_MINT_JS = r"""
(() => {
  if (window.__gtCastle && window.__gtCastle.pending) return 'started';
  window.__gtCastle = {ok:false, pending:true, token:'', method:null, error:null};
  const take = (t) => {
    if (!t) return '';
    if (typeof t === 'string') return t;
    return String(t.token || t.requestToken || t.request_token || '');
  };
  const findReactCastle = () => {
    const nodes = [document.documentElement, document.body];
    try { nodes.push(...document.querySelectorAll('div,main,section,form,span')); } catch (e) {}
    const seen = new Set();
    for (const el of nodes) {
      if (!el || seen.has(el)) continue;
      seen.add(el);
      const keys = Object.keys(el);
      for (const k of keys) {
        if (k.indexOf('react') < 0 && k.indexOf('React') < 0) continue;
        let f = el[k];
        if (f && f.current) f = f.current;
        for (let i = 0; i < 200 && f; i++, f = f.return || f._debugOwner) {
          const props = f.memoizedProps || f.pendingProps;
          const val = props && props.value;
          if (val && typeof val.createRequestToken === 'function') return val;
          const deps = f.dependencies;
          let node = deps && (deps.firstContext || deps);
          let guard = 0;
          while (node && guard++ < 30) {
            const v = node.memoizedValue;
            if (v && typeof v.createRequestToken === 'function') return v;
            node = node.next;
          }
        }
      }
    }
    return null;
  };
  const findApi = () => {
    const cands = [
      [window.__GtCastle, '__GtCastle'],
      [window.Castle, 'Castle'],
      [window._castle, '_castle'],
      [window.castle, 'castle'],
    ];
    try {
      for (const k of Object.keys(window)) {
        const v = window[k];
        if (v && typeof v === 'object' && (
          typeof v.createRequestToken === 'function' || typeof v.configure === 'function'
        )) cands.push([v, k]);
      }
    } catch (e) {}
    for (const [c, tag] of cands) {
      if (!c) continue;
      if (typeof c.createRequestToken === 'function') return {api:c, tag};
      if (typeof c.configure === 'function') {
        try {
          const inst = c.configure({pk: window.__gtCastlePk || 'pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz'});
          if (inst && typeof inst.createRequestToken === 'function') {
            return {api:inst, tag: tag + '.configure'};
          }
        } catch (e) {}
      }
    }
    return null;
  };
  const mintOnce = async (api, tag, opts) => {
    let call = api.createRequestToken;
    let used = tag;
    try {
      const t = await Promise.race([
        Promise.resolve(opts ? call.call(api, opts) : call.call(api)),
        new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 8000))
      ]);
      return {token: take(t), method: used};
    } catch (e) {
      return {token:'', method: used, error: String(e).slice(0, 160)};
    }
  };
  (async () => {
    const out = window.__gtCastle;
    try {
      const hid = document.querySelector(
        'input[name*="castle"], input[name*="request_token"], input[id*="castle"]'
      );
      if (hid && hid.value && hid.value.length > 40) {
        out.ok = true; out.token = hid.value; out.method = 'hidden_input'; out.pending = false;
        return;
      }
      const cap = window.__gtCastleCap || {};
      if (cap.token && String(cap.token).length > 40) {
        out.ok = true; out.token = cap.token; out.method = 'hook'; out.pending = false;
        return;
      }
      const html = document.documentElement ? document.documentElement.innerHTML : '';
      const pkM = html.match(/"castlePk"\s*:\s*"(pk_[^"]+)"/);
      const pk = pkM ? pkM[1] : 'pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz';
      window.__gtCastlePk = pk;

      let page = findReactCastle();
      for (let i = 0; !page && i < 15; i++) {
        await new Promise(r => setTimeout(r, 200));
        page = findReactCastle();
      }
      out.pageCastle = !!page;
      if (page && typeof page.createRequestToken === 'function') {
        const email = window.__gtCastleEmail || '';
        const got = await mintOnce(page, 'page_useCastle', {
          method: 'email_password', flow: 'signup', email: email
        });
        if (got.token.length > 40) {
          out.ok = true; out.token = got.token; out.method = got.method; out.pending = false;
          return;
        }
        out.error = got.error || 'page_empty';
      }

      const deadline = Date.now() + 4000;
      let found = findApi();
      while (!found && Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 200));
        found = findApi();
      }
      if (!found) {
        out.error = (out.error ? out.error + ' | ' : '') + 'no_mint_api';
        out.pending = false;
        return;
      }
      let api = found.api;
      try {
        if (typeof api.configure === 'function') {
          const inst = api.configure({ pk: pk });
          if (inst && typeof inst.createRequestToken === 'function') api = inst;
        }
      } catch (e) {
        out.error = 'configure: ' + String(e).slice(0, 120);
      }
      // Let the fresh SDK collect device signals (xAI warmup is ~1s, mint timeout 4s).
      for (let i = 0; i < 8; i++) {
        document.dispatchEvent(new MouseEvent('mousemove', {
          bubbles:true, clientX: 90 + i * 40, clientY: 140 + (i % 3) * 30
        }));
        await new Promise(r => setTimeout(r, 350));
      }
      let best = '';
      let bestMethod = found.tag;
      for (let n = 0; n < 2; n++) {
        const got = await mintOnce(api, found.tag + '+cfg', null);
        if (got.token.length > best.length) {
          best = got.token; bestMethod = got.method + '#' + (n + 1);
        }
        if (got.error) out.error = got.error;
        if (best.length > 8000) break;
        await new Promise(r => setTimeout(r, 800));
      }
      if (best.length > 40) {
        out.ok = true; out.token = best; out.method = bestMethod; out.pending = false;
        return;
      }
      out.error = out.error || ('empty_token method=' + found.tag);
    } catch (e) {
      out.error = String(e).slice(0, 180);
    }
    out.pending = false;
  })();
  return 'started';
})()
"""

_READ_MINT_JS = r"""
(() => {
  const o = window.__gtCastle || {};
  const cap = window.__gtCastleCap || {};
  const token = o.token || cap.token || '';
  return JSON.stringify({
    ok: !!(o.ok && token) || !!(cap.token && String(cap.token).length > 40),
    pending: !!o.pending,
    token: token,
    method: o.method || (cap.token ? 'hook' : null),
    pageCastle: !!o.pageCastle,
    error: o.error || null,
    tokenLen: (token || '').length,
    emailSent: !!cap.emailSent,
    createStatus: cap.status || 0
  });
})()
"""


def _load_castle_sdk() -> str:
    for p in (_VENDOR, _CACHE):
        try:
            if p.is_file() and p.stat().st_size > 50_000:
                return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    import urllib.request

    last = ""
    for url in _SDK_URLS:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            if len(data) < 50_000:
                last = f"short:{len(data)}"
                continue
            text = data.decode("utf-8", errors="replace")
            try:
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_text(text, encoding="utf-8")
            except Exception:
                pass
            return text
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"castle.browser.js missing ({last})")


def _apply_cookies(session: Any, cookies: list[dict[str, Any]]) -> int:
    n = 0
    for c in cookies:
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name or not value:
            continue
        domain = str(c.get("domain") or ".x.ai")
        path = str(c.get("path") or "/")
        try:
            session.cookies.set(name, value, domain=domain, path=path)
            n += 1
        except Exception:
            try:
                session.cookies.set(name, value)
                n += 1
            except Exception:
                pass
    return n


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            got = json.loads(raw)
            return got if isinstance(got, dict) else {}
        except Exception:
            return {}
    return {}


async def _eval_raw(tab: Any, script: str, *, timeout_ms: int = 15000) -> Any:
    try:
        from pydoll.commands.runtime_commands import RuntimeCommands

        cmd = RuntimeCommands.evaluate(
            expression=script,
            return_by_value=False,
            allow_unsafe_eval_blocked_by_csp=True,
            timeout=float(timeout_ms),
            user_gesture=True,
        )
        return await tab._execute_command(cmd)
    except Exception as e:
        log.warning("[castle] Runtime.evaluate failed: %s", e)
    for method_name in ("execute_script", "evaluate"):
        if not hasattr(tab, method_name):
            continue
        fn = getattr(tab, method_name)
        try:
            try:
                return await fn(
                    script,
                    return_by_value=False,
                    allow_unsafe_eval_blocked_by_csp=True,
                    timeout=float(timeout_ms),
                )
            except TypeError:
                return await fn(script)
        except Exception as e:
            log.warning("[castle] %s failed: %s", method_name, e)
    return None


def _extract_iby_token(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, bytes):
        raw = blob
    else:
        s = str(blob)
        idx = s.find("IBYI")
        if idx >= 0:
            return s[idx:]
        raw = s.encode("latin-1", errors="replace")
    idx = raw.find(b"IBYI")
    if idx < 0:
        return ""
    return raw[idx:].decode("utf-8", errors="replace")


async def _install_network_token_hook(tab: Any, sink: dict[str, Any]) -> None:
    try:
        from pydoll.commands.network_commands import NetworkCommands

        await tab._execute_command(NetworkCommands.enable())
    except Exception:
        try:
            await tab._execute_command({"method": "Network.enable", "params": {}})
        except Exception as e:
            log.debug("[castle] Network.enable: %s", e)
            return

    async def _on_request(event: dict) -> None:
        try:
            p = event.get("params") or {}
            req = p.get("request") or {}
            url = str(req.get("url") or "")
            if "CreateEmailValidationCode" not in url:
                return
            pd = req.get("postData") or ""
            rid = str(p.get("requestId") or "")
            if rid:
                sink["request_id"] = rid
            tok = _extract_iby_token(pd)
            if tok and len(tok) > len(str(sink.get("token") or "")):
                sink["token"] = tok
            sink["url"] = url[:180]
            sink["post_len"] = len(pd) if isinstance(pd, str) else 0
            want = str(sink.get("want_email") or "")
            blob = pd if isinstance(pd, str) else ""
            if want and want in blob:
                sink["saw_create"] = True
                sink["post_email"] = want
            elif not want:
                sink["saw_create"] = True
        except Exception:
            pass

    try:
        await tab.on("Network.requestWillBeSent", _on_request)
    except Exception as e:
        log.debug("[castle] net hook: %s", e)


async def _install_visibility_spoof(tab: Any) -> None:
    src = (
        "Object.defineProperty(Document.prototype,'hidden',{get:()=>false,configurable:true});"
        "Object.defineProperty(Document.prototype,'visibilityState',{get:()=>'visible',configurable:true});"
        "Document.prototype.hasFocus=function(){return true};"
    )
    try:
        await tab._execute_command(
            {
                "method": "Page.addScriptToEvaluateOnNewDocument",
                "params": {"source": src},
            }
        )
    except Exception:
        try:
            from pydoll.commands.page_commands import PageCommands

            cmd = getattr(PageCommands, "add_script_to_evaluate_on_new_document", None)
            if cmd:
                await tab._execute_command(cmd(src))
        except Exception as e:
            log.debug("[castle] addScript spoof: %s", e)


async def _inject_sdk(tab: Any) -> dict[str, Any]:
    from grokreg.browser.jsutil import _exec_js

    await _exec_js(
        tab,
        """(() => { window.__gtPrevCastle = window.Castle; return 1; })()""",
    )
    src = _load_castle_sdk()
    raw = await _eval_raw(tab, src, timeout_ms=20000)
    await _exec_js(
        tab,
        """(() => {
          if (window.Castle && typeof window.Castle.configure === 'function') {
            window.__GtCastle = window.Castle;
          }
          if (window.__gtPrevCastle !== undefined) window.Castle = window.__gtPrevCastle;
          return !!(window.__GtCastle && window.__GtCastle.configure);
        })()""",
    )
    probe = await _exec_js(
        tab,
        """(() => {
          const hits = [];
          try {
            for (const k of Object.keys(window)) {
              const v = window[k];
              if (v && typeof v === 'object' && (
                typeof v.createRequestToken === 'function' || typeof v.configure === 'function'
              )) hits.push(k);
            }
          } catch (e) {}
          const c = window.Castle;
          return JSON.stringify({
            hasCastle: !!c,
            keys: c && typeof c === 'object' ? Object.keys(c).slice(0, 16) : [],
            hasCreate: !!(c && typeof c.createRequestToken === 'function'),
            hasCfg: !!(c && typeof c.configure === 'function'),
            hits: hits.slice(0, 12)
          });
        })()""",
    )
    out = _as_dict(probe)
    if raw is not None and not out:
        out["eval"] = type(raw).__name__
    return out


_SEND_CREATE_JS = r"""
(email, token) => {
  const varint = (n) => {
    const out = [];
    n = n >>> 0;
    while (n > 0x7f) { out.push((n & 0x7f) | 0x80); n = n >>> 7; }
    out.push(n);
    return out;
  };
  const protoStr = (field, s) => {
    const raw = new TextEncoder().encode(String(s || ''));
    const head = [(field << 3) | 2, ...varint(raw.length)];
    const out = new Uint8Array(head.length + raw.length);
    out.set(head, 0);
    out.set(raw, head.length);
    return out;
  };
  const f1 = protoStr(1, email);
  const f3 = protoStr(3, token);
  const total = f1.length + f3.length;
  const frame = new Uint8Array(5 + total);
  frame[0] = 0;
  frame[1] = (total >>> 24) & 0xff;
  frame[2] = (total >>> 16) & 0xff;
  frame[3] = (total >>> 8) & 0xff;
  frame[4] = total & 0xff;
  frame.set(f1, 5);
  frame.set(f3, 5 + f1.length);
  window.__gtCreate = {pending:true, status:0, grpc:'', err:null};
  (async () => {
    try {
      const res = await fetch('https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode', {
        method: 'POST',
        headers: {
          'content-type': 'application/grpc-web+proto',
          'x-grpc-web': '1',
          'x-user-agent': 'connect-es/2.1.1',
          'origin': 'https://accounts.x.ai',
          'referer': 'https://accounts.x.ai/sign-up',
        },
        credentials: 'include',
        body: frame,
      });
      window.__gtCreate.status = res.status;
      window.__gtCreate.grpc = res.headers.get('grpc-status') || res.headers.get('grpc-message') || '';
      window.__gtCreate.ok = res.status >= 200 && res.status < 300;
    } catch (e) {
      window.__gtCreate.err = String(e).slice(0, 160);
      window.__gtCreate.ok = false;
    }
    window.__gtCreate.pending = false;
  })();
  return 'started';
}
"""


async def _browser_send_create_email(tab: Any, email: str, token: str) -> bool:
    from grokreg.browser.jsutil import _exec_js

    js = f"({_SEND_CREATE_JS})({json.dumps(email)}, {json.dumps(token)})"
    await _exec_js(tab, js)
    raw: dict[str, Any] = {}
    for _ in range(20):
        raw = _as_dict(
            await _exec_js(
                tab,
                "(() => JSON.stringify(window.__gtCreate || {}))()",
            )
        )
        if raw and raw.get("pending") is False:
            break
        await asyncio.sleep(0.25)
    ok = bool(raw.get("ok")) and int(raw.get("status") or 0) < 300
    slog.api_ok(
        f"CreateEmail via Chrome status={raw.get('status')} grpc={raw.get('grpc')} "
        f"err={raw.get('err')}"
    )
    return ok


async def _wiggle(tab: Any) -> None:
    from grokreg.browser.jsutil import _exec_js

    await _exec_js(
        tab,
        """(() => {
          for (let i = 0; i < 6; i++) {
            const x = 80 + i * 70, y = 120 + (i % 3) * 40;
            document.dispatchEvent(new MouseEvent('mousemove', {
              bubbles:true, clientX:x, clientY:y, view:window
            }));
            document.dispatchEvent(new PointerEvent('pointermove', {
              bubbles:true, clientX:x, clientY:y
            }));
          }
          window.scrollBy(0, 120);
          window.scrollBy(0, -80);
          return 1;
        })()""",
    )


_NATIVE_SEND_JS = r"""
(email) => {
  const wantEmail = String(email || '');
  window.__gtNative = {step:'start', error:null};
  const emailSel = 'input[type="email"], input[name="email"], input[autocomplete="email"], input[inputmode="email"]';
  const labelOf = (n) => (n.innerText || n.value || n.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim();
  const clickEmailEntry = () => {
    if (document.querySelector(emailSel)) return 'already';
    const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
    for (const n of nodes) {
      const t = labelOf(n).toLowerCase();
      if (!t) continue;
      if (t === 'sign up with email' || t === 'continue with email' || t === 'use email') {
        n.click(); return t;
      }
    }
    for (const n of nodes) {
      const t = labelOf(n).toLowerCase();
      if (t.includes('email') && (t.includes('sign') || t.includes('continue'))) {
        n.click(); return t;
      }
    }
    return '';
  };
  const fill = () => {
    const el = document.querySelector(emailSel);
    if (!el) return false;
    el.focus();
    try {
      const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
      if (desc && desc.set) desc.set.call(el, wantEmail);
      else el.value = wantEmail;
    } catch (e) { el.value = wantEmail; }
    el.dispatchEvent(new InputEvent('input', {bubbles:true, composed:true, inputType:'insertText', data:wantEmail}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    return (el.value || '') === wantEmail;
  };
  const clickGo = () => {
    const email = document.querySelector(emailSel);
    const roots = [];
    if (email) {
      if (email.closest('form')) roots.push(email.closest('form'));
      let p = email.parentElement;
      for (let i = 0; i < 6 && p; i++, p = p.parentElement) roots.push(p);
    }
    roots.push(document);
    const wantRe = /^(continue|next|submit|verify|sign up|xác nhận|tiếp tục)$/i;
    for (const root of roots) {
      for (const n of root.querySelectorAll('button, [role="button"], input[type="submit"]')) {
        const t = labelOf(n);
        if (!t || t.length > 24) continue;
        if (wantRe.test(t)) { n.click(); return t; }
      }
    }
    return '';
  };
  (async () => {
    try {
      const entry = clickEmailEntry();
      window.__gtNative.entry = entry;
      for (let i = 0; i < 20 && !document.querySelector(emailSel); i++) {
        await new Promise(r => setTimeout(r, 250));
      }
      if (!document.querySelector(emailSel)) {
        window.__gtNative.error = 'no_email_input';
        window.__gtNative.step = 'fail';
        return;
      }
      const filled = fill();
      window.__gtNative.filled = filled;
      await new Promise(r => setTimeout(r, 250));
      fill();
      const go = clickGo();
      window.__gtNative.go = go;
      window.__gtNative.step = go ? 'clicked' : 'no_continue';
    } catch (e) {
      window.__gtNative.error = String(e).slice(0, 160);
      window.__gtNative.step = 'fail';
    }
  })();
  return 'started';
}
"""


async def _pull_full_post(tab: Any, sink: dict[str, Any]) -> None:
    rid = str(sink.get("request_id") or "")
    if not rid:
        return
    try:
        raw = await tab._execute_command(
            {
                "method": "Network.getRequestPostData",
                "params": {"requestId": rid},
            }
        )
        body = ""
        if isinstance(raw, dict):
            body = str((raw.get("result") or raw).get("postData") or "")
        tok = _extract_iby_token(body)
        if tok and len(tok) > len(str(sink.get("token") or "")):
            sink["token"] = tok
        sink["post_full_len"] = len(body)
        want = str(sink.get("want_email") or "")
        if want and want in body:
            sink["saw_create"] = True
            sink["post_email"] = want
    except Exception as e:
        log.debug("[castle] getRequestPostData: %s", e)


async def _native_create_email(
    tab: Any, config: dict[str, Any], email: str, sink: dict[str, Any] | None = None
) -> MintResult:
    from grokreg.browser.chrome import detect_page_step
    from grokreg.browser.page_flow import (
        click_sign_up_with_email,
        detect_page_error,
        prepare_and_submit_email,
    )

    if sink is not None:
        sink["want_email"] = email
    await click_sign_up_with_email(tab)
    clicked = await prepare_and_submit_email(tab, config, email)
    slog.api_ok(f"Castle submit clicked={clicked}")
    from grokreg.browser.jsutil import _exec_js

    step = "unknown"
    err = None
    for _ in range(28):
        if sink:
            await _pull_full_post(tab, sink)
        try:
            step = await detect_page_step(tab)
        except Exception:
            step = "unknown"
        try:
            err = await detect_page_error(tab)
        except Exception:
            err = None
        probe = _as_dict(
            await _exec_js(
                tab,
                """(() => JSON.stringify({
                  hasCode: !!document.querySelector(
                    'input[name="code"], input[autocomplete="one-time-code"],
                    input[inputmode="numeric"], input[name="emailValidationCode"]'
                  ),
                  hasEmail: !!document.querySelector(
                    'input[type="email"], input[name="email"]'
                  ),
                  emailed: /emailed a one time security code/i.test(
                    (document.body && document.body.innerText) || ''
                  )
                }))()""",
            )
        )
        has_code = bool(probe.get("hasCode") or probe.get("emailed"))
        if has_code or step == "otp":
            tok = str((sink or {}).get("token") or "").strip()
            slog.api_ok(
                f"Castle OTP input token_len={len(tok)} post="
                f"{(sink or {}).get('post_full_len') or (sink or {}).get('post_len')}"
            )
            return MintResult(token=tok, email_sent=True, method="otp_page")
        if err:
            slog.api_err(f"Castle page error after submit: {err} step={step}")
            return MintResult(
                token=str((sink or {}).get("token") or ""),
                email_sent=False,
                method="page_error",
                error=str(err)[:160],
            )
        await asyncio.sleep(0.6)
    snap = _as_dict(
        await _exec_js(
            tab,
            """(() => JSON.stringify({
              href: location.href,
              hasEmail: !!document.querySelector('input[type="email"], input[name="email"]'),
              hasCode: !!document.querySelector('input[name="code"], input[autocomplete="one-time-code"]'),
              snip: ((document.body && document.body.innerText) || '').replace(/\\s+/g,' ').trim().slice(0, 220)
            }))()""",
        )
    )
    try:
        from grokreg.browser.network_castle import read_xai_fetch_sniffer

        sniff = await read_xai_fetch_sniffer(tab)
    except Exception:
        sniff = {}
    tok = str((sink or {}).get("token") or "").strip()
    if snap.get("hasCode") or "one time security code" in str(snap.get("snip") or "").lower():
        slog.api_ok(f"Castle OTP page (late) snap={snap.get('snip')}")
        return MintResult(token=tok, email_sent=True, method="otp_page_late")
    slog.api_err(
        f"Castle no OTP input step={step} saw_create={(sink or {}).get('saw_create')} "
        f"post={(sink or {}).get('post_full_len') or (sink or {}).get('post_len')} "
        f"tok={len(tok)} snap={snap} sniff={(sniff or {}).get('last')}"
    )
    return MintResult(
        token=tok,
        email_sent=False,
        method="native_page_flow",
        error=f"stuck_{step}",
    )


async def _mint_async(
    config: dict[str, Any],
    session: Any | None = None,
    email: str = "",
) -> MintResult:
    from grokreg.browser.chrome import (
        chrome_debug_port,
        close_browser_handle,
        open_or_attach_browser,
        probe_cdp_ws,
    )
    from grokreg.browser.jsutil import _exec_js
    from grokreg.delivery.sso_capture import _cdp_get_cookies

    handle = None
    result = MintResult()
    try:
        port = chrome_debug_port(config)
        log.info("[castle] mint via Chrome port=%s live=%s", port, probe_cdp_ws(port))
        # Hidden tab = Castle warmup timeout → error_generic, no OTP.
        mint_cfg = dict(config)
        mint_cfg["chrome_steal_focus"] = True
        mint_cfg["chrome_window_mode"] = "normal"
        mint_cfg["chrome_background"] = False
        handle = await open_or_attach_browser(mint_cfg)
        tab = handle.tab
        await _install_visibility_spoof(tab)
        from grokreg.browser.chrome import navigate_signup_with_cf

        net_sink: dict[str, Any] = {"want_email": email}
        await _install_network_token_hook(tab, net_sink)
        await navigate_signup_with_cf(tab, mint_cfg, SIGNUP_URL)
        await _exec_js(tab, _VIS_SPOOF_JS)
        await _exec_js(tab, _HOOK_JS)
        await _wiggle(tab)
        if email:
            slog.api_ok("Castle: form native (Turnstile + sendVerificationCode)")
            result = await _native_create_email(tab, mint_cfg, email, net_sink)
            if result.email_sent:
                slog.api_ok(
                    f"Castle OTP page method={result.method} token={len(result.token)}"
                )
            elif result.token:
                slog.api_ok(f"Castle token only len={len(result.token)} — retry CreateEmail")
                if await _browser_send_create_email(tab, email, result.token):
                    # Only trust this if the page then advances.
                    from grokreg.browser.chrome import detect_page_step

                    for _ in range(12):
                        if await detect_page_step(tab) == "otp":
                            result.email_sent = True
                            result.method = "fetch_then_otp"
                            break
                        await asyncio.sleep(0.5)
        if not result.token and not result.email_sent:
            result.error = result.error or "empty"
            slog.api_err(f"Castle mint fail: {result.error}")

        if session is not None:
            try:
                cookies = await _cdp_get_cookies(tab, ["https://accounts.x.ai/", SIGNUP_URL])
                skip = {"sso", "sso-rw"}
                cookies = [c for c in cookies if str(c.get("name") or "") not in skip]
                n = _apply_cookies(session, cookies)
                log.info("[castle] copied %s cookies (no sso) to HTTP session", n)
            except Exception as e:
                log.warning("[castle] cookie copy: %s", e)
    finally:
        if handle is not None:
            try:
                await close_browser_handle(handle)
            except Exception:
                pass
    return result


def mint_castle(
    config: dict[str, Any],
    session: Any | None = None,
    email: str = "",
) -> MintResult:
    """Sync wrapper — protocol worker is sync."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("mint_castle called from running loop")
    return asyncio.run(_mint_async(config, session, email))


def mint_castle_token(config: dict[str, Any], session: Any | None = None) -> str:
    return mint_castle(config, session).token
