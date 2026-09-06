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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
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


def random_string(length: int = 12, charset: str | None = None) -> str:
    charset = charset or (string.ascii_letters + string.digits)
    return "".join(random.choices(charset, k=length))


def random_password(length: int = 14) -> str:
    """Strong random password (used only when config.fixed_password is empty)."""
    length = max(12, int(length or 14))
    # Ensure mix of upper/lower/digit/symbol (xAI strength rules)
    upper = random.choice(string.ascii_uppercase)
    lower = random.choice(string.ascii_lowercase)
    digit = random.choice(string.digits)
    sym = random.choice("!@#$%*")
    rest = "".join(
        random.choices(string.ascii_letters + string.digits + "!@#$%*", k=length - 4)
    )
    chars = list(upper + lower + digit + sym + rest)
    random.shuffle(chars)
    return "".join(chars)


def resolve_password(config: dict[str, Any]) -> str:
    """Prefer config.fixed_password (local only); else generate once-strong random."""
    fixed = str(config.get("fixed_password") or "").strip()
    if fixed:
        return fixed
    return random_password(int(config.get("password_length") or 16))


_RECENT_NAMES_PATH = ROOT / "data" / "recent_names.json"
_RECENT_NAMES_MAX = 80
_RECENT_NAMES_LOCK = threading.RLock()


def _load_recent_names() -> list[str]:
    with _RECENT_NAMES_LOCK:
        try:
            if _RECENT_NAMES_PATH.exists():
                data = json.loads(_RECENT_NAMES_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(x) for x in data][-_RECENT_NAMES_MAX:]
        except Exception:
            pass
    return []


def _save_recent_name(full: str) -> None:
    with _RECENT_NAMES_LOCK:
        try:
            recent = _load_recent_names()
            recent.append(full)
            recent = recent[-_RECENT_NAMES_MAX:]
            _RECENT_NAMES_PATH.write_text(
                json.dumps(recent, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass


def random_name(
    first_pool: list[str] | None = None,
    last_pool: list[str] | None = None,
) -> tuple[str, str]:
    """
    Pick a varied First + Last name.
    Avoids first==last and skips pairs used in recent runs (anti-repeat flag risk).
    """
    firsts = [n.strip() for n in (first_pool or FIRST_NAMES) if str(n).strip()]
    lasts = [n.strip() for n in (last_pool or LAST_NAMES) if str(n).strip()]
    if not firsts:
        firsts = list(FIRST_NAMES)
    if not lasts:
        lasts = list(LAST_NAMES)

    with _RECENT_NAMES_LOCK:
        recent = set(_load_recent_names())
        # try several times for a fresh combo
        for _ in range(60):
            first = random.choice(firsts)
            last = random.choice(lasts)
            if first.lower() == last.lower():
                continue
            full = f"{first} {last}"
            if full in recent:
                continue
            _save_recent_name(full)
            return first, last

        # fallback: any non-matching pair
        first = random.choice(firsts)
        last = random.choice([x for x in lasts if x.lower() != first.lower()] or lasts)
        full = f"{first} {last}"
        _save_recent_name(full)
        return first, last


# Set per register_one so save_account can update temp-mail failover stats
_CURRENT_EMAIL_PROVIDER: str = ""
_LEDGER_LOCK = threading.RLock()


def remember_account_time(
    email: str,
    when: str | None = None,
    *,
    overwrite: bool = False,
) -> str:
    """Persist first-seen Sub2/reg time per email (Asia/local wall clock)."""
    em = (email or "").strip().lower()
    if not em or "@" not in em:
        return ""
    stamp = (when or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = Path(__file__).resolve().parents[2] / "data" / "account_times.json"
    with _LEDGER_LOCK:
        data: dict[str, str] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k).lower(): str(v) for k, v in raw.items() if v}
            except Exception:
                data = {}
        if not overwrite and data.get(em):
            return data[em]
        data[em] = stamp
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception:
            pass
    return stamp


def save_account(path: Path, email: str, password: str, status: str) -> None:
    """
    Internal ledger only (source for Google Sheet rebuild).
    User-facing result destination is Google Sheet — not this file.
    """
    with _LEDGER_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{email}|{password}|{status}\n")
    log.debug("Internal ledger → %s | %s", email, status)
    st = str(status or "")
    if st.startswith("added_sub2api") or st == "success" or st.startswith(
        "success_sub2api"
    ):
        try:
            remember_account_time(email)
        except Exception:
            pass
    # Failover learning: OTP lag on azpop → next run prefers wibu (and reverse)
    try:
        prov = (_CURRENT_EMAIL_PROVIDER or "").lower()
        if prov in ("azpopmail", "tmail_wibu"):
            tmr.note_from_status(prov, status)
    except Exception:
        pass


def normalize_otp_for_input(otp: str) -> str:
    """
    xAI mail shows codes as XXX-XXX (e.g. YI2-BKR) but the form input
    often wants alphanumerics only (YI2BKR).
    """
    if not otp:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(otp)).upper()


