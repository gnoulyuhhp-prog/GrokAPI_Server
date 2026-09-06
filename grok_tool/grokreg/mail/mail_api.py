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


from grokreg.core.helpers import (
    extract_otp,
    normalize_otp_for_input,
    _clean_mail_text,
    _score_otp_candidate,
    _render_template,
    _dig_json_otp,
)

@dataclass
class EmailSession:
    address: str
    password: str
    provider: str
    token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    raw_line: str = ""
    list_path: Optional[Path] = None
    extra: dict[str, Any] = field(default_factory=dict)
    mailbox: str = ""

    @property
    def mailbox_address(self) -> str:
        """Outlook mailbox for Graph/IMAP. Plus-alias lives in ``address``."""
        mb = (self.mailbox or "").strip()
        if mb:
            return mb
        extra = self.extra or {}
        mb = str(extra.get("mailbox") or extra.get("main_email") or "").strip()
        return mb or self.address


# Only accept OTP from xAI / Grok related mails — never random digits in Outlook welcome etc.
_XAI_MAIL_HINTS = (
    "x.ai",
    "xai",
    "grok",
    "spacexai",
    "accounts.x.ai",
    "noreply@x.ai",
    "no-reply@x.ai",
    "noreply@",
    "verify your email",
    "verification code",
    "security code",
    "your code is",
    "confirmation code",
    "one time security code",
    "one-time",
    "validate your email",
)
_OTP_IGNORE_HINTS = (
    "welcome to your new outlook",
    "welcome to outlook",
    "microsoft account",
    "security-noreply@microsoft.com",
    "no-reply@microsoft.com",
    "accountprotection",
)


def _normalize_mail_item(m: dict[str, Any]) -> dict[str, Any]:
    """Normalize Graph / mailgen / vercel inbox shapes into a flat message dict."""
    body = m.get("body")
    if isinstance(body, dict):
        body_text = str(body.get("content") or "")
    else:
        body_text = str(body or "")

    frm = m.get("from")
    if isinstance(frm, dict):
        ea = frm.get("emailAddress") if isinstance(frm.get("emailAddress"), dict) else frm
        if isinstance(ea, dict):
            from_s = f"{ea.get('name', '')} {ea.get('address', '')}".strip()
        else:
            from_s = str(frm)
    else:
        from_s = str(frm or m.get("sender") or "")

    return {
        "id": str(m.get("id") or m.get("uid") or m.get("messageId") or ""),
        "date": str(m.get("date") or m.get("receivedDateTime") or m.get("time") or ""),
        "subject": str(m.get("subject") or ""),
        "preview": str(m.get("preview") or m.get("bodyPreview") or m.get("snippet") or ""),
        "message": body_text or str(m.get("message") or m.get("content") or m.get("text") or ""),
        "content": body_text or str(m.get("content") or m.get("message") or ""),
        "from": from_s,
        "code": m.get("code") or "",
    }


def _msg_blob(m: dict[str, Any]) -> str:
    n = _normalize_mail_item(m) if "receivedDateTime" in m or isinstance(m.get("body"), dict) else m
    return " ".join(
        str(n.get(k) or "")
        for k in (
            "subject", "code", "message", "content", "body", "text",
            "from", "preview", "snippet", "sender", "from_address",
        )
    )


def _is_xai_mail(blob: str) -> bool:
    low = (blob or "").lower()
    if any(b in low for b in _OTP_IGNORE_HINTS):
        # still allow if clearly xAI
        if not any(h in low for h in ("x.ai", "xai", "grok", "spacexai", "accounts.x.ai")):
            return False
    return any(h in low for h in _XAI_MAIL_HINTS)


def _extract_otp_strict(text: str, regex: str | None = None) -> Optional[str]:
    """
    Prefer xAI alphanumeric YI2-BKR / XXX-XXX / XXXX-XXX, then classic 6-digit.
    Avoid random digit noise from HTML/CSS.
    """
    return extract_otp(text, pattern=regex)


