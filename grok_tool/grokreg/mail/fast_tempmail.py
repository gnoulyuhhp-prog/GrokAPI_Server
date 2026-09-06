"""
Fast Temp Mail providers ported from GROK-REG:
- TinyHost (https://tinyhost.shop or custom base)
- TempMail.lol (https://api.tempmail.lol/v2)
- TempMailVIP (https://tempmailapi.io.vn)
- Racing / Fastest Inbox Provider
"""

from __future__ import annotations

import html
import logging
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import quote

import requests

from grokreg.core.helpers import extract_otp, random_string
from grokreg.core.stop_control import raise_if_stop, sleep_interruptible
from grokreg.mail.mail_api import EmailSession

log = logging.getLogger("grok-reg")

_CODE_LABEL = r"(?:verification|confirm(?:ation)?|security|login|one[ -]?time|otp|code|mã xác minh|mã xác nhận|mã otp)"
_CODE_VALUE = r"([0-9]{3})[\s\-‐-―]?([0-9]{3})"
_CODE_AFTER_LABEL_RE = re.compile(
    rf"{_CODE_LABEL}[^0-9]{{0,80}}(?<![0-9]){_CODE_VALUE}(?![0-9])",
    re.IGNORECASE | re.DOTALL,
)
_CODE_BEFORE_LABEL_RE = re.compile(
    rf"(?<![0-9]){_CODE_VALUE}(?![0-9])[^a-z0-9]{{0,30}}{_CODE_LABEL}",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL_RE = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@([a-z0-9.-]+)", re.IGNORECASE)


def _trusted_xai_sender(sender: str) -> bool:
    domains = [match.group(1).lower().rstrip(".") for match in _EMAIL_RE.finditer(sender or "")]
    return any(
        domain in {"x.ai", "grok.com"} or domain.endswith((".x.ai", ".grok.com"))
        for domain in domains
    )


def extract_xai_code_strict(sender: str, subject: str, body: str) -> Optional[str]:
    full_text = f"{subject}\n{body}"
    cleaned = html.unescape(full_text)
    cleaned = re.sub(r"<(?:style|script|svg)\b[^>]*>.*?</(?:style|script|svg)>", " ", cleaned, flags=re.I | re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    match = _CODE_AFTER_LABEL_RE.search(cleaned) or _CODE_BEFORE_LABEL_RE.search(cleaned)
    if match:
        return "".join(match.groups())
    
    if _trusted_xai_sender(sender):
        otp = extract_otp(cleaned)
        if otp:
            return otp
    return None


def _message_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp / 1000 if stamp > 1e12 else stamp
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(str(value)).timestamp()
        except (TypeError, ValueError):
            return None


class BaseFastTempProvider:
    name: str = "base"

    def create_session(self) -> EmailSession:
        raise NotImplementedError

    def list_messages(self, session: EmailSession) -> list[dict[str, Any]]:
        raise NotImplementedError

    def wait_otp(self, session: EmailSession, timeout: int = 180) -> Optional[str]:
        started_at = time.time()
        deadline = started_at + timeout
        attempt = 0
        previous_count: int | None = None

        while time.time() < deadline:
            raise_if_stop()
            attempt += 1
            try:
                messages = self.list_messages(session)
                if previous_count != len(messages):
                    log.info(f"[mail] {session.provider}: API trả {len(messages)} thư")
                    previous_count = len(messages)

                messages.sort(
                    key=lambda msg: (
                        _message_time(msg.get("date") or msg.get("receivedAt") or msg.get("created_at")) or 0,
                        str(msg.get("id") or msg.get("uid") or ""),
                    ),
                    reverse=True,
                )

                for msg in messages:
                    stamp = _message_time(msg.get("date") or msg.get("receivedAt") or msg.get("created_at"))
                    if stamp is not None and stamp < started_at - 60:
                        continue

                    sender = str(msg.get("sender") or msg.get("from") or msg.get("from_address") or "")
                    subject = str(msg.get("subject") or "")
                    body = (
                        str(msg.get("body") or msg.get("body_text") or msg.get("text") or "")
                        + "\n"
                        + str(msg.get("html_body") or msg.get("body_html") or msg.get("html") or "")
                    )

                    code = extract_xai_code_strict(sender, subject, body)
                    if code:
                        log.info(f"[mail] Nhận mã xAI: {code} (lần thử {attempt})")
                        return code
            except Exception as exc:
                log.debug(f"[mail] {session.provider} poll error: {exc}")

            sleep_interruptible(3.0)
        log.warning(f"[mail] {session.provider} timeout OTP sau {timeout}s")
        return None


class TinyHostProvider(BaseFastTempProvider):
    name = "tinyhost"

    def __init__(self, base_url: str = "https://tinyhost.shop", rejected_domains: set[str] | None = None) -> None:
        self.base_url = (base_url or "https://tinyhost.shop").rstrip("/")
        self.rejected_domains = {d.lower().strip() for d in (rejected_domains or set())}
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"})

    def create_session(self) -> EmailSession:
        resp = self._http.get(f"{self.base_url}/api/random-domains/", params={"limit": 50}, timeout=15)
        resp.raise_for_status()
        raw_domains = resp.json().get("domains") or []
        domains: list[str] = []
        for raw in raw_domains:
            clean = str(raw).strip().strip(".").lower()
            if re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9]\.)+[a-z]{2,}$", clean):
                domains.append(clean)
        if not domains:
            raise RuntimeError("TinyHost không trả domain hợp lệ")
        available = [d for d in domains if d not in self.rejected_domains] or domains
        domain = random.choice(available)
        user = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
        addr = f"{user}@{domain}"
        return EmailSession(
            address=addr,
            password=random_string(14),
            provider="tinyhost",
            extra={"base_url": self.base_url, "user": user, "domain": domain},
        )

    def list_messages(self, session: EmailSession) -> list[dict[str, Any]]:
        user = session.extra.get("user")
        domain = session.extra.get("domain")
        if not user or not domain:
            user, domain = session.address.rsplit("@", 1)
        url = f"{self.base_url}/api/email/{quote(domain, safe='')}/{quote(user, safe='')}/"
        resp = self._http.get(url, params={"page": 1, "limit": 50}, timeout=15)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        messages = resp.json().get("emails") or []
        detailed: list[dict[str, Any]] = []
        for message in messages:
            if message.get("body") or message.get("html_body") or message.get("body_text"):
                detailed.append(message)
                continue
            email_id = message.get("id")
            if email_id is None:
                detailed.append(message)
                continue
            det_resp = self._http.get(f"{url}{email_id}", timeout=15)
            if det_resp.status_code == 200:
                ddata = det_resp.json()
                detail = ddata.get("email", ddata)
            else:
                detail = None
            detailed.append(detail or message)
        return detailed