_OTP_JUNK = {
    "RAWHTML",
    "HTML",
    "HTTPS",
    "HTTP",
    "UTF8",
    "JSON",
    "SCRIPT",
    "STYLES",
    "STYLE",
    "DOCTYPE",
    "CHARSET",
    "WINDOW",
    "DOCUMENT",
}


def is_plausible_xai_otp(otp: str) -> bool:
    """xAI codes are 6 mixed alnum (YI2BKR / SX8T88), not CSS/HTML junk like PER-100."""
    raw = str(otp or "").strip().upper()
    n = normalize_otp_for_input(raw)
    if len(n) != 6:
        return False
    if n in _OTP_JUNK:
        return False
    # 638-944 / 638944 — xAI sometimes sends a 6-digit dashed code.
    if re.fullmatch(r"\d{3}-\d{3}", raw) or (
        re.fullmatch(r"\d{6}", n) and "-" not in raw
    ):
        return True
    if not re.search(r"[A-Z]", n) or not re.search(r"\d", n):
        return False
    # reject PER-100 / UTF-8 style halves that are pure digits or junk words
    if "-" in raw:
        left, _, right = raw.partition("-")
        if left.isalpha() and right.isdigit():
            return False
        if left.isdigit() and right.isalpha():
            return False
    return True


# xAI codes: YI2-BKR, YE8-CQ8, sometimes 4-3 or 2-4 groups; also pure 6 digits
_OTP_DASH_RE = re.compile(r"\b([A-Z0-9]{2,5}-[A-Z0-9]{2,5})\b", re.I)
_OTP_LABEL_DASH_RE = re.compile(
    r"(?:confirmation\s*code|verification\s*code|security\s*code|"
    r"one[\s-]?time(?:\s+(?:security\s+)?)?code|your\s+code(?:\s+is)?|"
    r"validate your email|code)\s*[:\-]?\s*([A-Z0-9]{2,5}-[A-Z0-9]{2,5})",
    re.I | re.S,
)
_OTP_LABEL_DIGITS_RE = re.compile(
    r"(?:confirmation\s*code|verification\s*code|security\s*code|"
    r"one[\s-]?time(?:\s+(?:security\s+)?)?code|your\s+code(?:\s+is)?|"
    r"code|otp|verify)\s*[:\-]?\s*(\d{6})\b",
    re.I | re.S,
)
_OTP_DIGITS_RE = re.compile(r"\b(\d{6})\b")
_OTP_NOISE = re.compile(
    r"^(?:000000|111111|123456|654321|ffffff|0000ff)$",
    re.I,
)