def _otp_from_mail_payload(
    data: Any,
    regex: str,
    *,
    since_iso: str | None = None,
    ignore_ids: set[str] | None = None,
    strict_xai: bool = True,
) -> Optional[str]:
    """
    Parse mail JSON for verification code.
    Always prefer NEWEST xAI/Grok mail. Never pick codes from Outlook welcome etc.
    """
    if data is None:
        return None

    ignore_ids = ignore_ids or set()

    def _msg_id(m: dict[str, Any]) -> str:
        return str(
            m.get("id")
            or m.get("uid")
            or m.get("messageId")
            or m.get("message_id")
            or ""
        )

    def _msg_date(m: dict[str, Any]) -> str:
        return str(m.get("date") or m.get("receivedDateTime") or m.get("time") or "")

    if not isinstance(data, dict):
        text = str(data)
        if strict_xai and not _is_xai_mail(text):
            return None
        return _extract_otp_strict(text, regex)

    messages = (
        data.get("messages")
        or data.get("emails")
        or data.get("mails")
        or data.get("data")
    )
    if isinstance(messages, dict):
        messages = (
            messages.get("messages")
            or messages.get("emails")
            or messages.get("items")
            or []
        )

    # single-object payload without messages list
    if not isinstance(messages, list):
        blob = " ".join(
            str(data.get(k) or "")
            for k in ("subject", "content", "message", "preview", "code", "from")
        )
        code = data.get("code") or data.get("otp") or data.get("verification_code")
        if code and re.fullmatch(r"\d{4,8}", str(code).strip()):
            if not strict_xai or _is_xai_mail(blob) or not blob:
                return str(code).strip()
        if not strict_xai or _is_xai_mail(blob):
            return _extract_otp_strict(blob, regex)
        return None

    normalized = [
        _normalize_mail_item(m) for m in messages if isinstance(m, dict)
    ]
    ordered = sorted(
        normalized,
        key=_msg_date,
        reverse=True,  # newest first
    )

    candidates: list[tuple[str, str, str]] = []  # (date, id, otp)
    for m in ordered:
        mid = _msg_id(m)
        if mid and mid in ignore_ids:
            continue
        mdate = _msg_date(m)
        # HARD filter: never accept mail older than signup submit time
        if since_iso and mdate and mdate < since_iso:
            continue
        blob = _msg_blob(m)
        if strict_xai and not _is_xai_mail(blob):
            continue

        otp: Optional[str] = None
        c = m.get("code")
        if c:
            cs = str(c).strip().upper()
            if re.fullmatch(r"[A-Z0-9]{2,5}-[A-Z0-9]{2,5}", cs) or re.fullmatch(
                r"\d{4,8}", cs
            ):
                otp = cs
        if not otp:
            # subject often has "confirmation code: YI2-BKR"
            subj = str(m.get("subject") or "")
            otp = _extract_otp_strict(subj, regex) or _extract_otp_strict(blob, regex)
        if otp:
            candidates.append((mdate, mid, otp))

    if not candidates:
        return None

    # newest first (already sorted by date)
    best = candidates[0]
    log.info(
        "OTP from newest xAI mail date=%s display=%s input=%s id=%s",
        best[0],
        best[2],
        normalize_otp_for_input(best[2]),
        best[1],
    )
    return best[2]


