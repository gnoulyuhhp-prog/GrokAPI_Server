"""
Temp mail client for https://tmail.wibucrypto.pro

Laravel + Livewire 2 (no public REST API). Uses Livewire Message Protocol:

  GET  /mailbox
  POST /livewire/message/frontend.actions  (create / random)
  POST /livewire/message/frontend.app      (syncEmail + fetchMessages)
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote

import requests

log = logging.getLogger("grok-reg")

DEFAULT_BASE = "https://tmail.wibucrypto.pro"

_XAI_HINTS = (
    "noreply@x.ai",
    "no-reply@x.ai",
    "accounts.x.ai",
    "verify your email",
    "verification code",
    "security code",
    "your code is",
    "confirmation code",
    "your xai",
)


def _is_xai_blob(blob: str) -> bool:
    b = (blob or "").lower()
    return any(h in b for h in _XAI_HINTS)


def extract_otp_from_text(text: str) -> Optional[str]:
    """
    Use shared main.extract_otp so azpop + wibu handle YI2-BKR the same way.
    Fallback local patterns if import fails.
    """
    if not text:
        return None
    try:
        from grokreg.core.helpers import extract_otp as _main_extract

        return _main_extract(text)
    except Exception:
        pass
    # local fallback: dashed alnum first, then 6 digits
    m = re.search(
        r"(?:confirmation\s*code|verification\s*code|code)\s*[:\-]?\s*"
        r"([A-Z0-9]{2,5}-[A-Z0-9]{2,5})",
        text,
        re.I,
    )
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z0-9]{2,5}-[A-Z0-9]{2,5})\b", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    return None

def _strip_tags(raw: str) -> str:
    if not raw:
        return ""
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class TmailSession:
    """Runtime session state kept in EmailSession.extra."""

    address: str
    base_url: str = DEFAULT_BASE
    csrf: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    app_fingerprint: dict[str, Any] = field(default_factory=dict)
    app_server_memo: dict[str, Any] = field(default_factory=dict)
    actions_fingerprint: dict[str, Any] = field(default_factory=dict)
    actions_server_memo: dict[str, Any] = field(default_factory=dict)


class TmailWibuProvider:
    """
    Temp mail via tmail.wibucrypto.pro (Livewire 2).

    create()  → random or custom user@domain
    wait_otp() → poll syncEmail + fetchMessages, extract 6-digit xAI code
    """

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or {}
        self.base = str(cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
        self.verify_ssl = bool(cfg.get("verify_ssl", True))
        self.poll_interval = float(cfg.get("poll_interval") or 4)
        pref = cfg.get("domains") or cfg.get("preferred_domains") or []
        if isinstance(pref, str):
            pref = [p.strip() for p in pref.split(",") if p.strip()]
        self.preferred_domains = [str(d).strip().lower() for d in pref if str(d).strip()]
        # Prefer custom create on clean domains — random multi-subdomains often blocked by xAI
        self.mode = str(cfg.get("create_mode") or "create").lower()  # create | random
        self._last_csrf: str = ""
        self._http = requests.Session()
        self._http.trust_env = False
        self._http.verify = self.verify_ssl
        if not self.verify_ssl:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
        self._http.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def handshake(self) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
        """
        GET /mailbox → csrf + frontend.app + frontend.actions components.
        Returns (page_email_or_empty, csrf, app_component, actions_component).
        """
        r = self._http.get(f"{self.base}/mailbox", timeout=30)
        r.raise_for_status()
        text = r.text or ""
        csrf = self._extract_csrf(text, r)
        if not csrf:
            raise RuntimeError("TmailWibu: CSRF token not found on /mailbox")
        self._last_csrf = csrf

        comps = self._parse_components(text)
        app = comps.get("frontend.app")
        actions = comps.get("frontend.actions")
        if not app:
            raise RuntimeError("TmailWibu: frontend.app Livewire component missing")
        if not actions:
            raise RuntimeError("TmailWibu: frontend.actions Livewire component missing")

        page_email = ""
        m = re.search(r"const email = '([^']+)'", text)
        if m:
            page_email = m.group(1).strip()
        if not page_email:
            # cookie-backed email in actions data
            page_email = str(
                (actions.get("serverMemo") or {}).get("data", {}).get("email") or ""
            )

        log.info(
            "TmailWibu handshake ok csrf=%s… email=%s app_id=%s",
            csrf[:8],
            page_email or "(none)",
            (app.get("fingerprint") or {}).get("id", "")[:12],
        )
        return page_email, csrf, app, actions

    def _extract_csrf(self, html: str, resp: requests.Response) -> str:
        m = re.search(r'csrf-token"\s+content="([^"]+)"', html, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"livewire_token\s*=\s*'([^']+)'", html)
        if m:
            return m.group(1).strip()
        # XSRF cookie (URL-encoded JSON) — Laravel often accepts decoded value
        xsrf = resp.cookies.get("XSRF-TOKEN") or self._http.cookies.get("XSRF-TOKEN")
        if xsrf:
            try:
                return unquote(xsrf)
            except Exception:
                return str(xsrf)
        return ""

    @staticmethod
    def _parse_components(page_html: str) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for m in re.finditer(
            r'wire:id="([^"]+)"\s+wire:initial-data="([^"]+)"',
            page_html,
        ):
            try:
                data = json.loads(html_lib.unescape(m.group(2)))
            except Exception:
                continue
            name = str((data.get("fingerprint") or {}).get("name") or "")
            if name:
                out[name] = data
        return out

    # ------------------------------------------------------------------
    # Livewire POST
    # ------------------------------------------------------------------

    def _livewire_headers(self, csrf: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Livewire": "true",
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base}/mailbox",
            "Origin": self.base,
        }

    def _livewire_post(
        self,
        component_name: str,
        fingerprint: dict[str, Any],
        server_memo: dict[str, Any],
        updates: list[dict[str, Any]],
        csrf: str,
    ) -> dict[str, Any]:
        url = f"{self.base}/livewire/message/{component_name}"
        payload = {
            "fingerprint": fingerprint,
            "serverMemo": server_memo,
            "updates": updates,
        }
        r = self._http.post(
            url,
            json=payload,
            headers=self._livewire_headers(csrf),
            timeout=45,
        )
        if r.status_code in (419, 401, 403):
            raise RuntimeError(f"TmailWibu: session/csrf expired HTTP {r.status_code}")
        if r.status_code >= 500:
            # checksum mismatch often surfaces as 500
            raise RuntimeError(
                f"TmailWibu: Livewire server error HTTP {r.status_code}: {(r.text or '')[:180]}"
            )
        r.raise_for_status()
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"TmailWibu: non-JSON Livewire response: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError("TmailWibu: unexpected Livewire payload type")
        # merge memo so subsequent calls keep checksum fresh
        sm = data.get("serverMemo") or {}
        if isinstance(sm, dict):
            if "data" in sm and isinstance(sm["data"], dict):
                base_data = server_memo.setdefault("data", {})
                if isinstance(base_data, dict):
                    base_data.update(sm["data"])
            for k in ("checksum", "htmlHash", "dataMeta", "errors", "children"):
                if k in sm:
                    server_memo[k] = sm[k]
        return data

    # ------------------------------------------------------------------
    # Create mailbox
    # ------------------------------------------------------------------

    def create_mailbox(self) -> tuple[str, dict[str, Any]]:
        """
        Create a fresh temp address.
        Returns (email_address, extra_state) for EmailSession.extra.
        """
        _page_email, csrf, _app, actions = self.handshake()
        address = ""

        if self.mode == "random":
            address = self._create_random(actions, csrf)
        else:
            address = self._create_custom(actions, csrf)

        if not address:
            raise RuntimeError("TmailWibu: create returned empty email")

        # Re-handshake so cookie + frontend.app checksum match the new mailbox
        page_email2, csrf2, app2, actions2 = self.handshake()
        final = page_email2 or address

        log.info("TmailWibu ready: %s  (base=%s)", final, self.base)
        extra: dict[str, Any] = {
            "base_url": self.base,
            "csrf": csrf2,
            "cookies": self._http.cookies.get_dict(),
            "app_fingerprint": app2.get("fingerprint") or {},
            "app_server_memo": app2.get("serverMemo") or {},
            "actions_fingerprint": actions2.get("fingerprint") or {},
            "actions_server_memo": actions2.get("serverMemo") or {},
        }
        return final, extra

    def _current_csrf(self) -> str:
        if self._last_csrf:
            return self._last_csrf
        xsrf = self._http.cookies.get("XSRF-TOKEN")
        if xsrf:
            try:
                return unquote(xsrf)
            except Exception:
                return str(xsrf)
        return ""

    def _create_random(self, actions: dict[str, Any], csrf: str) -> str:
        fp = actions["fingerprint"]
        memo = actions["serverMemo"]
        data = self._livewire_post(
            "frontend.actions",
            fp,
            memo,
            [
                {
                    "type": "callMethod",
                    "payload": {
                        "id": f"r{int(time.time() * 1000)}",
                        "method": "random",
                        "params": [],
                    },
                }
            ],
            csrf,
        )
        email = str((data.get("serverMemo") or {}).get("data", {}).get("email") or "")
        if not email:
            # follow redirect cookie by re-get
            pe, _, _, _ = self.handshake()
            email = pe
        return email

    def _create_custom(self, actions: dict[str, Any], csrf: str) -> str:
        domains = list(
            (actions.get("serverMemo") or {}).get("data", {}).get("domains") or []
        )
        domains = [str(d).strip().lower() for d in domains if str(d).strip()]
        domain = self._pick_domain(domains)
        user = self._random_user()
        fp = actions["fingerprint"]
        memo = actions["serverMemo"]
        log.info("TmailWibu create custom %s@%s (pool=%s)", user, domain, len(domains))
        data = self._livewire_post(
            "frontend.actions",
            fp,
            memo,
            [
                {
                    "type": "syncInput",
                    "payload": {"id": "u", "name": "user", "value": user},
                },
                {
                    "type": "syncInput",
                    "payload": {"id": "d", "name": "domain", "value": domain},
                },
                {
                    "type": "callMethod",
                    "payload": {
                        "id": f"c{int(time.time() * 1000)}",
                        "method": "create",
                        "params": [],
                    },
                },
            ],
            csrf,
        )
        email = str((data.get("serverMemo") or {}).get("data", {}).get("email") or "")
        if not email:
            email = f"{user}@{domain}"
        return email

    def _pick_domain(self, live: list[str]) -> str:
        live_set = set(live)
        # preferred that exist live (config order first)
        pref = (
            [d for d in self.preferred_domains if d in live_set]
            if live
            else list(self.preferred_domains)
        )
        def _is_clean(d: str) -> bool:
            # reject long random multi-sub (xAI + OTP often drop these)
            if d.count(".") > 2 or len(d) > 22:
                return False
            # random prefix on known tlds: anro.name.ng / btaeli.name.ng
            if re.match(r"^[a-z0-9]{3,}\.(name\.ng)$", d) and d not in (
                "aden.name.ng",
                "adon.name.ng",
                "alen.name.ng",
                "ames.name.ng",
                "adix.name.ng",
            ):
                return False
            if re.match(r"^[a-z0-9]{6,}\.(edu\.vn|top)$", d):
                return False
            if re.match(r"^[a-z0-9]{8,}\.", d):
                return False
            for bad in ("caa", "eem", "okdt", "boron", "btaeli", "caapxsa"):
                if bad in d:
                    return False
            return True

        clean = [d for d in live if _is_clean(d)]
        # If config preferred domains are live → ONLY those (tool kia dùng domain sạch)
        pref_live = [d for d in pref if d in live_set]
        if pref_live:
            pool = list(pref_live)
        else:
            pool = list(clean)
        if not pool:
            # shortest live fallback
            pool = sorted(live, key=len)[:20] if live else ["wibucrypto.pro"]
        # Use same ranker as azpop (success history + soft ban, no random first)
        try:
            from grokreg.browser.anti_flag import pick_diverse_domain, rank_domains

            choice = pick_diverse_domain(pool)
            ranked_preview = rank_domains(pool)[:6]
        except Exception:
            ranked_preview = pool[:6]
            # mild weighted shuffle among top preferred
            top = pool[: max(3, min(6, len(pool)))]
            choice = random.choice(top)
        log.info(
            "TmailWibu domain pick: %s (pref=%s clean=%s live=%s) top=%s",
            choice,
            len(pref),
            len(clean),
            len(live),
            ranked_preview,
        )
        return choice

    @staticmethod
    def _random_user(n: int = 10) -> str:
        # start with letter — some sites reject pure digits
        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=n - 1))
        return first + rest

    # ------------------------------------------------------------------
    # OTP
    # ------------------------------------------------------------------

    def wait_otp(
        self,
        session: Any,
        timeout: int = 180,
        *,
        ignore_ids: set[str] | None = None,
    ) -> Optional[str]:
        ignore_ids = set(ignore_ids or set())
        address = str(getattr(session, "address", "") or "")
        if not address or "@" not in address:
            raise RuntimeError(f"TmailWibu: invalid session address {address!r}")

        extra = dict(getattr(session, "extra", None) or {})
        self._restore_cookies(extra.get("cookies") or {})

        deadline = time.time() + timeout
        t0 = time.time()
        log.info(
            "Waiting OTP via TmailWibu %s for %s (max %ss)...",
            self.base,
            address,
            timeout,
        )
        seen: set[str] = set(ignore_ids)
        poll_i = 0
        consecutive_err = 0

        while time.time() < deadline:
            poll_i += 1
            try:
                # 1) Full-page scrape (most reliable — SSR has messages when IMAP lands)
                page_otp, page_html, page_email = self._scrape_mailbox_page(extra)
                if page_email and page_email.lower() != address.lower():
                    log.warning(
                        "TmailWibu page email %s != %s", page_email, address
                    )
                if page_otp:
                    log.info(
                        "OTP found (TmailWibu PAGE): display=%s input=%s (%.1fs)",
                        page_otp,
                        self._norm(page_otp),
                        time.time() - t0,
                    )
                    session.extra = extra
                    return page_otp

                # 2) Livewire fetchMessages (syncEmail + fetch, then open each id)
                messages, html_blob = self._fetch_messages(address, extra)
                consecutive_err = 0
                messages = self._newest_first(messages)

                if messages:
                    log.info(
                        "TmailWibu inbox %s msg(s) — newest subj=%r",
                        len(messages),
                        str(messages[0].get("subject") or messages[0].get("body") or "")[
                            :60
                        ],
                    )
                elif poll_i % 4 == 0:
                    log.info(
                        "TmailWibu still empty for %s (%.0fs) poll=%s",
                        address,
                        time.time() - t0,
                        poll_i,
                    )

                # try extract from each message blob + try open by id
                for msg in messages:
                    mid = str(msg.get("id") or msg.get("uid") or "")
                    if mid and mid in seen and not msg.get("body"):
                        continue
                    blob = self._msg_blob(msg)
                    otp = self._try_extract(blob, looks_required=True)
                    if otp:
                        log.info(
                            "OTP found (TmailWibu): display=%s input=%s id=%s (%.1fs)",
                            otp,
                            self._norm(otp),
                            mid or "-",
                            time.time() - t0,
                        )
                        session.extra = extra
                        return otp
                    # open message body via Livewire if we only have an id
                    if mid and re.fullmatch(r"\d+", mid):
                        body_html = self._open_message(address, extra, mid)
                        if body_html:
                            otp = self._try_extract(body_html, looks_required=True)
                            if otp:
                                log.info(
                                    "OTP found (TmailWibu open): display=%s input=%s id=%s (%.1fs)",
                                    otp,
                                    self._norm(otp),
                                    mid,
                                    time.time() - t0,
                                )
                                session.extra = extra
                                return otp
                    if mid:
                        seen.add(mid)

                # 3) Livewire HTML effects
                for blob in (html_blob, page_html):
                    if not blob or "Empty Inbox" in blob:
                        continue
                    otp = self._try_extract(blob, looks_required=True)
                    if otp:
                        log.info(
                            "OTP found (TmailWibu HTML): display=%s input=%s (%.1fs)",
                            otp,
                            self._norm(otp),
                            time.time() - t0,
                        )
                        session.extra = extra
                        return otp

                # hard refresh every few polls to avoid stale Livewire checksum
                if poll_i % 3 == 0:
                    self._rehandshake_into(address, extra)

            except Exception as e:
                # allow ESC stop to bubble
                if e.__class__.__name__ == "StopRequested":
                    raise
                consecutive_err += 1
                log.warning("TmailWibu poll error (%s): %s", consecutive_err, e)
                try:
                    self._rehandshake_into(address, extra)
                    consecutive_err = 0
                except Exception as e2:
                    log.warning("TmailWibu re-handshake failed: %s", e2)

            try:
                from grokreg.core.stop_control import raise_if_stop, sleep_interruptible

                raise_if_stop()
                sleep_interruptible(self.poll_interval + random.uniform(0.3, 1.5))
            except ImportError:
                time.sleep(self.poll_interval + random.uniform(0.3, 1.5))

        log.error("TmailWibu OTP timeout after %ss for %s", timeout, address)
        session.extra = extra
        return None

    @staticmethod
    def _norm(otp: str) -> str:
        try:
            from grokreg.core.helpers import normalize_otp_for_input

            return normalize_otp_for_input(otp)
        except Exception:
            return re.sub(r"[^A-Za-z0-9]", "", otp or "").upper()

    @staticmethod
    def _msg_blob(msg: dict[str, Any]) -> str:
        return " ".join(
            str(msg.get(k) or "")
            for k in (
                "subject",
                "from",
                "sender",
                "preview",
                "body",
                "content",
                "message",
                "text",
                "html",
            )
        )

    @staticmethod
    def _newest_first(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            return sorted(
                list(messages or []),
                key=lambda x: str(
                    x.get("date")
                    or x.get("time")
                    or x.get("id")
                    or x.get("uid")
                    or ""
                ),
                reverse=True,
            )
        except Exception:
            return list(reversed(list(messages or [])))

    def _try_extract(self, blob: str, *, looks_required: bool = True) -> Optional[str]:
        if not blob:
            return None
        plain = _strip_tags(blob) if "<" in blob else blob
        otp = extract_otp_from_text(plain) or extract_otp_from_text(blob)
        if not otp:
            return None
        try:
            from grokreg.core.helpers import is_plausible_xai_otp

            if not is_plausible_xai_otp(otp):
                return None
        except Exception:
            pass
        if looks_required:
            looks = _is_xai_blob(plain) or bool(
                re.search(r"verif|confirm|code|x\.ai|grok|noreply", plain, re.I)
            )
            if not looks and not re.fullmatch(
                r"[A-Z0-9]{2,5}-[A-Z0-9]{2,5}", otp or ""
            ):
                return None
        return otp

    def _restore_cookies(self, cookies: dict[str, Any]) -> None:
        if not cookies:
            return
        for k, v in cookies.items():
            try:
                self._http.cookies.set(str(k), str(v), domain="tmail.wibucrypto.pro")
            except Exception:
                try:
                    self._http.cookies.set(str(k), str(v))
                except Exception:
                    pass

    def _rehandshake_into(self, address: str, extra: dict[str, Any]) -> None:
        """Full GET /mailbox refresh; keep address (cookie should still map)."""
        log.debug("TmailWibu re-handshake for %s", address)
        page_email, csrf, app, actions = self.handshake()
        extra["csrf"] = csrf
        extra["cookies"] = self._http.cookies.get_dict()
        extra["app_fingerprint"] = app.get("fingerprint") or {}
        extra["app_server_memo"] = app.get("serverMemo") or {}
        extra["actions_fingerprint"] = actions.get("fingerprint") or {}
        extra["actions_server_memo"] = actions.get("serverMemo") or {}
        if page_email and page_email.lower() != address.lower():
            log.warning(
                "TmailWibu cookie email %s != session %s — still sync session address",
                page_email,
                address,
            )

    def _scrape_mailbox_page(
        self, extra: dict[str, Any]
    ) -> tuple[Optional[str], str, str]:
        """
        GET /mailbox full HTML — when IMAP delivers, SSR often has subject/body.
        Returns (otp_or_None, html, page_email).
        """
        r = self._http.get(f"{self.base}/mailbox", timeout=30)
        r.raise_for_status()
        text = r.text or ""
        csrf = self._extract_csrf(text, r)
        if csrf:
            self._last_csrf = csrf
            extra["csrf"] = csrf
        extra["cookies"] = self._http.cookies.get_dict()
        # refresh livewire components from page
        comps = self._parse_components(text)
        if "frontend.app" in comps:
            app = comps["frontend.app"]
            extra["app_fingerprint"] = app.get("fingerprint") or {}
            extra["app_server_memo"] = app.get("serverMemo") or {}
        page_email = ""
        m = re.search(r"const email = '([^']+)'", text)
        if m:
            page_email = m.group(1).strip()
        if "Empty Inbox" in text and "noreply@x.ai" not in text.lower():
            return None, text, page_email
        otp = None
        if _is_xai_blob(text):
            otp = self._try_extract(text, looks_required=True)
        return otp, text, page_email

    def _open_message(
        self, address: str, extra: dict[str, Any], msg_id: str
    ) -> str:
        """Try Livewire methods to load full message HTML body."""
        csrf = str(extra.get("csrf") or self._current_csrf())
        fp = dict(extra.get("app_fingerprint") or {})
        memo = dict(extra.get("app_server_memo") or {})
        if not fp or not memo:
            return ""
        # common method names across Tmail forks
        for method, params in (
            ("select", [int(msg_id) if msg_id.isdigit() else msg_id]),
            ("open", [int(msg_id) if msg_id.isdigit() else msg_id]),
            ("show", [int(msg_id) if msg_id.isdigit() else msg_id]),
            ("setId", [int(msg_id) if msg_id.isdigit() else msg_id]),
        ):
            try:
                data = self._livewire_post(
                    "frontend.app",
                    fp,
                    memo,
                    [
                        {
                            "type": "callMethod",
                            "payload": {
                                "id": f"o{msg_id}{method}",
                                "method": method,
                                "params": params,
                            },
                        }
                    ],
                    csrf,
                )
                html_blob = str((data.get("effects") or {}).get("html") or "")
                if html_blob and "Empty Inbox" not in html_blob:
                    return html_blob
            except Exception:
                continue
        return ""

    def _fetch_messages(
        self, address: str, extra: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str]:
        """
        Livewire: syncEmail then fetchMessages (separate requests — more reliable).
        """
        csrf = str(extra.get("csrf") or self._current_csrf())
        fp = dict(extra.get("app_fingerprint") or {})
        memo = dict(extra.get("app_server_memo") or {})
        if not fp or not memo:
            self._rehandshake_into(address, extra)
            csrf = str(extra.get("csrf") or self._current_csrf())
            fp = dict(extra.get("app_fingerprint") or {})
            memo = dict(extra.get("app_server_memo") or {})

        def _post(updates: list[dict[str, Any]]) -> dict[str, Any]:
            return self._livewire_post("frontend.app", fp, memo, updates, csrf)

        try:
            _post(
                [
                    {
                        "type": "fireEvent",
                        "payload": {
                            "id": f"se{int(time.time() * 1000)}",
                            "event": "syncEmail",
                            "params": [address],
                        },
                    }
                ]
            )
            data = _post(
                [
                    {
                        "type": "fireEvent",
                        "payload": {
                            "id": f"fm{int(time.time() * 1000)}",
                            "event": "fetchMessages",
                            "params": [],
                        },
                    }
                ]
            )
        except RuntimeError:
            self._rehandshake_into(address, extra)
            csrf = str(extra.get("csrf") or self._current_csrf())
            fp = dict(extra.get("app_fingerprint") or {})
            memo = dict(extra.get("app_server_memo") or {})
            data = self._livewire_post(
                "frontend.app",
                fp,
                memo,
                [
                    {
                        "type": "fireEvent",
                        "payload": {
                            "id": f"se{int(time.time() * 1000)}",
                            "event": "syncEmail",
                            "params": [address],
                        },
                    },
                    {
                        "type": "fireEvent",
                        "payload": {
                            "id": f"fm{int(time.time() * 1000)}",
                            "event": "fetchMessages",
                            "params": [],
                        },
                    },
                ],
                csrf,
            )

        extra["csrf"] = csrf
        extra["cookies"] = self._http.cookies.get_dict()
        extra["app_fingerprint"] = fp
        extra["app_server_memo"] = memo

        effects = data.get("effects") or {}
        html_blob = str(effects.get("html") or "")
        sm = data.get("serverMemo") or {}
        sm_data = sm.get("data") or memo.get("data") or {}
        data_meta = sm.get("dataMeta") or memo.get("dataMeta") or {}
        messages = self._coerce_messages(sm_data, html_blob, data_meta)
        return messages, html_blob

    def _coerce_messages(
        self,
        sm_data: dict[str, Any],
        html_blob: str,
        data_meta: Any = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        raw = sm_data.get("messages") if isinstance(sm_data, dict) else None
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    out.append(item)
                else:
                    out.append({"id": str(item), "body": ""})
        # Livewire sometimes puts model attrs under dataMeta
        if isinstance(data_meta, dict):
            models = (
                data_meta.get("models")
                or data_meta.get("modelCollections")
                or data_meta
            )
            if isinstance(models, dict):
                for key in ("messages", "message"):
                    block = models.get(key)
                    if isinstance(block, dict):
                        # serialized collection rows
                        for row_key, row_val in block.items():
                            if isinstance(row_val, dict) and (
                                "subject" in row_val or "body" in row_val
                            ):
                                out.append(row_val)
                            elif isinstance(row_val, list):
                                for r in row_val:
                                    if isinstance(r, dict):
                                        out.append(r)
        if html_blob and "Empty Inbox" not in html_blob:
            out.extend(self._parse_messages_html(html_blob))
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for m in out:
            key = f"{m.get('id')}|{m.get('subject')}|{str(m.get('body') or '')[:40]}"
            if key in seen:
                continue
            seen.add(key)
            uniq.append(m)
        return uniq

    def _parse_messages_html(self, html_blob: str) -> list[dict[str, Any]]:
        """Best-effort scrape of message cards from Livewire HTML effects."""
        msgs: list[dict[str, Any]] = []
        plain = _strip_tags(html_blob)
        if plain and len(plain) > 20 and "Empty Inbox" not in plain:
            msgs.append(
                {
                    "id": f"html-{hash(plain) & 0xFFFFFFFF:x}",
                    "subject": plain[:120],
                    "body": plain,
                    "from": "",
                }
            )
        low = html_blob.lower()
        if any(h in low for h in ("x.ai", "verification", "confirmation", "noreply")):
            msgs.append(
                {
                    "id": f"raw-{hash(html_blob) & 0xFFFFFFFF:x}",
                    "subject": "raw-html",
                    "body": _strip_tags(html_blob),
                    "from": "",
                }
            )
        return msgs