class TempMailLolProvider(BaseFastTempProvider):
    name = "tempmail_lol"

    def __init__(self, api_key: str = "", base_url: str = "https://api.tempmail.lol/v2") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.tempmail.lol/v2").rstrip("/")
        self._http = requests.Session()
        headers = {"Accept": "application/json", "User-Agent": "GROK-TOOL/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http.headers.update(headers)

    def create_session(self) -> EmailSession:
        resp = self._http.post(f"{self.base_url}/inbox/create", json={}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        address = str(data.get("address") or "")
        token = str(data.get("token") or "")
        if not address or not token:
            raise RuntimeError("TempMail.lol trả inbox không hợp lệ")
        return EmailSession(
            address=address,
            password=random_string(14),
            provider="tempmail_lol",
            token=token,
            extra={"token": token, "base_url": self.base_url},
        )

    def list_messages(self, session: EmailSession) -> list[dict[str, Any]]:
        token = session.token or session.extra.get("token")
        resp = self._http.get(f"{self.base_url}/inbox", params={"token": token}, timeout=15)
        if resp.status_code in (404, 410):
            return []
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("emails") or data.get("messages") or []


class TempMailVipProvider(BaseFastTempProvider):
    name = "tempmail_vip"

    def __init__(self, api_key: str = "", base_url: str = "https://tempmailapi.io.vn/public_api.php") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url or "https://tempmailapi.io.vn/public_api.php"
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": "GROK-TOOL/1.0"})

    def create_session(self) -> EmailSession:
        if not self.api_key:
            raise ValueError("TempMailVIP cần API key")
        resp = self._http.get(
            self.base_url,
            headers={"X-API-Key": self.api_key},
            params={"action": "create", "api_key": self.api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success") or not data.get("email"):
            raise RuntimeError(str(data.get("message") or "TempMailVIP không tạo được inbox"))
        return EmailSession(
            address=str(data["email"]),
            password=random_string(14),
            provider="tempmail_vip",
            extra={"api_key": self.api_key},
        )

    def list_messages(self, session: EmailSession) -> list[dict[str, Any]]:
        api_key = self.api_key or session.extra.get("api_key")
        resp = self._http.get(
            self.base_url,
            headers={"X-API-Key": api_key},
            params={"action": "list", "email": session.address, "limit": 50, "api_key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return []
        messages = data.get("emails") or []
        detailed: list[dict[str, Any]] = []
        for message in messages:
            if message.get("body") or message.get("body_text") or message.get("body_html"):
                detailed.append(message)
                continue
            selector = {}
            if message.get("uid") is not None:
                selector = {"uid": message["uid"]}
            elif message.get("id") is not None:
                selector = {"id": message["id"]}
            else:
                detailed.append(message)
                continue
            det_resp = self._http.get(
                self.base_url,
                headers={"X-API-Key": api_key},
                params={"action": "read", "email": session.address, **selector, "api_key": api_key},
                timeout=15,
            )
            if det_resp.status_code == 200:
                ddata = det_resp.json()
                detail = ddata.get("email") if ddata.get("success") else None
            else:
                detail = None
            detailed.append(detail or message)
        return detailed


class RacingMailProvider(BaseFastTempProvider):
    name = "racing"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        vip_key = str(self.config.get("tempmail_vip_key") or self.config.get("tempmail_vip", {}).get("api_key") or "").strip()
        lol_key = str(self.config.get("tempmail_lol_key") or self.config.get("tempmail_lol", {}).get("api_key") or "").strip()
        tinyhost_base = str(self.config.get("tinyhost_base_url") or "https://tinyhost.shop").strip()

        self.candidates: list[BaseFastTempProvider] = [
            TinyHostProvider(base_url=tinyhost_base),
            TempMailLolProvider(api_key=lol_key),
        ]
        if vip_key:
            self.candidates.append(TempMailVipProvider(api_key=vip_key))

    def create_session(self) -> EmailSession:
        errors = []
        shuffled = list(self.candidates)
        random.shuffle(shuffled)

        for provider in shuffled:
            try:
                session = provider.create_session()
                log.info(f"[racing mail] Chọn thành công provider: {session.provider} ({session.address})")
                return session
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")

        raise RuntimeError("Không provider nào trong Racing mode khả dụng: " + "; ".join(errors))

    def list_messages(self, session: EmailSession) -> list[dict[str, Any]]:
        for prov in self.candidates:
            if prov.name == session.provider:
                return prov.list_messages(session)
        return TinyHostProvider().list_messages(session)