class MailApiClient:
    """
    Multi-provider external mail reader (primary OTP source for hotmail).

    Built-in providers (dongvanfb NOT in default — hay lag):
      - mailgen       → mailgen.shop /api/inbox-read + /api/get-inbox
      - generic_graph → same-style Graph endpoints on base_url
      - dongvan_compat→ clone API shape graph_messages/graph_code (optional)
      - custom        → arbitrary URL + body template
    """

    DEFAULT_PROVIDERS: list[dict[str, Any]] = [
        {
            "name": "email_inbox_receiver",
            "enabled": True,
            "base_url": "https://email-inbox-receiver.vercel.app",
            "auth_mode": "graph",  # graph | oauth2
            "max_messages": 20,
        },
        {"name": "ms_graph", "enabled": True},
        {"name": "mailgen", "enabled": True, "base_url": "https://mailgen.shop"},
        {
            "name": "custom",
            "enabled": False,
            "base_url": "http://127.0.0.1:1234",
            "method": "POST",
            "endpoint": "/api/otp",
            "body": {
                "email": "{email}",
                "password": "{password}",
                "refresh_token": "{refresh_token}",
                "client_id": "{client_id}",
            },
            "headers": {},
        },
    ]

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.enabled = bool(cfg.get("enabled", True))
        self.client_id = str(
            cfg.get("client_id") or "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
        )
        self.poll_interval = int(cfg.get("poll_interval") or 4)
        self.timeout = int(cfg.get("timeout") or 180)
        self.otp_regex = str(cfg.get("otp_regex") or r"\b(\d{6})\b")
        self.type = str(cfg.get("type") or "all")
        providers = cfg.get("providers")
        if isinstance(providers, list) and providers:
            self.providers = [p for p in providers if isinstance(p, dict)]
        else:
            # legacy single-provider config
            legacy_name = str(cfg.get("provider") or "").lower().strip()
            if legacy_name in ("dongvanfb", "dongvan", "tools.dongvanfb"):
                self.providers = [
                    {
                        "name": "dongvan_compat",
                        "enabled": True,
                        "base_urls": [
                            str(cfg.get("base_url") or "https://tools.dongvanfb.net")
                        ],
                    }
                ]
            elif legacy_name == "custom":
                self.providers = [
                    {
                        "name": "custom",
                        "enabled": True,
                        "base_url": str(cfg.get("base_url") or ""),
                        "method": str(cfg.get("method") or "POST"),
                        "endpoint": str(
                            cfg.get("endpoint_get_otp") or cfg.get("endpoint") or "/api/otp"
                        ),
                        "body": dict(cfg.get("params") or {}),
                        "headers": dict(cfg.get("headers") or {}),
                    }
                ]
            else:
                self.providers = list(self.DEFAULT_PROVIDERS)

        # for logs
        self.provider = ",".join(
            str(p.get("name")) for p in self.providers if p.get("enabled", True)
        ) or "none"

    def _client_id(self, session: EmailSession) -> str:
        return (session.client_id or self.client_id or MS_CLIENT_IDS[0]).strip()

    def _email_data_line(self, session: EmailSession) -> str:
        cid = self._client_id(session)
        mb = session.mailbox_address
        return f"{mb}|{session.password}|{session.refresh_token or ''}|{cid}"

    def _mapping(self, session: EmailSession) -> dict[str, str]:
        return {
            "email": session.mailbox_address,
            "password": session.password,
            "refresh_token": session.refresh_token or "",
            "client_id": self._client_id(session),
            "email_data": self._email_data_line(session),
        }

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: int = 20,
    ) -> tuple[int, Any, str]:
        r = requests.post(url, json=payload, headers=headers or None, timeout=timeout)
        text = r.text or ""
        data: Any = None
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data, text

    # ----- provider implementations -----

    def _fetch_email_inbox_receiver(
        self,
        session: EmailSession,
        *,
        base_url: str = "https://email-inbox-receiver.vercel.app",
        auth_mode: str = "graph",
        max_messages: int = 20,
        ignore_ids: set[str] | None = None,
        since_iso: str | None = None,
    ) -> Optional[str]:
        """
        https://email-inbox-receiver.vercel.app/hotmail
        POST /api/read-inbox
        """
        base = (base_url or "https://email-inbox-receiver.vercel.app").rstrip("/")
        url = f"{base}/api/read-inbox"
        cid = self._client_id(session)
        modes = [auth_mode] if auth_mode else ["graph"]
        if "oauth2" not in modes:
            modes.append("oauth2")
        if "graph" not in modes:
            modes.insert(0, "graph")

        last_err = ""
        for mode in modes:
            body = {
                "hotmail_email": session.mailbox_address,
                "refresh_token": session.refresh_token or "",
                "client_id": cid,
                "auth_mode": mode,
                "return_all_emails": True,
                "max_messages": int(max_messages or 20),
            }
            try:
                status, data, text = self._post(url, body, timeout=35)
            except Exception as e:
                last_err = str(e)
                log.debug("email_inbox_receiver %s err: %s", mode, e)
                continue

            if not isinstance(data, dict):
                last_err = text[:120]
                continue

            if not data.get("success") and status >= 400:
                last_err = str(data.get("error") or text)[:160]
                log.debug("email_inbox_receiver %s: %s", mode, last_err)
                continue

            # success or soft-fail with emails still present
            emails = data.get("emails") or data.get("messages") or []
            if not emails and not data.get("success"):
                last_err = str(data.get("error") or "no emails")[:160]
                continue

            otp = _otp_from_mail_payload(
                {"emails": emails} if isinstance(emails, list) else data,
                self.otp_regex,
                since_iso=since_iso,
                ignore_ids=ignore_ids,
                strict_xai=True,
            )
            if otp:
                log.info(
                    "OTP via email-inbox-receiver (%s): %s",
                    mode,
                    otp,
                )
                return otp

            # no xAI code yet — not an error, just empty for this poll
            log.debug(
                "email_inbox_receiver %s: %s mails, no new xAI code yet",
                mode,
                len(emails) if isinstance(emails, list) else 0,
            )
            # graph mode returned inbox; no need try oauth2 if success
            if data.get("success"):
                return None

        if last_err:
            log.debug("email_inbox_receiver failed: %s", last_err)
        return None

    def _ms_graph_access(self, session: EmailSession) -> Optional[str]:
        rt = session.refresh_token or ""
        if not rt:
            return None
        cid = self._client_id(session)
        for token_url in (
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        ):
            for scope in (
                "https://graph.microsoft.com/Mail.Read offline_access openid profile",
                "https://graph.microsoft.com/.default offline_access",
            ):
                try:
                    r = requests.post(
                        token_url,
                        data={
                            "client_id": cid,
                            "grant_type": "refresh_token",
                            "refresh_token": rt,
                            "scope": scope,
                        },
                        timeout=20,
                    )
                    if r.status_code == 200:
                        access = r.json().get("access_token")
                        if access:
                            return access
                except Exception:
                    continue
        return None

    def snapshot_inbox_ids(self, session: EmailSession) -> set[str]:
        """
        Message IDs already in inbox BEFORE signup submit — ignore these for OTP.
        Prefer email-inbox-receiver (same IDs as poll), fallback MS Graph.
        """
        ids: set[str] = set()
        # 1) vercel inbox receiver
        try:
            base = "https://email-inbox-receiver.vercel.app"
            for pcfg in self.providers:
                if str(pcfg.get("name", "")).lower() in (
                    "email_inbox_receiver",
                    "vercel_inbox",
                    "inbox_receiver",
                    "email-inbox-receiver",
                ) and pcfg.get("enabled", True):
                    base = str(pcfg.get("base_url") or base).rstrip("/")
                    break
            url = f"{base}/api/read-inbox"
            body = {
                "hotmail_email": session.mailbox_address,
                "refresh_token": session.refresh_token or "",
                "client_id": self._client_id(session),
                "auth_mode": "graph",
                "return_all_emails": True,
                "max_messages": 30,
            }
            status, data, _ = self._post(url, body, timeout=35)
            if isinstance(data, dict):
                emails = data.get("emails") or data.get("messages") or []
                if isinstance(emails, list):
                    for m in emails:
                        if isinstance(m, dict) and m.get("id"):
                            ids.add(str(m["id"]))
        except Exception as e:
            log.debug("snapshot via inbox-receiver: %s", e)

        # 2) graph fallback
        if not ids:
            access = self._ms_graph_access(session)
            if access:
                try:
                    g = requests.get(
                        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                        headers={"Authorization": f"Bearer {access}"},
                        params={
                            "$top": 30,
                            "$orderby": "receivedDateTime desc",
                            "$select": "id,subject,receivedDateTime",
                        },
                        timeout=20,
                    )
                    if g.status_code == 200:
                        ids = {
                            str(m.get("id"))
                            for m in g.json().get("value", [])
                            if m.get("id")
                        }
                except Exception as e:
                    log.debug("snapshot_graph_ids: %s", e)

        log.info("Baseline inbox snapshot: %s message ids", len(ids))
        return ids

    def snapshot_graph_ids(self, session: EmailSession) -> set[str]:
        """Alias kept for callers."""
        return self.snapshot_inbox_ids(session)

    def _fetch_ms_graph(
        self,
        session: EmailSession,
        *,
        ignore_ids: set[str] | None = None,
        since_iso: str | None = None,
    ) -> Optional[str]:
        """Microsoft Graph direct — newest xAI OTP only."""
        access = self._ms_graph_access(session)
        if not access:
            log.debug("ms_graph: cannot refresh access_token")
            return None

        try:
            g = requests.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
                headers={"Authorization": f"Bearer {access}"},
                params={
                    "$top": 20,
                    "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,bodyPreview,body,from,receivedDateTime",
                },
                timeout=20,
            )
        except Exception as e:
            log.debug("ms_graph messages err: %s", e)
            return None
        if g.status_code != 200:
            log.debug("ms_graph messages HTTP %s", g.status_code)
            return None

        messages = []
        for m in g.json().get("value", []):
            body = (m.get("body") or {}).get("content") or ""
            frm = (m.get("from") or {}).get("emailAddress") or {}
            messages.append(
                {
                    "id": m.get("id") or "",
                    "subject": m.get("subject") or "",
                    "preview": m.get("bodyPreview") or "",
                    "message": body,
                    "content": body,
                    "from": f"{frm.get('name','')} {frm.get('address','')}".strip(),
                    "date": m.get("receivedDateTime") or "",
                }
            )
        # log top subjects for debug
        for m in messages[:3]:
            log.debug(
                "inbox[%s] %s | %s",
                m.get("date"),
                m.get("from"),
                (m.get("subject") or "")[:60],
            )

        otp = _otp_from_mail_payload(
            {"messages": messages},
            self.otp_regex,
            since_iso=since_iso,
            ignore_ids=ignore_ids,
            strict_xai=True,
        )
        if otp:
            log.info("OTP via ms_graph (newest xAI only): %s", otp)
        return otp

    def _fetch_mailgen(self, session: EmailSession, base: str) -> Optional[str]:
        """mailgen.shop — public hotmail reader."""
        base = base.rstrip("/")
        edata = self._email_data_line(session)
        cid = self._client_id(session)
        attempts: list[tuple[str, dict[str, Any]]] = [
            (f"{base}/api/inbox-read", {"emailData": edata, "messageCount": 25}),
            (
                f"{base}/api/get-inbox",
                {
                    "data": f"{session.mailbox_address}|{session.refresh_token or ''}|{cid}|",
                    "mode": "graph",
                },
            ),
            (
                f"{base}/api/get-inbox",
                {
                    "data": f"{session.mailbox_address}|{session.refresh_token or ''}|{cid}|",
                    "mode": "oauth",
                },
            ),
            # some forks accept classic body
            (
                f"{base}/api/graph_messages",
                {
                    "email": session.mailbox_address,
                    "refresh_token": session.refresh_token or "",
                    "client_id": cid,
                },
            ),
        ]
        for url, body in attempts:
            try:
                status, data, text = self._post(url, body, timeout=18)
            except Exception as e:
                log.debug("mailgen %s err: %s", url, e)
                continue
            if status >= 500:
                log.debug("mailgen %s HTTP %s", url, status)
                continue
            if status == 404:
                continue
            if isinstance(data, dict):
                err = str(data.get("error") or data.get("content") or "")
                if err and data.get("success") is False:
                    log.debug("mailgen %s: %s", url.split("/")[-1], err[:100])
                otp = _otp_from_mail_payload(data, self.otp_regex, strict_xai=True)
                if otp:
                    log.info("OTP via mailgen %s: %s", url.split("/")[-1], otp)
                    return otp
            else:
                if _is_xai_mail(text):
                    otp = _extract_otp_strict(text, self.otp_regex)
                    if otp:
                        log.info("OTP via mailgen text: %s", otp)
                        return otp
        return None

    def _fetch_dongvan_compat(
        self, session: EmailSession, bases: list[str], endpoints: list[str] | None = None
    ) -> Optional[str]:
        """Any host that speaks graph_messages / graph_code style API."""
        cid = self._client_id(session)
        email = session.mailbox_address
        rt = session.refresh_token or ""
        pw = session.password
        endpoints = endpoints or [
            "/api/graph_messages",
            "/api/get_messages_oauth2",
            "/api/graph_code",
            "/api/get_code_oauth2",
            "/api/inbox-read",
            "/api/get-inbox",
        ]
        bodies_for = {
            "graph_messages": [
                {"email": email, "refresh_token": rt, "client_id": cid},
                {
                    "email": email,
                    "pass": pw,
                    "password": pw,
                    "refresh_token": rt,
                    "client_id": cid,
                },
                {"emailData": self._email_data_line(session), "messageCount": 25},
            ],
            "get_messages_oauth2": [
                {
                    "email": email,
                    "refresh_token": rt,
                    "client_id": cid,
                    "list_mail": "all",
                }
            ],
            "graph_code": [
                {
                    "email": email,
                    "refresh_token": rt,
                    "client_id": cid,
                    "type": self.type or "all",
                }
            ],
            "get_code_oauth2": [
                {
                    "email": email,
                    "refresh_token": rt,
                    "client_id": cid,
                    "type": self.type or "all",
                }
            ],
            "inbox-read": [
                {"emailData": self._email_data_line(session), "messageCount": 25}
            ],
            "get-inbox": [
                {
                    "data": f"{email}|{rt}|{cid}|",
                    "mode": "graph",
                },
                {
                    "data": f"{email}|{rt}|{cid}|",
                    "mode": "oauth",
                },
            ],
        }
        for base in bases:
            base = base.rstrip("/")
            if not base:
                continue
            for ep in endpoints:
                key = ep.rstrip("/").split("/")[-1]
                bodies = bodies_for.get(key) or [
                    {"email": email, "refresh_token": rt, "client_id": cid}
                ]
                for body in bodies:
                    url = urljoin(base + "/", ep.lstrip("/"))
                    try:
                        status, data, text = self._post(url, body, timeout=15)
                    except Exception as e:
                        log.debug("compat %s err: %s", url, e)
                        continue
                    if status >= 400:
                        continue
                    if isinstance(data, dict):
                        otp = _otp_from_mail_payload(data, self.otp_regex, strict_xai=True)
                        if otp:
                            log.info("OTP via %s %s: %s", base, ep, otp)
                            return otp
                    else:
                        if _is_xai_mail(text):
                            otp = _extract_otp_strict(text, self.otp_regex)
                            if otp:
                                return otp
        return None

    def _fetch_custom(self, session: EmailSession, pcfg: dict[str, Any]) -> Optional[str]:
        base = str(pcfg.get("base_url") or "").rstrip("/")
        if not base:
            return None
        endpoint = str(pcfg.get("endpoint") or pcfg.get("endpoint_get_otp") or "/api/otp")
        method = str(pcfg.get("method") or "POST").upper()
        url = urljoin(base + "/", endpoint.lstrip("/"))
        mapping = self._mapping(session)
        body_tpl = pcfg.get("body") or pcfg.get("params") or {
            "email": "{email}",
            "password": "{password}",
            "refresh_token": "{refresh_token}",
            "client_id": "{client_id}",
        }
        headers = _render_template(dict(pcfg.get("headers") or {}), mapping)
        params = _render_template(body_tpl, mapping)
        try:
            if method == "GET":
                r = requests.get(url, params=params, headers=headers, timeout=20)
            else:
                r = requests.post(url, json=params, headers=headers, timeout=20)
        except Exception as e:
            log.debug("custom mail_api error: %s", e)
            return None
        body = r.text or ""
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            otp = _otp_from_mail_payload(data, self.otp_regex, strict_xai=True)
            if otp:
                return otp
        if _is_xai_mail(body):
            return _extract_otp_strict(body, self.otp_regex)
        return None

    def _run_provider(
        self,
        pcfg: dict[str, Any],
        session: EmailSession,
        *,
        ignore_ids: set[str] | None = None,
        since_iso: str | None = None,
    ) -> Optional[str]:
        name = str(pcfg.get("name") or "custom").lower().strip()
        if not pcfg.get("enabled", True):
            return None

        if name in (
            "email_inbox_receiver",
            "vercel_inbox",
            "inbox_receiver",
            "email-inbox-receiver",
        ):
            return self._fetch_email_inbox_receiver(
                session,
                base_url=str(
                    pcfg.get("base_url")
                    or "https://email-inbox-receiver.vercel.app"
                ),
                auth_mode=str(pcfg.get("auth_mode") or "graph"),
                max_messages=int(pcfg.get("max_messages") or 20),
                ignore_ids=ignore_ids,
                since_iso=since_iso,
            )

        if name in ("ms_graph", "graph_direct", "microsoft_graph"):
            return self._fetch_ms_graph(
                session, ignore_ids=ignore_ids, since_iso=since_iso
            )

        if name in ("mailgen", "mailgen.shop"):
            base = str(pcfg.get("base_url") or "https://mailgen.shop")
            return self._fetch_mailgen(session, base)

        if name in ("generic_graph", "graph", "mailgen_get_inbox"):
            base = str(pcfg.get("base_url") or "https://mailgen.shop")
            endpoints = pcfg.get("endpoints")
            if not isinstance(endpoints, list) or not endpoints:
                endpoints = ["/api/get-inbox", "/api/inbox-read"]
            return self._fetch_dongvan_compat(session, [base], list(endpoints))

        if name in (
            "dongvan_compat",
            "dongvanfb",
            "dongvan",
            "tools.dongvanfb",
            "compat",
        ):
            bases = pcfg.get("base_urls") or pcfg.get("bases")
            if not isinstance(bases, list) or not bases:
                base = str(pcfg.get("base_url") or "")
                bases = [base] if base else []
            endpoints = pcfg.get("endpoints")
            return self._fetch_dongvan_compat(
                session,
                [str(b) for b in bases if b],
                list(endpoints) if isinstance(endpoints, list) else None,
            )

        if name == "custom":
            return self._fetch_custom(session, pcfg)

        # unknown name → treat as custom if base_url set
        if pcfg.get("base_url"):
            return self._fetch_custom(session, pcfg)
        return None

    def fetch_once(
        self,
        session: EmailSession,
        *,
        ignore_ids: set[str] | None = None,
        since_iso: str | None = None,
    ) -> Optional[str]:
        if not self.enabled:
            return None
        active = [p for p in self.providers if p.get("enabled", True)]
        if not active:
            return None

        # Prefer fast external inbox receiver, then direct Graph
        def _prio(p: dict[str, Any]) -> int:
            n = str(p.get("name", "")).lower()
            if n in (
                "email_inbox_receiver",
                "vercel_inbox",
                "inbox_receiver",
                "email-inbox-receiver",
            ):
                return 0
            if n in ("ms_graph", "graph_direct", "microsoft_graph"):
                return 1
            return 2

        ordered = sorted(active, key=_prio)

        for pcfg in ordered:
            name = pcfg.get("name", "?")
            try:
                otp = self._run_provider(
                    pcfg,
                    session,
                    ignore_ids=ignore_ids,
                    since_iso=since_iso,
                )
                if otp:
                    log.info("OTP from provider [%s]: %s", name, otp)
                    return otp
            except Exception as e:
                log.debug("provider %s failed: %s", name, e)
        return None

    def wait_otp(
        self,
        session: EmailSession,
        timeout: int | None = None,
        *,
        ignore_ids: set[str] | None = None,
        since_iso: str | None = None,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        wait_for = timeout if timeout is not None else self.timeout
        active = [p.get("name") for p in self.providers if p.get("enabled", True)]
        deadline = time.time() + wait_for

        # Snapshot old mails so we never reuse stale codes (e.g. Outlook welcome digits)
        base_ids = set(ignore_ids or set())
        if session.provider == "hotmail" and not base_ids:
            base_ids = self.snapshot_graph_ids(session)
        if not since_iso:
            # ISO UTC slightly in the past to allow clock skew
            since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))

        log.info(
            "Waiting NEWEST xAI OTP via %s (max %ss, ignore_ids=%s, since=%s)...",
            active,
            wait_for,
            len(base_ids),
            since_iso,
        )
        if session.provider == "hotmail" and not session.refresh_token:
            log.warning("Hotmail missing refresh_token — external API may fail")

        last_err_log = 0.0
        while time.time() < deadline:
            raise_if_stop()
            try:
                otp = self.fetch_once(
                    session, ignore_ids=base_ids, since_iso=since_iso
                )
                if otp:
                    log.info("OTP found (mail_api newest): %s", otp)
                    return otp
            except StopRequested:
                raise
            except Exception as e:
                now = time.time()
                if now - last_err_log > 15:
                    log.warning("mail_api poll error: %s", e)
                    last_err_log = now
            sleep_interruptible(max(2, self.poll_interval))

        log.warning("mail_api OTP timeout after %ss (no new xAI mail)", wait_for)
        return None