def _clean_mail_text(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    clean = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", clean)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"&[a-z#0-9]+;", " ", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _score_otp_candidate(code: str, context: str) -> int:
    """Higher = better. Prefer dashed alnum near xAI labels over bare digits."""
    c = (code or "").strip().upper()
    ctx = (context or "").lower()
    if not c or _OTP_NOISE.match(c.replace("-", "")):
        return -100
    score = 0
    if re.fullmatch(r"[A-Z0-9]{2,5}-[A-Z0-9]{2,5}", c):
        score += 50
        # classic 3-3 xAI style
        if re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", c):
            score += 15
        # must contain at least one letter (YI2-BKR) — pure digit dash rarer
        if re.search(r"[A-Z]", c):
            score += 20
    elif re.fullmatch(r"\d{6}", c):
        score += 10
    else:
        return -50

    if any(
        h in ctx
        for h in (
            "noreply@x.ai",
            "no-reply@x.ai",
            "accounts.x.ai",
            "confirmation code",
            "verification code",
            "security code",
            "x.ai",
            "spacexai",
            "grok",
        )
    ):
        score += 40
    if "code" in ctx or "verif" in ctx or "confirm" in ctx:
        score += 10
    return score


def extract_otp(text: str, pattern: str | None = None) -> Optional[str]:
    """
    Extract xAI / Grok verification code from mail text/HTML.

    Supports:
      - YI2-BKR / YE8-CQ8 (XXX-XXX)
      - XXXX-XXX / XX-XXXX (2–5 alnum each side)
      - classic 6-digit
      - any of the above near noreply@x.ai / confirmation labels

    Prefers highest-scoring match (dashed alnum + xAI context >> bare digits).
    """
    if not text:
        return None
    clean = _clean_mail_text(text)
    if not clean:
        return None

    found: list[tuple[int, str, int]] = []  # score, code, pos

    def _add(code: str, pos: int, local_ctx: str) -> None:
        code_u = (code or "").strip().upper()
        if not code_u or not is_plausible_xai_otp(code_u):
            return
        # window context around match
        lo = max(0, pos - 80)
        hi = min(len(clean), pos + len(code_u) + 80)
        ctx = (local_ctx or "") + " " + clean[lo:hi]
        sc = _score_otp_candidate(code_u, ctx)
        if sc < 0:
            return
        found.append((sc, code_u, pos))

    # 1) optional user/config pattern first (high priority if matches)
    if pattern:
        try:
            for m in re.finditer(pattern, clean, re.I | re.S):
                g = m.group(1) if m.lastindex else m.group(0)
                _add(g, m.start(), "config_regex")
        except re.error:
            pass

    # 2) labeled dashed codes (best)
    for m in _OTP_LABEL_DASH_RE.finditer(clean):
        _add(m.group(1), m.start(1), m.group(0))

    # 3) any dashed alnum (YI2-BKR etc.)
    for m in _OTP_DASH_RE.finditer(clean):
        _add(m.group(1), m.start(1), "")

    # 4) labeled 6-digit
    for m in _OTP_LABEL_DIGITS_RE.finditer(clean):
        _add(m.group(1), m.start(1), m.group(0))

    # 5) bare 6-digit last
    for m in _OTP_DIGITS_RE.finditer(clean):
        _add(m.group(1), m.start(1), "")

    if not found:
        return None

    # highest score; if tie, later occurrence (often the body code)
    found.sort(key=lambda x: (x[0], x[2]), reverse=True)
    best = found[0]
    log.debug(
        "OTP candidates=%s → pick %s score=%s",
        [(c, s) for s, c, _ in found[:5]],
        best[1],
        best[0],
    )
    return best[1]


def _render_template(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for k, v in mapping.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(value, dict):
        return {k: _render_template(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, mapping) for v in value]
    return value


def _dig_json_otp(data: Any) -> Optional[str]:
    keys = (
        "otp", "code", "verification_code", "verificationCode",
        "pin", "token", "message", "data", "result", "content", "text", "body",
    )
    if isinstance(data, str):
        return extract_otp(data)
    if isinstance(data, (int, float)):
        s = str(int(data))
        return s if re.fullmatch(r"\d{6}", s) else None
    if isinstance(data, dict):
        for k in keys:
            if k in data:
                found = _dig_json_otp(data[k])
                if found:
                    return found
        for v in data.values():
            found = _dig_json_otp(v)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _dig_json_otp(item)
            if found:
                return found
    return None



