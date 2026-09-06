"""
Anti-flag helpers for Grok register tool.
Fingerprint isolation, human-like timing/input, light stealth injects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import string
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("grok-reg")

ROOT = Path(__file__).resolve().parents[2]

# Prefer REAL installed Chrome version — spoofed UA is a top CF fail reason
def _detect_chrome_major() -> str:
    try:
        import re
        import subprocess
        from pathlib import Path

        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        for exe in candidates:
            if not exe.exists():
                continue
            # version folder next to chrome.exe
            parent = exe.parent
            for child in parent.iterdir():
                if child.is_dir() and re.match(r"^\d+\.", child.name):
                    maj = child.name.split(".")[0]
                    if maj.isdigit():
                        return maj
            from grokreg.core import winhide

            r = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                **winhide.kwargs(),
            )
            m = re.search(r"(\d+)\.", (r.stdout or "") + (r.stderr or ""))
            if m:
                return m.group(1)
    except Exception:
        pass
    return "131"


_CHROME_MAJOR = _detect_chrome_major()
# Single realistic UA matching installed major — do NOT randomize across majors
_USER_AGENTS = [
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_MAJOR}.0.0.0 Safari/537.36",
]

_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,vi;q=0.8",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8",
    "en-US,en;q=0.9,fr;q=0.7",
]

# Fallback isolation pool (only when align_tz_to_ip=false)
_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "America/Detroit",
]

# Timezone pools consistent with egress country/region.
# Rule: random WITHIN the IP's plausible region — never America/* on VN IP.
# "mọi nước có thể" = all countries/zones that share the same geo cluster as the IP.
_TZ_BY_COUNTRY: dict[str, list[str]] = {
    # VN IP → random among SE Asia UTC+7 countries (plausible neighbours)
    "VN": [
        "Asia/Ho_Chi_Minh",
        "Asia/Bangkok",
        "Asia/Phnom_Penh",
        "Asia/Vientiane",
        "Asia/Jakarta",  # WIB = UTC+7
        "Asia/Pontianak",
    ],
    "TH": [
        "Asia/Bangkok",
        "Asia/Ho_Chi_Minh",
        "Asia/Phnom_Penh",
        "Asia/Vientiane",
        "Asia/Jakarta",
    ],
    "KH": ["Asia/Phnom_Penh", "Asia/Bangkok", "Asia/Ho_Chi_Minh", "Asia/Vientiane"],
    "LA": ["Asia/Vientiane", "Asia/Bangkok", "Asia/Ho_Chi_Minh", "Asia/Phnom_Penh"],
    "ID": [
        "Asia/Jakarta",
        "Asia/Pontianak",
        "Asia/Makassar",
        "Asia/Jayapura",
        "Asia/Bangkok",
    ],
    "MY": ["Asia/Kuala_Lumpur", "Asia/Singapore", "Asia/Brunei"],
    "SG": ["Asia/Singapore", "Asia/Kuala_Lumpur", "Asia/Brunei"],
    "PH": ["Asia/Manila", "Asia/Singapore", "Asia/Hong_Kong"],
    "CN": ["Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "Asia/Urumqi"],
    "HK": ["Asia/Hong_Kong", "Asia/Shanghai", "Asia/Macau"],
    "TW": ["Asia/Taipei", "Asia/Shanghai", "Asia/Hong_Kong"],
    "JP": ["Asia/Tokyo", "Asia/Osaka", "Asia/Sapporo"],
    "KR": ["Asia/Seoul"],
    "IN": ["Asia/Kolkata", "Asia/Calcutta"],
    "US": [
        "America/New_York",
        "America/Chicago",
        "America/Los_Angeles",
        "America/Denver",
        "America/Phoenix",
        "America/Detroit",
        "America/Indiana/Indianapolis",
        "America/Boise",
    ],
    "CA": [
        "America/Toronto",
        "America/Vancouver",
        "America/Edmonton",
        "America/Winnipeg",
        "America/Halifax",
    ],
    "GB": ["Europe/London", "Europe/Dublin"],
    "DE": ["Europe/Berlin", "Europe/Amsterdam", "Europe/Paris", "Europe/Brussels"],
    "FR": ["Europe/Paris", "Europe/Brussels", "Europe/Berlin", "Europe/Madrid"],
    "NL": ["Europe/Amsterdam", "Europe/Brussels", "Europe/Berlin", "Europe/Paris"],
    "AU": [
        "Australia/Sydney",
        "Australia/Melbourne",
        "Australia/Brisbane",
        "Australia/Perth",
        "Australia/Adelaide",
    ],
    "BR": ["America/Sao_Paulo", "America/Fortaleza", "America/Manaus", "America/Recife"],
    "MX": ["America/Mexico_City", "America/Monterrey", "America/Tijuana", "America/Cancun"],
    "RU": ["Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg"],
    "UA": ["Europe/Kyiv", "Europe/Kiev", "Europe/Warsaw"],
    "PL": ["Europe/Warsaw", "Europe/Berlin", "Europe/Prague"],
}

# Same-offset clusters when country unknown but IP timezone known
_TZ_BY_OFFSET_CLUSTER: dict[str, list[str]] = {
    "Asia/Ho_Chi_Minh": _TZ_BY_COUNTRY["VN"],
    "Asia/Saigon": _TZ_BY_COUNTRY["VN"],
    "Asia/Bangkok": _TZ_BY_COUNTRY["TH"],
    "Asia/Jakarta": _TZ_BY_COUNTRY["ID"],
    "Asia/Singapore": _TZ_BY_COUNTRY["SG"],
    "Asia/Shanghai": _TZ_BY_COUNTRY["CN"],
    "Asia/Tokyo": _TZ_BY_COUNTRY["JP"],
    "Asia/Seoul": _TZ_BY_COUNTRY["KR"],
    "Asia/Kolkata": _TZ_BY_COUNTRY["IN"],
    "America/New_York": [
        "America/New_York",
        "America/Detroit",
        "America/Indiana/Indianapolis",
        "America/Toronto",
    ],
    "America/Chicago": ["America/Chicago", "America/Winnipeg", "America/Mexico_City"],
    "America/Denver": ["America/Denver", "America/Boise", "America/Edmonton"],
    "America/Los_Angeles": [
        "America/Los_Angeles",
        "America/Vancouver",
        "America/Tijuana",
        "America/Phoenix",
    ],
    "America/Phoenix": ["America/Phoenix", "America/Denver", "America/Los_Angeles"],
    "Europe/London": ["Europe/London", "Europe/Dublin"],
    "Europe/Berlin": [
        "Europe/Berlin",
        "Europe/Paris",
        "Europe/Amsterdam",
        "Europe/Brussels",
        "Europe/Warsaw",
    ],
    "Europe/Paris": [
        "Europe/Paris",
        "Europe/Berlin",
        "Europe/Amsterdam",
        "Europe/Brussels",
        "Europe/Madrid",
    ],
    "Australia/Sydney": [
        "Australia/Sydney",
        "Australia/Melbourne",
        "Australia/Brisbane",
    ],
}

# Language pools aligned with country (random pick when isolating)
_LANG_BY_COUNTRY: dict[str, list[str]] = {
    "VN": [
        "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "vi-VN,vi;q=0.9,en;q=0.8",
        "en-US,en;q=0.9,vi;q=0.8",
        "en-US,en;q=0.9",
    ],
    "TH": [
        "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
        "en-US,en;q=0.9,th;q=0.8",
        "en-US,en;q=0.9",
    ],
    "KH": ["km-KH,km;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9"],
    "LA": ["lo-LA,lo;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9"],
    "ID": [
        "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "en-US,en;q=0.9,id;q=0.8",
        "en-US,en;q=0.9",
    ],
    "MY": ["ms-MY,ms;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,ms;q=0.8"],
    "SG": ["en-SG,en;q=0.9,zh-CN;q=0.8", "en-US,en;q=0.9"],
    "PH": ["en-PH,en;q=0.9,fil;q=0.8", "en-US,en;q=0.9"],
    "US": ["en-US,en;q=0.9", "en-US,en;q=0.9,es;q=0.7"],
    "CA": ["en-CA,en;q=0.9,fr-CA;q=0.8", "en-US,en;q=0.9"],
    "GB": ["en-GB,en;q=0.9", "en-GB,en;q=0.9,en-US;q=0.8"],
    "DE": ["de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,de;q=0.8"],
    "FR": ["fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,fr;q=0.8"],
    "JP": ["ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,ja;q=0.8"],
    "KR": ["ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,ko;q=0.8"],
    "CN": ["zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,zh-CN;q=0.8"],
    "TW": ["zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9"],
    "HK": ["zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9"],
    "AU": ["en-AU,en;q=0.9", "en-AU,en;q=0.9,en-US;q=0.8"],
    "IN": ["en-IN,en;q=0.9,hi;q=0.8", "en-US,en;q=0.9"],
    "BR": ["pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,pt-BR;q=0.8"],
    "MX": ["es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,es;q=0.8"],
    "NL": ["nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,nl;q=0.8"],
    "PL": ["pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,pl;q=0.8"],
    "RU": ["ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,ru;q=0.8"],
    "UA": ["uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7", "en-US,en;q=0.9"],
}

_VIEWPORTS = [
    (1920, 1080),
    (1536, 864),
    (1440, 900),
    (1366, 768),
    (1600, 900),
    (1280, 720),
    (1680, 1050),
]

_WEBGL_RENDERERS = [
    "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Super Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)",
    "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0)",
]


def _tz_pool_for_egress(env: dict[str, Any]) -> list[str]:
    """
    All timezones plausible for this egress IP.
    VN IP → SE Asia UTC+7 cluster (VN/TH/KH/LA/ID…), never America/*.
    US IP → US metro zones, etc.
    """
    country = str(env.get("country") or "").upper()
    ip_tz = str(env.get("ip_timezone") or "").strip()
    if ip_tz in ("Asia/Saigon",):
        ip_tz = "Asia/Ho_Chi_Minh"

    pool: list[str] = []
    if country and country in _TZ_BY_COUNTRY:
        pool = list(_TZ_BY_COUNTRY[country])
    elif ip_tz and ip_tz in _TZ_BY_OFFSET_CLUSTER:
        pool = list(_TZ_BY_OFFSET_CLUSTER[ip_tz])
    elif ip_tz:
        # Same continent fallback from IANA prefix
        if ip_tz.startswith("Asia/"):
            # Generic Asia: merge nearby common zones + ip_tz itself
            pool = [ip_tz] + [
                z
                for z in (
                    "Asia/Bangkok",
                    "Asia/Ho_Chi_Minh",
                    "Asia/Singapore",
                    "Asia/Jakarta",
                    "Asia/Shanghai",
                    "Asia/Hong_Kong",
                    "Asia/Tokyo",
                    "Asia/Seoul",
                    "Asia/Manila",
                    "Asia/Kuala_Lumpur",
                )
                if z != ip_tz
            ]
        elif ip_tz.startswith("America/"):
            pool = [ip_tz] + list(_TZ_BY_COUNTRY["US"])
        elif ip_tz.startswith("Europe/"):
            pool = [ip_tz] + list(_TZ_BY_COUNTRY["DE"])
        elif ip_tz.startswith("Australia/"):
            pool = list(_TZ_BY_COUNTRY["AU"])
        else:
            pool = [ip_tz]
    elif country == "VN":
        pool = list(_TZ_BY_COUNTRY["VN"])
    else:
        pool = list(_TIMEZONES)

    # Always include exact IP timezone if known (anchor)
    if ip_tz and ip_tz not in pool:
        pool.insert(0, ip_tz)

    # Dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for z in pool:
        if z and z not in seen:
            seen.add(z)
            out.append(z)
    return out or ["UTC"]


def _lang_pool_for_egress(env: dict[str, Any], real_lang: str) -> list[str]:
    country = str(env.get("country") or "").upper()
    pool = list(_LANG_BY_COUNTRY.get(country) or [])
    if real_lang and real_lang not in pool:
        pool.insert(0, real_lang)
    if not pool:
        pool = [real_lang or "en-US,en;q=0.9"]
    return pool


def _same_region(a: str, b: str) -> bool:
    """True if two IANA zones are in the same geo cluster (safe to treat as match)."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Shared pools: if both appear in any country pool together
    for pool in _TZ_BY_COUNTRY.values():
        if a in pool and b in pool:
            return True
    for pool in _TZ_BY_OFFSET_CLUSTER.values():
        if a in pool and b in pool:
            return True
    # Classic UTC+7 aliases
    se7 = {
        "Asia/Ho_Chi_Minh",
        "Asia/Saigon",
        "Asia/Bangkok",
        "Asia/Phnom_Penh",
        "Asia/Vientiane",
        "Asia/Jakarta",
        "Asia/Pontianak",
    }
    if a in se7 and b in se7:
        return True
    return False


def human_delay(lo: float = 1.5, hi: float = 4.5) -> float:
    """Non-uniform delay (beta-ish via triangular) — not perfectly even."""
    lo = float(lo)
    hi = float(hi)
    if hi < lo:
        lo, hi = hi, lo
    # bias slightly toward middle-low (humans pause irregularly)
    mid = lo + (hi - lo) * random.uniform(0.25, 0.55)
    return random.triangular(lo, hi, mid)


async def asleep(lo: float = 1.5, hi: float = 4.5, *, label: str = "") -> None:
    """Human delay; aborts quickly if user pressed ESC / STOP."""
    sec = human_delay(lo, hi)
    if label:
        log.debug("human_delay %.2fs (%s)", sec, label)
    try:
        from grokreg.core.stop_control import interruptible_sleep, raise_if_stop

        raise_if_stop()
        await interruptible_sleep(sec)
    except ImportError:
        import asyncio

        await asyncio.sleep(sec)


def short_pause(lo: float = 0.35, hi: float = 1.1) -> float:
    return human_delay(lo, hi)


def inter_account_cooldown(lo: float = 45.0, hi: float = 90.0) -> float:
    """Between successful accounts — irregular 45–90s."""
    return human_delay(lo, hi)


_GEO_CACHE: dict[str, Any] | None = None
_REAL_ENV_CACHE: dict[str, Any] | None = None


def _detect_egress_geo() -> dict[str, Any]:
    """Real public IP / country / timezone of current egress (no spoof)."""
    global _GEO_CACHE
    if _GEO_CACHE is not None:
        return dict(_GEO_CACHE)
    out: dict[str, Any] = {}
    try:
        import urllib.request

        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
        out = {
            "ip": str(data.get("ip") or "").strip(),
            "country": str(data.get("country") or "").strip().upper(),
            "timezone": str(data.get("timezone") or "").strip(),
            "city": str(data.get("city") or "").strip(),
            "org": str(data.get("org") or "").strip(),
        }
    except Exception as e:
        log.debug("egress geo detect failed: %s", e)
    _GEO_CACHE = out
    if out.get("ip"):
        log.info(
            "Egress geo (real): ip=%s country=%s tz=%s city=%s",
            out.get("ip"),
            out.get("country") or "?",
            out.get("timezone") or "?",
            out.get("city") or "?",
        )
    return dict(out)


def _detect_os_timezone() -> str:
    """OS local timezone name (Windows / IANA). Prefer real machine clock."""
    # 1) zoneinfo / tzlocal style
    try:
        import time as _time

        name = str(getattr(_time, "tzname", ("", ""))[0] or "")
        # Windows often returns "SE Asia Standard Time" etc — map common ones
        win_map = {
            "SE Asia Standard Time": "Asia/Bangkok",
            "SE Asia Daylight Time": "Asia/Bangkok",
            "Indochina Time": "Asia/Ho_Chi_Minh",
            "Pacific Standard Time": "America/Los_Angeles",
            "Pacific Daylight Time": "America/Los_Angeles",
            "Eastern Standard Time": "America/New_York",
            "Eastern Daylight Time": "America/New_York",
            "Central Standard Time": "America/Chicago",
            "Central Daylight Time": "America/Chicago",
            "UTC": "UTC",
        }
        if name in win_map:
            return win_map[name]
    except Exception:
        pass
    # 2) PowerShell Windows timezone → IANA (best effort)
    try:
        import subprocess

        ps = (
            "[System.TimeZoneInfo]::Local.Id"
        )
        from grokreg.core import winhide

        r = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                ps,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            **winhide.kwargs(),
        )
        win_id = (r.stdout or "").strip()
        win_to_iana = {
            "SE Asia Standard Time": "Asia/Bangkok",  # same offset as HCM
            "Singapore Standard Time": "Asia/Singapore",
            "China Standard Time": "Asia/Shanghai",
            "Tokyo Standard Time": "Asia/Tokyo",
            "Pacific Standard Time": "America/Los_Angeles",
            "Eastern Standard Time": "America/New_York",
            "Central Standard Time": "America/Chicago",
            "Mountain Standard Time": "America/Denver",
            "UTC": "UTC",
            "GMT Standard Time": "Europe/London",
            "W. Europe Standard Time": "Europe/Berlin",
        }
        # Vietnam commonly set to SE Asia Standard Time (UTC+7)
        if win_id in win_to_iana:
            # Prefer Ho Chi Minh label when country looks VN later
            return win_to_iana[win_id]
        if win_id:
            log.debug("Windows TZ id (unmapped): %s", win_id)
    except Exception:
        pass
    # 3) UTC offset fallback → coarse IANA
    try:
        import time as _time

        # local = UTC + offset; offset west of UTC is negative on some platforms
        if _time.daylight:
            offset_sec = -_time.altzone
        else:
            offset_sec = -_time.timezone
        hours = int(round(offset_sec / 3600))
        by_offset = {
            7: "Asia/Ho_Chi_Minh",
            8: "Asia/Singapore",
            9: "Asia/Tokyo",
            0: "UTC",
            1: "Europe/Berlin",
            -5: "America/New_York",
            -6: "America/Chicago",
            -7: "America/Denver",
            -8: "America/Los_Angeles",
        }
        if hours in by_offset:
            return by_offset[hours]
    except Exception:
        pass
    return ""


def _detect_real_screen() -> tuple[int, int]:
    """Primary monitor resolution (real). Fallback 1920x1080."""
    # Windows via PowerShell
    try:
        import subprocess

        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$s=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
            "Write-Output ($s.Width.ToString() + 'x' + $s.Height.ToString())"
        )
        from grokreg.core import winhide

        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=6,
            **winhide.kwargs(),
        )
        text = (r.stdout or "").strip().splitlines()
        for line in text:
            if "x" in line.lower():
                a, b = line.lower().split("x", 1)
                w, h = int(a.strip()), int(b.strip())
                if 800 <= w <= 7680 and 600 <= h <= 4320:
                    return w, h
    except Exception:
        pass
    # Linux / fallback env
    try:
        import os

        if os.environ.get("RESOLUTION"):
            a, b = os.environ["RESOLUTION"].lower().split("x", 1)
            return int(a), int(b)
    except Exception:
        pass
    return 1920, 1080


def _detect_cpu_count() -> int:
    try:
        import os

        n = int(os.cpu_count() or 8)
        return max(2, min(32, n))
    except Exception:
        return 8


def _detect_os_lang() -> str:
    """Accept-Language style string from real OS locale — do not invent exotic mixes."""

    def _normalize(primary: str) -> str:
        primary = (primary or "").replace("_", "-").strip()
        if not primary or primary.upper() in {"C", "POSIX", "C.UTF-8", "C.UTF8"}:
            return ""
        # strip encoding suffix: en-US.UTF-8
        primary = primary.split(".")[0]
        base = primary.split("-")[0].lower()
        if base == "vi":
            return "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
        if base == "en":
            if "-" not in primary:
                primary = "en-US"
            return f"{primary},en;q=0.9"
        if len(base) == 2:
            return f"{primary},en-US;q=0.8,en;q=0.7"
        return ""

    try:
        import locale

        loc = ""
        try:
            loc = locale.getdefaultlocale()[0] or ""
        except Exception:
            loc = ""
        if not loc:
            try:
                loc = locale.setlocale(locale.LC_ALL, "") or ""
            except Exception:
                loc = ""
        out = _normalize(loc)
        if out:
            return out
    except Exception:
        pass
    try:
        import subprocess

        from grokreg.core import winhide

        r = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "(Get-Culture).Name",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            **winhide.kwargs(),
        )
        name = (r.stdout or "").strip()  # e.g. en-US, vi-VN
        out = _normalize(name)
        if out:
            return out
    except Exception:
        pass
    return "en-US,en;q=0.9"


def detect_real_environment() -> dict[str, Any]:
    """Cache real machine + egress signals (source of truth for low-leak fingerprint)."""
    global _REAL_ENV_CACHE
    if _REAL_ENV_CACHE is not None:
        return dict(_REAL_ENV_CACHE)
    geo = _detect_egress_geo()
    os_tz = _detect_os_timezone()
    ip_tz = str(geo.get("timezone") or "").strip()
    # Canonical VN labels
    if os_tz in ("Asia/Bangkok", "Asia/Saigon") and (geo.get("country") == "VN" or not ip_tz):
        # Prefer Asia/Ho_Chi_Minh when egress is VN (same UTC+7 as Bangkok)
        if geo.get("country") == "VN" or not ip_tz:
            os_tz = "Asia/Ho_Chi_Minh" if geo.get("country") == "VN" else os_tz
    if ip_tz in ("Asia/Saigon",):
        ip_tz = "Asia/Ho_Chi_Minh"
    w, h = _detect_real_screen()
    env = {
        "os_timezone": os_tz,
        "ip_timezone": ip_tz,
        "ip": geo.get("ip") or "",
        "country": geo.get("country") or "",
        "city": geo.get("city") or "",
        "screen_w": w,
        "screen_h": h,
        "cpu": _detect_cpu_count(),
        "language": _detect_os_lang(),
        "platform": "Win32",
    }
    _REAL_ENV_CACHE = env
    log.info(
        "Real env: os_tz=%s ip_tz=%s country=%s screen=%sx%s cpu=%s lang=%s",
        env["os_timezone"] or "?",
        env["ip_timezone"] or "?",
        env["country"] or "?",
        env["screen_w"],
        env["screen_h"],
        env["cpu"],
        (env["language"] or "")[:24],
    )
    return dict(env)


def _choose_timezone(
    env: dict[str, Any],
    *,
    align_to_ip: bool = True,
    randomize_in_region: bool = True,
) -> tuple[str, bool, list[str]]:
    """
    Pick timezone with LEAST leak, optional per-account random inside IP region.

    Returns (tz, need_cdp_override, pool_used).

    - align_tz_to_ip=true (default):
        * Build pool of ALL countries/zones plausible for this IP
          e.g. VN IP → Asia/Ho_Chi_Minh, Bangkok, Phnom_Penh, Vientiane, Jakarta…
        * Random pick from that pool (never America/* on VN).
        * CDP override only when chosen tz ≠ OS tz.
    - align_tz_to_ip=false:
        * Free-world random from _TIMEZONES (US pool) — higher leak risk.
    """
    os_tz = str(env.get("os_timezone") or "").strip()
    ip_tz = str(env.get("ip_timezone") or "").strip()
    if ip_tz in ("Asia/Saigon",):
        ip_tz = "Asia/Ho_Chi_Minh"
    country = str(env.get("country") or "").upper()

    if not align_to_ip:
        pool = list(_TIMEZONES)
        tz = random.choice(pool)
        need = bool(os_tz and not _same_region(os_tz, tz) and os_tz != tz)
        return tz, need, pool

    pool = _tz_pool_for_egress(env)

    if randomize_in_region and len(pool) > 1:
        # Prefer exact IP tz ~40% of the time (anchor), else random neighbour country zone
        if ip_tz and ip_tz in pool and random.random() < 0.4:
            tz = ip_tz
        else:
            tz = random.choice(pool)
    else:
        # Stable: exact IP tz, else first in pool
        tz = ip_tz if ip_tz and ip_tz in pool else (pool[0] if pool else "UTC")

    # CDP override only if OS clock is outside the chosen zone/region
    if os_tz and (os_tz == tz or _same_region(os_tz, tz)):
        need_override = False
        # If OS already in same region, can keep OS label to avoid CDP signal
        # but still return chosen tz for logging; only skip CDP when same or same region
        if os_tz == tz:
            need_override = False
        else:
            # Same region different city → still override so JS timezone matches persona
            need_override = True
    elif os_tz and align_to_ip:
        log.warning(
            "TZ mismatch OS=%s vs pick=%s (ip=%s country=%s) → CDP override",
            os_tz,
            tz,
            ip_tz or "?",
            country or "?",
        )
        need_override = True
    else:
        need_override = bool(os_tz and os_tz != tz)

    return tz, need_override, pool


def pick_fingerprint(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Per-account fingerprint isolation (fresh profile + unique seeds).

    Goals (max anti-flag):
      - Unique viewport / noise seed / profile age every account
      - Timezone: random among ALL countries/zones plausible for egress IP
        (VN IP → SE Asia UTC+7 cluster: VN/TH/KH/LA/ID… — NEVER America/*)
      - Never force fake UA (binary mismatch = CF fail)
      - Hardware/WebGL values are seeds for optional patch_* only (off by default)

    config.antiflag knobs:
      minimal_fingerprint, isolate_profile, align_tz_to_ip,
      randomize_tz_in_region, randomize_lang_in_region, force_ua,
      randomize_hardware, profile_age_days_min/max, patch_*
    """
    cfg = config or {}
    af_cfg = cfg.get("antiflag") if isinstance(cfg.get("antiflag"), dict) else {}
    af_cfg = af_cfg or {}
    minimal = bool(af_cfg.get("minimal_fingerprint", False))
    isolate = bool(af_cfg.get("isolate_profile", True))
    align_to_ip = bool(af_cfg.get("align_tz_to_ip", True))
    rand_tz = bool(af_cfg.get("randomize_tz_in_region", True))
    rand_lang = bool(af_cfg.get("randomize_lang_in_region", True))
    rand_hw = bool(af_cfg.get("randomize_hardware", True))

    env = detect_real_environment()
    sw, sh = int(env.get("screen_w") or 1920), int(env.get("screen_h") or 1080)

    # --- viewport: isolated random window, clamped to real screen ---
    if minimal and not isolate:
        w = max(1100, int(sw * 0.92))
        h = max(700, int(sh * 0.88))
    else:
        w, h = random.choice(_VIEWPORTS)
        w += random.randint(-12, 16)
        h += random.randint(-8, 12)
        w = max(1280, min(1920, w))
        h = max(720, min(1080, h))
    # Never larger than real monitor (impossible window size = leak)
    w = max(1000, min(sw, w))
    h = max(700, min(sh, h))

    # --- timezone: random within IP-plausible country cluster ---
    tz, need_override, tz_pool = _choose_timezone(
        env, align_to_ip=align_to_ip, randomize_in_region=rand_tz
    )

    # --- language: random within same country pool (or real OS) ---
    real_lang = str(env.get("language") or "en-US,en;q=0.9")
    country = str(env.get("country") or "").upper()
    if af_cfg.get("force_en_us"):
        lang = "en-US,en;q=0.9"
    elif rand_lang and align_to_ip:
        lang = random.choice(_lang_pool_for_egress(env, real_lang))
    elif country == "US":
        lang = "en-US,en;q=0.9"
    else:
        lang = real_lang

    # --- hardware: randomize seeds for isolation; only applied if patch_hardware ---
    real_cpu = int(env.get("cpu") or 8)
    if rand_hw and isolate:
        hw = random.choice([4, 6, 8, 12, 16])
        # Prefer values near real CPU so it is not wildly inconsistent if ever exposed
        if abs(hw - real_cpu) > 8:
            hw = real_cpu if real_cpu in (4, 6, 8, 12, 16) else random.choice(
                [c for c in (4, 6, 8, 12, 16) if abs(c - real_cpu) <= 8] or [8]
            )
        mem = random.choice([4, 8, 16])
    else:
        hw = real_cpu
        mem = None if minimal else 8

    age_lo = int(af_cfg.get("profile_age_days_min") or 45)
    age_hi = int(af_cfg.get("profile_age_days_max") or 180)
    if age_hi < age_lo:
        age_lo, age_hi = age_hi, age_lo

    # Spoof patches (navigator/WebGL/Audio) — on when stealth_inject or patch_*
    stealth_on = bool(af_cfg.get("stealth_inject", False))
    patch_hw = bool(af_cfg.get("patch_hardware", stealth_on))
    patch_webgl = bool(af_cfg.get("patch_webgl", stealth_on))
    patch_audio = bool(af_cfg.get("patch_audio", stealth_on))
    patch_canvas = bool(af_cfg.get("patch_canvas", False))
    if (patch_hw or stealth_on) and mem is None:
        mem = random.choice([4, 8, 16])

    webgl_renderer = random.choice(_WEBGL_RENDERERS)
    rl = webgl_renderer.lower()
    if "intel" in rl:
        webgl_vendor = "Google Inc. (Intel)"
    elif "amd" in rl or "radeon" in rl:
        webgl_vendor = "Google Inc. (AMD)"
    else:
        webgl_vendor = "Google Inc. (NVIDIA)"

    fp = {
        "mode": "isolate" if isolate else ("minimal" if minimal else "legacy"),
        "user_agent": None if not af_cfg.get("force_ua") else _USER_AGENTS[0],
        "language": lang,
        "timezone": tz,
        "timezone_cdp_override": bool(
            need_override and af_cfg.get("spoof_timezone", True)
        ),
        "width": w,
        "height": h,
        "screen_w": sw,
        "screen_h": sh,
        "noise_seed": random.randint(1, 2**31 - 1),
        "force_ua": bool(af_cfg.get("force_ua", False)),
        "device_scale": 1,
        "platform": "Win32",
        "hardware_concurrency": hw,
        "device_memory": mem,
        "webgl_renderer": webgl_renderer,
        "webgl_vendor": webgl_vendor,
        "profile_age_days": random.randint(age_lo, age_hi),
        "ip": env.get("ip") or "",
        "country": country,
        "tz_pool": tz_pool,
        "tz_pool_size": len(tz_pool),
        "stealth_full": stealth_on,
        "patch_canvas": patch_canvas,
        "patch_webgl": patch_webgl,
        "patch_audio": patch_audio,
        "patch_hardware": patch_hw,
        "hide_webdriver": bool(af_cfg.get("hide_webdriver", True)),
        "use_swiftshader": bool(af_cfg.get("use_swiftshader", False)),
        "disable_canvas_read": bool(af_cfg.get("disable_canvas_read", True)),
        # default False = block non-proxied UDP (safer isolation)
        "webrtc_allow_non_proxied_udp": bool(
            af_cfg.get("webrtc_allow_non_proxied_udp", False)
        ),
    }
    if fp["force_ua"] and not fp["user_agent"]:
        fp["user_agent"] = _USER_AGENTS[0]

    log.info(
        "Fingerprint(%s): tz=%s (pool=%s/%s) cdp_tz=%s %sx%s (screen %sx%s) "
        "cpu=%s mem=%s lang=%s country=%s ip=%s age=%sd webgl=%s",
        fp["mode"],
        fp["timezone"],
        len(tz_pool),
        ",".join(z.split("/")[-1] for z in tz_pool[:6]),
        fp["timezone_cdp_override"],
        fp["width"],
        fp["height"],
        sw,
        sh,
        hw,
        mem if mem is not None else "-",
        (lang or "")[:28],
        country or "?",
        (fp["ip"] or "?")[:18],
        fp["profile_age_days"],
        (fp.get("webgl_renderer") or "")[:40],
    )
    return fp


def fresh_profile_dir(root: Path | None = None) -> Path:
    """Unique clean Chrome user-data-dir per account."""
    root = root or (ROOT / "chrome_runs")
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    path = root / f"run_{stamp}_{rand}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def to_windows_path(path: Path | str) -> str:
    s = str(path)
    if s.startswith("/mnt/") and len(s) > 6 and s[5] == "/":
        drive = s[5].upper()
        rest = s[7:].replace("/", "\\")
        return f"{drive}:\\{rest}"
    return s


def apply_chrome_fingerprint_args(options: Any, fp: dict[str, Any], add_arg) -> None:
    """
    Per-run Chrome CLI isolation (safe with pydoll — add_arg should swallow duplicates).

    Defaults avoid high-leak flags:
      - no fake UA unless force_ua
      - no swiftshader unless use_swiftshader (software GL = bot signal)
      - WebRTC UDP disabled only when webrtc_non_proxied_udp=true (or proxy in use)
    """
    w, h = int(fp["width"]), int(fp["height"])
    add_arg(options, f"--window-size={w},{h}")

    lang_primary = str(fp.get("language") or "en-US").split(",")[0].strip() or "en-US"
    if lang_primary.upper() in {"C", "POSIX"} or len(lang_primary) < 2:
        lang_primary = "en-US"
    add_arg(options, f"--lang={lang_primary}")
    # Chrome ignores unknown flags; accept-lang is also set via browser prefs
    accept = str(fp.get("language") or "en-US,en;q=0.9")
    if accept.upper().startswith("C"):
        accept = "en-US,en;q=0.9"
    add_arg(options, f"--accept-lang={accept}")

    add_arg(options, "--disable-blink-features=AutomationControlled")
    # NEVER add --no-first-run / --no-default-browser-check here:
    # pydoll ChromiumOptionsManager.add_default_arguments() already sets them
    # and raises ArgumentAlreadyExistsInOptions if present before Chrome().
    add_arg(
        options,
        "--disable-features=IsolateOrigins,site-per-process,AudioServiceOutOfProcess",
    )

    # Canvas fingerprint read often used by bot detectors — optional block
    if fp.get("disable_canvas_read", True):
        add_arg(options, "--disable-reading-from-canvas")

    # Software GL is a known automation signal — only when explicitly enabled
    if fp.get("use_swiftshader"):
        add_arg(options, "--use-gl=swiftshader")
        log.info("Chrome GL=swiftshader (opt-in; can look automated)")

    # WebRTC local IP leak protection (always on for isolation / proxy safety)
    # Set antiflag.webrtc_non_proxied_udp=true only if you intentionally want default WebRTC.
    if not fp.get("webrtc_allow_non_proxied_udp"):
        add_arg(options, "--force-webrtc-ip-handling-policy=disable_non_proxied_udp")

    if fp.get("force_ua") and fp.get("user_agent"):
        add_arg(options, f"--user-agent={fp['user_agent']}")
        log.warning("Forced UA set — ensure it matches installed Chrome major")


def apply_browser_preferences(options: Any, fp: dict[str, Any]) -> None:
    """
    Seed Chrome profile prefs so a fresh user-data-dir looks like a used profile
    (age, locale, permission defaults) instead of brand-new automation profile.
    Safe no-op if options has no browser_preferences.
    """
    now = int(time.time())
    age = int(fp.get("profile_age_days") or 90) * 86400
    install = now - age
    last = now - random.randint(3600, 86400 * 2)
    lang = str(fp.get("language") or "en-US,en;q=0.9")
    # prefs accept_languages wants comma list without q-values ideally
    accept = ",".join(
        x.strip().split(";")[0] for x in lang.split(",") if x.strip()
    ) or "en-US,en"

    prefs = {
        "profile": {
            "created_by_version": f"{_CHROME_MAJOR}.0.0.0",
            "creation_time": str(install),
            "last_engagement_time": str(last),
            "exit_type": random.choice(["Normal", "Normal", "Crashed"]),
            "name": "Person 1",
            "avatar_index": random.randint(0, 26),
            "default_content_setting_values": {
                "notifications": 2,
                "geolocation": 2,
                "media_stream": 2,
            },
        },
        "intl": {"accept_languages": accept},
        "browser": {"check_default_browser": False},
        "safebrowsing": {"enabled": True},
        "autofill": {"enabled": True},
        "dns_prefetching": {"enabled": True},
        "enable_do_not_track": False,
        "webrtc": {
            # complement CLI WebRTC policy
            "ip_handling_policy": "disable_non_proxied_udp",
            "multiple_routes_enabled": False,
            "nonproxied_udp_enabled": False,
        },
    }

    try:
        if hasattr(options, "browser_preferences"):
            # property setter merges shallowly
            options.browser_preferences = prefs
            log.info(
                "Browser prefs applied (profile_age≈%sd lang=%s exit=%s)",
                fp.get("profile_age_days") or 90,
                accept[:20],
                prefs["profile"]["exit_type"],
            )
        elif hasattr(options, "set_accept_languages"):
            options.set_accept_languages(accept)
    except Exception as e:
        log.debug("apply_browser_preferences: %s", e)

    # Optional pydoll helpers
    try:
        if hasattr(options, "block_notifications"):
            options.block_notifications = True
    except Exception:
        pass


# Minimal inject: only hide webdriver.
STEALTH_JS_MINIMAL = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
  } catch (e) {}
  return true;
})()
"""

# Full fingerprint spoof (navigator + WebGL + Audio). Inject AFTER CF only.
# Placeholders: __HW__ __MEM__ __PLATFORM__ __LANGS__ __WEBGL_RENDERER__ __WEBGL_VENDOR__
#               __PATCH_HW__ __PATCH_WEBGL__ __PATCH_AUDIO__ __PATCH_CANVAS__ __HIDE_WD__ __NOISE_SEED__
STEALTH_JS = r"""
(() => {
  const seed = __NOISE_SEED__;
  const mulberry32 = (a) => () => {
    let t = a += 0x6D2B79F5;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
  const rnd = mulberry32(seed);
  const PATCH_HW = __PATCH_HW__;
  const PATCH_CANVAS = __PATCH_CANVAS__;
  const PATCH_WEBGL = __PATCH_WEBGL__;
  const PATCH_AUDIO = __PATCH_AUDIO__;
  const HIDE_WD = __HIDE_WD__;

  if (HIDE_WD) {
    try {
      Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true,
      });
    } catch (e) {}
  }

  // --- navigator hardware / locale / platform ---
  if (PATCH_HW) {
    try {
      Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => __HW__,
        configurable: true,
      });
    } catch (e) {}
    try {
      if (__MEM__ !== null) {
        Object.defineProperty(navigator, 'deviceMemory', {
          get: () => __MEM__,
          configurable: true,
        });
      }
    } catch (e) {}
    try {
      Object.defineProperty(navigator, 'platform', {
        get: () => __PLATFORM__,
        configurable: true,
      });
    } catch (e) {}
    try {
      Object.defineProperty(navigator, 'languages', {
        get: () => __LANGS__,
        configurable: true,
      });
    } catch (e) {}
    try {
      // language mirrors first entry of languages
      const langs = __LANGS__;
      const primary = (langs && langs[0]) ? langs[0] : 'en-US';
      Object.defineProperty(navigator, 'language', {
        get: () => primary,
        configurable: true,
      });
    } catch (e) {}
  }

  // --- optional canvas noise ---
  if (PATCH_CANVAS) {
    try {
      const toDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
          try {
            const { width, height } = this;
            const img = ctx.getImageData(0, 0, Math.min(width, 16), Math.min(height, 16));
            for (let i = 0; i < img.data.length; i += 4) {
              img.data[i] = img.data[i] ^ (rnd() * 3 | 0);
            }
            ctx.putImageData(img, 0, 0);
          } catch (e) {}
        }
        return toDataURL.apply(this, args);
      };
    } catch (e) {}
  }

  // --- WebGL vendor / renderer ---
  if (PATCH_WEBGL) {
    try {
      const renderer = __WEBGL_RENDERER__;
      const vendor = __WEBGL_VENDOR__;
      const patchGetParam = (proto) => {
        if (!proto || !proto.getParameter) return;
        const getParameter = proto.getParameter;
        proto.getParameter = function(param) {
          // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL
          if (param === 37445) return vendor;
          if (param === 37446) return renderer;
          return getParameter.apply(this, arguments);
        };
      };
      if (typeof WebGLRenderingContext !== 'undefined') {
        patchGetParam(WebGLRenderingContext.prototype);
      }
      if (typeof WebGL2RenderingContext !== 'undefined') {
        patchGetParam(WebGL2RenderingContext.prototype);
      }
    } catch (e) {}
  }

  // --- Audio fingerprint noise (sparse steps) ---
  if (PATCH_AUDIO) {
    try {
      const origGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function() {
        const data = origGetChannelData.apply(this, arguments);
        try {
          if (data && data.length) {
            for (let i = 0; i < data.length; i += 100) {
              data[i] += (Math.random() - 0.5) * 0.0001;
            }
          }
        } catch (e) {}
        return data;
      };
    } catch (e) {}
  }

  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
  } catch (e) {}

  return true;
})()
"""


def build_stealth_script(fp: dict[str, Any], *, full: bool = False) -> str:
    """
    Build inject JS.
    full=True or any patch_* → STEALTH_JS (navigator/WebGL/Audio).
    else → minimal webdriver hide only.
    """
    want_full = full or bool(
        fp.get("patch_hardware")
        or fp.get("patch_webgl")
        or fp.get("patch_audio")
        or fp.get("patch_canvas")
        or fp.get("stealth_full")
    )
    if not want_full and fp.get("mode") == "minimal":
        return STEALTH_JS_MINIMAL

    # languages: prefer fp list; user snippet style default ['en-US','en']
    langs = [x.strip().split(";")[0] for x in str(fp.get("language") or "en-US,en").split(",")]
    langs = [x for x in langs if x and x.upper() not in {"C", "POSIX"}] or ["en-US", "en"]
    # ensure at least en fallback like the provided snippet
    if "en" not in langs and not any(x.lower().startswith("en") for x in langs):
        langs = langs + ["en"]

    mem = fp.get("device_memory")
    if mem is None and (fp.get("patch_hardware") or want_full):
        mem = 8

    js = STEALTH_JS
    js = js.replace("__NOISE_SEED__", str(int(fp.get("noise_seed") or 1)))
    js = js.replace("__LANGS__", json.dumps(langs))
    js = js.replace("__PLATFORM__", json.dumps(fp.get("platform") or "Win32"))
    js = js.replace("__HW__", str(int(fp.get("hardware_concurrency") or 8)))
    js = js.replace("__MEM__", "null" if mem is None else str(int(mem)))
    # When full stealth requested, force HW/WebGL/Audio patches on
    patch_hw = bool(fp.get("patch_hardware") or want_full)
    patch_webgl = bool(fp.get("patch_webgl") or want_full)
    patch_audio = bool(fp.get("patch_audio") or want_full)
    patch_canvas = bool(fp.get("patch_canvas", False))
    js = js.replace("__PATCH_HW__", "true" if patch_hw else "false")
    js = js.replace("__PATCH_CANVAS__", "true" if patch_canvas else "false")
    js = js.replace("__PATCH_WEBGL__", "true" if patch_webgl else "false")
    js = js.replace("__PATCH_AUDIO__", "true" if patch_audio else "false")
    js = js.replace("__HIDE_WD__", "true" if fp.get("hide_webdriver", True) else "false")

    renderer = str(
        fp.get("webgl_renderer")
        or "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Super Direct3D11 vs_5_0 ps_5_0)"
    )
    vendor = str(fp.get("webgl_vendor") or "")
    if not vendor:
        rl = renderer.lower()
        if "intel" in rl:
            vendor = "Google Inc. (Intel)"
        elif "amd" in rl or "radeon" in rl:
            vendor = "Google Inc. (AMD)"
        else:
            vendor = "Google Inc. (NVIDIA)"
    js = js.replace("__WEBGL_RENDERER__", json.dumps(renderer))
    js = js.replace("__WEBGL_VENDOR__", json.dumps(vendor))
    return js


async def _cdp_set_timezone(tab: Any, tz: str) -> None:
    if not tz:
        return
    try:
        if hasattr(tab, "execute_command"):
            await tab.execute_command(
                {
                    "method": "Emulation.setTimezoneOverride",
                    "params": {"timezoneId": tz},
                }
            )
            return
        if hasattr(tab, "call"):
            await tab.call("Emulation.setTimezoneOverride", timezoneId=tz)
    except Exception as e:
        log.debug("timezone override: %s", e)


async def inject_stealth(tab: Any, fp: dict[str, Any], exec_js) -> None:
    """
    Inject fingerprint spoof AFTER Cloudflare is clear.
    - patch_hardware / patch_webgl / patch_audio → navigator + WebGL + Audio
    - CDP timezone override when timezone_cdp_override is set
    """
    full = bool(
        fp.get("stealth_full")
        or fp.get("patch_canvas")
        or fp.get("patch_webgl")
        or fp.get("patch_audio")
        or fp.get("patch_hardware")
    )
    try:
        script = build_stealth_script(fp, full=full)
        await exec_js(tab, script)
        log.info(
            "Stealth inject ok full=%s hw=%s webgl=%s audio=%s canvas=%s",
            full,
            bool(fp.get("patch_hardware") or full),
            bool(fp.get("patch_webgl") or full),
            bool(fp.get("patch_audio") or full),
            bool(fp.get("patch_canvas")),
        )
    except Exception as e:
        log.debug("stealth inject: %s", e)

    # Only override TZ when needed — CDP Emulation itself is a detectable signal
    if fp.get("timezone_cdp_override") and fp.get("timezone"):
        await _cdp_set_timezone(tab, str(fp["timezone"]))
        log.info("CDP timezone override applied: %s (OS≠IP)", fp["timezone"])
    else:
        log.debug(
            "No CDP TZ override (using OS clock; tz=%s)",
            fp.get("timezone") or "default",
        )


async def clear_browser_storage(tab: Any, exec_js) -> None:
    """Clear local/session/IndexedDB for the current origin (JS side)."""
    try:
        await exec_js(
            tab,
            """
            (() => {
              try { localStorage.clear(); } catch(e) {}
              try { sessionStorage.clear(); } catch(e) {}
              try {
                if (window.caches && caches.keys) {
                  caches.keys().then(keys => keys.forEach(k => caches.delete(k)));
                }
              } catch(e) {}
              try {
                if (window.indexedDB && indexedDB.databases) {
                  indexedDB.databases().then(dbs => {
                    (dbs||[]).forEach(d => { try { indexedDB.deleteDatabase(d.name); } catch(e) {} });
                  });
                }
              } catch(e) {}
              return true;
            })()
            """,
        )
        log.info("Cleared tab storage (local/session/idb/cache)")
    except Exception as e:
        log.debug("clear storage: %s", e)


async def _cdp_call(target: Any, method: str, params: dict | None = None) -> bool:
    """Best-effort CDP call on tab or browser connection handler."""
    params = params or {}
    try:
        if hasattr(target, "execute_command"):
            await target.execute_command({"method": method, "params": params})
            return True
        if hasattr(target, "call"):
            await target.call(method, **params)
            return True
    except Exception as e:
        log.debug("CDP %s: %s", method, e)
    return False


async def clear_browser_cookies(browser: Any) -> None:
    try:
        if hasattr(browser, "delete_all_cookies"):
            await browser.delete_all_cookies()
            log.info("Cleared all browser cookies")
            return
    except Exception as e:
        log.debug("delete_all_cookies: %s", e)
    # CDP fallback
    try:
        handler = getattr(browser, "_connection_handler", None) or browser
        await _cdp_call(handler, "Network.clearBrowserCookies")
        await _cdp_call(handler, "Network.clearBrowserCache")
        log.info("Cleared cookies/cache via CDP")
    except Exception as e:
        log.debug("CDP cookie clear: %s", e)


def _is_identity_cookie_name(name: str) -> bool:
    """Match competitor clear_identity_cookies / clear_sso_cookies naming."""
    lower = str(name or "").strip().lower()
    if not lower:
        return False
    # KEEP Cloudflare / anti-bot (competitor: never delete these)
    if lower in {"__cf_bm", "cf_clearance"} or lower.startswith("cf_"):
        return False
    identity = {
        "sso",
        "sso-rw",
        "sso_token",
        "sso-token",
        "auth_token",
        "session",
        "sessionid",
    }
    return lower in identity or lower.startswith("sso")


async def _cdp_list_cookies(tab: Any) -> list[dict]:
    """List browser cookies via CDP (competitor: export then filter by name)."""
    cookies: list[dict] = []

    def _extract(raw: Any) -> list[dict]:
        out: list[dict] = []
        if not isinstance(raw, dict):
            if isinstance(raw, list):
                return [c for c in raw if isinstance(c, dict)]
            return out
        # pydoll often wraps: {result: {cookies: [...]}}
        r0 = raw.get("result") if isinstance(raw.get("result"), dict) else raw
        if isinstance(r0, dict):
            cl = r0.get("cookies")
            if isinstance(cl, list):
                out.extend([c for c in cl if isinstance(c, dict)])
        if isinstance(raw.get("cookies"), list):
            out.extend([c for c in raw["cookies"] if isinstance(c, dict)])
        return out

    try:
        from pydoll.commands.network_commands import NetworkCommands

        for urls in (
            [
                "https://accounts.x.ai/",
                "https://x.ai/",
                "https://grok.com/",
                "https://auth.x.ai/",
            ],
            None,
        ):
            try:
                if urls is None:
                    cmd = NetworkCommands.get_cookies()
                else:
                    cmd = NetworkCommands.get_cookies(urls=list(urls))
                raw = await tab._execute_command(cmd)
                cookies.extend(_extract(raw))
            except Exception as e:
                log.debug("get_cookies(%s): %s", urls, e)
    except Exception as e:
        log.debug("NetworkCommands.get_cookies unavailable: %s", e)

    if not cookies:
        try:
            from pydoll.commands.storage_commands import StorageCommands

            raw = await tab._execute_command(StorageCommands.get_cookies())
            cookies.extend(_extract(raw))
        except Exception as e:
            log.debug("Storage.getCookies: %s", e)

    # de-dupe by name|domain|path
    seen: set[str] = set()
    uniq: list[dict] = []
    for c in cookies:
        key = f"{c.get('name')}|{c.get('domain')}|{c.get('path') or '/'}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


async def _cdp_delete_cookie(tab: Any, *, name: str, domain: str = "", path: str = "/") -> bool:
    params: dict[str, Any] = {"name": name, "path": path or "/"}
    if domain:
        params["domain"] = domain
    # Prefer pydoll command object when available
    try:
        if hasattr(tab, "_execute_command"):
            try:
                from pydoll.commands.network_commands import NetworkCommands

                cmd = NetworkCommands.delete_cookies(
                    name=name, domain=domain or None, path=path or "/"
                )
                await tab._execute_command(cmd)
                return True
            except Exception:
                await tab._execute_command(
                    {"method": "Network.deleteCookies", "params": params}
                )
                return True
    except Exception as e:
        log.debug("deleteCookies %s@%s: %s", name, domain, e)
    return await _cdp_call(tab, "Network.deleteCookies", params)


async def clear_sso_identity_only(tab: Any | None = None) -> int:
    """
    Competitor-style guest start (grok-register-web):
      - Remove ONLY identity cookies (sso / sso-rw / sso_*, session, auth_token)
      - KEEP Cloudflare cf_clearance / __cf_bm (Turnstile/Castle survive)
      - Delete by *actual* cookie domain+path from jar (not fixed list only)
      - Soft: clear identity keys in local/sessionStorage on current origin
    Returns number of cookies deleted (best-effort).
    """
    if tab is None:
        return 0

    removed = 0
    # 1) Primary: list real cookies → delete identity by exact domain/path
    #    (competitor D8: multi-domain jar clear; fixed name+domain list alone is incomplete)
    try:
        listed = await _cdp_list_cookies(tab)
        for c in listed:
            name = str(c.get("name") or "")
            if not _is_identity_cookie_name(name):
                continue
            domain = str(c.get("domain") or "")
            path = str(c.get("path") or "/")
            if await _cdp_delete_cookie(tab, name=name, domain=domain, path=path):
                removed += 1
            # Also try with leading-dot variants
            if domain and not domain.startswith("."):
                if await _cdp_delete_cookie(
                    tab, name=name, domain="." + domain.lstrip("."), path=path
                ):
                    removed += 1
        if listed:
            left_id = [
                str(c.get("name"))
                for c in await _cdp_list_cookies(tab)
                if _is_identity_cookie_name(str(c.get("name") or ""))
            ]
            log.info(
                "SSO jar wipe: deleted≈%s leftover_identity=%s",
                removed,
                left_id[:8] or "none",
            )
    except Exception as e:
        log.debug("jar-level identity wipe failed: %s", e)

    # 2) Fallback fixed domains (competitor clear_sso_cookies)
    names = ("sso", "sso-rw", "sso_token", "sso-token", "auth_token", "session", "sessionid")
    domains = (
        ".x.ai",
        "x.ai",
        "accounts.x.ai",
        "auth.x.ai",
        ".auth.x.ai",
        ".grok.com",
        "grok.com",
        "www.grok.com",
    )
    for name in names:
        for domain in domains:
            try:
                if await _cdp_delete_cookie(tab, name=name, domain=domain, path="/"):
                    removed += 1
            except Exception:
                pass

    # 3) Soft origin cookie clear on accounts.x.ai only (competitor protocol soft reset)
    #    Does NOT clear CF on .x.ai parent if cookie host-only on accounts — still try.
    try:
        await _cdp_call(
            tab,
            "Storage.clearDataForOrigin",
            {
                "origin": "https://accounts.x.ai",
                "storageTypes": "cookies",
            },
        )
    except Exception:
        pass

    # 4) local/sessionStorage identity keys (not full wipe — keep device signals)
    try:
        from grokreg.browser.jsutil import _exec_js

        await _exec_js(
            tab,
            """
            (() => {
              let n = 0;
              const drop = (store) => {
                try {
                  for (let i = store.length - 1; i >= 0; i--) {
                    const k = store.key(i) || '';
                    if (/sso|session|auth|token|user|account|oauth|login/i.test(k)) {
                      store.removeItem(k); n++;
                    }
                  }
                } catch(e) {}
              };
              drop(localStorage);
              drop(sessionStorage);
              return n;
            })()
            """,
        )
    except Exception:
        try:
            from grokreg.delivery.sub2api_oauth import js

            await js(
                tab,
                """
                (() => {
                  try { localStorage.clear(); } catch(e) {}
                  try { sessionStorage.clear(); } catch(e) {}
                  return 1;
                })()
                """,
            )
        except Exception:
            pass

    log.info(
        "SSO-only wipe done (identity cookies, kept cf_clearance) deleted≈%s",
        removed,
    )
    return removed


async def ensure_guest_session(tab: Any, exec_js=None) -> dict[str, Any]:
    """
    Competitor soft reset before signup:
      1) clear identity cookies (keep CF)
      2) open signup origin
      3) if still logged-in UI → wipe again + hard navigate signup
    """
    from grokreg.browser.jsutil import _exec_js as _ej

    ej = exec_js or _ej
    result: dict[str, Any] = {"wiped": 0, "logged_in_after": False, "href": ""}

    result["wiped"] = await clear_sso_identity_only(tab)

    signup_urls = (
        "https://accounts.x.ai/sign-up",
        "https://accounts.x.ai/",
        "https://grok.com/",
    )
    for url in signup_urls[:1]:
        try:
            await tab.go_to(url)
            await asyncio.sleep(0.5)
            break
        except Exception:
            continue

    # Detect leftover SSO cookie
    try:
        left = [
            c
            for c in await _cdp_list_cookies(tab)
            if _is_identity_cookie_name(str(c.get("name") or ""))
        ]
        if left:
            log.warning(
                "Identity cookies still present after wipe: %s — second pass",
                [c.get("name") for c in left[:6]],
            )
            result["wiped"] += await clear_sso_identity_only(tab)
    except Exception:
        pass

    # Detect logged-in UI (competitor would not start signup on /account dashboard)
    try:
        from grokreg.browser.page_flow import page_is_logged_in

        still = await page_is_logged_in(tab)
        result["logged_in_after"] = bool(still)
        if still:
            log.warning("Still logged in after SSO wipe — force re-wipe + signup URL")
            result["wiped"] += await clear_sso_identity_only(tab)
            try:
                await tab.go_to("https://accounts.x.ai/sign-up")
                await asyncio.sleep(0.6)
            except Exception:
                pass
            result["logged_in_after"] = bool(await page_is_logged_in(tab))
    except Exception as e:
        log.debug("ensure_guest logged_in check: %s", e)

    try:
        href = await ej(tab, "location.href")
        result["href"] = str(href or "")[:160]
    except Exception:
        pass

    log.info(
        "ensure_guest_session wiped≈%s still_logged_in=%s href=%s",
        result["wiped"],
        result["logged_in_after"],
        result["href"],
    )
    return result


async def clear_browser_session(
    browser: Any,
    tab: Any | None = None,
    exec_js=None,
    *,
    clear_cookies: bool = True,
    clear_storage: bool = True,
    sso_only: bool = False,
) -> None:
    """
    Session wipe before signup.

    sso_only=True (recommended / competitor): only drop identity cookies,
    preserve Cloudflare clearance + device state.

    Full wipe: all cookies + HTTP cache + storage (harder on Turnstile/Castle).
    """
    if sso_only:
        await clear_sso_identity_only(tab)
        return
    if clear_cookies:
        await clear_browser_cookies(browser)
        # Extra CDP wipe when available on tab
        if tab is not None:
            await _cdp_call(tab, "Network.clearBrowserCookies")
            await _cdp_call(tab, "Network.clearBrowserCache")
            await _cdp_call(tab, "Network.setCacheDisabled", {"cacheDisabled": True})
    if clear_storage and tab is not None and exec_js is not None:
        await clear_browser_storage(tab, exec_js)
        # Wipe common xAI / Grok origins if CDP Storage is available
        for origin in (
            "https://accounts.x.ai",
            "https://x.ai",
            "https://grok.com",
            "https://www.grok.com",
        ):
            await _cdp_call(
                tab,
                "Storage.clearDataForOrigin",
                {
                    "origin": origin,
                    "storageTypes": "cookies,local_storage,indexeddb,cache_storage,service_workers",
                },
            )


async def human_mouse_jiggle(tab: Any, exec_js) -> None:
    """Small random mouse moves + light scroll (JS Pointer/Mouse events)."""
    try:
        await exec_js(
            tab,
            f"""
            (() => {{
              const w = window.innerWidth || 1200;
              const h = window.innerHeight || 800;
              const n = 2 + Math.floor(Math.random() * 3);
              for (let i = 0; i < n; i++) {{
                const x = Math.floor(w * (0.15 + Math.random() * 0.7));
                const y = Math.floor(h * (0.15 + Math.random() * 0.7));
                const t = document.elementFromPoint(x, y) || document.body;
                ['mousemove','pointermove'].forEach(type => {{
                  t.dispatchEvent(new MouseEvent(type, {{
                    bubbles: true, clientX: x, clientY: y, view: window
                  }}));
                }});
              }}
              const dy = (Math.random() < 0.5 ? 1 : -1) * (40 + Math.floor(Math.random() * 120));
              window.scrollBy({{ top: dy, left: 0, behavior: 'smooth' }});
              return true;
            }})()
            """,
        )
    except Exception as e:
        log.debug("mouse jiggle: %s", e)


async def human_pre_click(tab: Any, exec_js, asleep_fn) -> None:
    """Jiggle + short pause before important clicks."""
    await human_mouse_jiggle(tab, exec_js)
    await asleep_fn(0.4, 1.2, label="pre_click")


# ---------------------------------------------------------------------------
# Hotmail recent-fail cooldown / azpop domain stats
# ---------------------------------------------------------------------------

FAIL_COOLDOWN_PATH = ROOT / "data" / "mail_fail_cooldown.json"
DOMAIN_STATS_PATH = ROOT / "data" / "azpop_domain_stats.json"


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def mark_mail_fail(email: str, minutes: int = 120, reason: str = "") -> None:
    data = _load_json(FAIL_COOLDOWN_PATH)
    data[email.lower()] = {
        "until": time.time() + minutes * 60,
        "reason": reason[:120],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_json(FAIL_COOLDOWN_PATH, data)


def is_mail_in_fail_cooldown(email: str) -> tuple[bool, int]:
    data = _load_json(FAIL_COOLDOWN_PATH)
    rec = data.get(email.lower())
    if not rec:
        return False, 0
    left = int(rec.get("until", 0) - time.time())
    if left <= 0:
        return False, 0
    return True, left


# Recent domain picks — rotate so xAI does not see one domain spam
DOMAIN_RECENT_PATH = ROOT / "data" / "azpop_domain_recent.json"
DOMAIN_RECENT_MAX = 12
# Soft-fail window: OTP fail/slow in last N seconds → heavy penalty
DOMAIN_FAIL_COOLDOWN_SEC = 25 * 60
# Do not reuse same domain within last K picks if alternatives exist
DOMAIN_AVOID_LAST_N = 4


def mark_domain_otp(domain: str, *, ok: bool, elapsed: float, reason: str = "") -> None:
    domain = (domain or "").strip().lower()
    if not domain:
        return
    data = _load_json(DOMAIN_STATS_PATH)
    d = data.get(domain) or {
        "ok": 0,
        "fail": 0,
        "avg_ok_sec": 30.0,
        "last": 0,
        "last_ok": 0,
        "last_fail": 0,
        "streak_fail": 0,
    }
    now = time.time()
    if ok:
        d["ok"] = int(d.get("ok") or 0) + 1
        prev = float(d.get("avg_ok_sec") or 30)
        d["avg_ok_sec"] = round(prev * 0.7 + float(elapsed) * 0.3, 1)
        d["last_ok"] = now
        d["streak_fail"] = 0
        d.pop("hard_ban_until", None)
    else:
        d["fail"] = int(d.get("fail") or 0) + 1
        d["last_fail"] = now
        d["streak_fail"] = int(d.get("streak_fail") or 0) + 1
        if reason:
            d["last_fail_reason"] = str(reason)[:80]
        r = (reason or "").lower()
        # Explicit domain rejection by xAI → hard-ban 6h (true disposable block)
        hard_markers = (
            "disposable",
            "not allowed",
            "blocked domain",
            "email_rejected",
            "invalid email domain",
            "domain not supported",
            "use a different email",
        )
        # IP/session-level generic ("Something went wrong") is NOT domain-specific.
        # Hard-banning every domain for 6h after one generic fail poisons ranking
        # (mailhiha/mailbanvia historically best, then all banned → worse domains).
        ip_level = any(
            x in r
            for x in (
                "error_generic",
                "something went wrong",
                "email_submit",
                "no_otp_page",
                "try again",
            )
        )
        if any(x in r for x in hard_markers):
            d["hard_ban_until"] = now + 6 * 3600
            d["hard_ban_reason"] = str(reason)[:80]
            log.info("Domain hard-ban 6h: %s (%s)", domain, reason[:60])
        elif ip_level:
            # soft cooldown only (ranker already penalizes last_fail + streak)
            d["soft_ban_until"] = now + 20 * 60
            d["soft_ban_reason"] = str(reason)[:80]
            # clear mistaken hard bans from older builds
            if d.get("hard_ban_reason") and any(
                x in str(d.get("hard_ban_reason") or "").lower()
                for x in ("error_generic", "something went wrong", "email_submit", "no_otp_page")
            ):
                d.pop("hard_ban_until", None)
            log.info("Domain soft-ban 20m (IP-level): %s (%s)", domain, reason[:60])
    d["last"] = now
    data[domain] = d
    _save_json(DOMAIN_STATS_PATH, data)


def ban_domain(
    domain: str,
    hours: float = 6.0,
    reason: str = "",
    *,
    soft: bool | None = None,
) -> None:
    """
    Ban a domain. For IP-level generic errors use soft=True (or omit hours intent).
    soft=None → auto-detect from reason (error_generic → soft 20m).
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return
    r = (reason or "").lower()
    if soft is None:
        soft = any(
            x in r
            for x in (
                "error_generic",
                "something went wrong",
                "email_submit",
                "no_otp_page",
                "try again",
            )
        ) and not any(
            x in r
            for x in (
                "disposable",
                "not allowed",
                "blocked domain",
                "email_rejected",
            )
        )
    if soft:
        # mark_domain_otp handles soft_ban_until for ip-level reasons
        mark_domain_otp(domain, ok=False, elapsed=0, reason=reason or "soft_ban")
        return
    # true hard ban with custom hours
    data = _load_json(DOMAIN_STATS_PATH)
    d = data.get(domain) or {}
    now = time.time()
    d["fail"] = int(d.get("fail") or 0) + 1
    d["last_fail"] = now
    d["streak_fail"] = int(d.get("streak_fail") or 0) + 1
    d["hard_ban_until"] = now + float(hours) * 3600
    d["hard_ban_reason"] = str(reason or "manual_ban")[:80]
    d["last"] = now
    if reason:
        d["last_fail_reason"] = str(reason)[:80]
    data[domain] = d
    _save_json(DOMAIN_STATS_PATH, data)
    log.info("Domain hard-ban %.1fh: %s (%s)", hours, domain, (reason or "")[:60])


def clear_ip_level_hard_bans() -> int:
    """
    One-shot cleanup: remove hard_ban_until entries that were wrongly set from
    error_generic / Something went wrong (IP-level, not domain block).
    Returns count cleared.
    """
    data = _load_json(DOMAIN_STATS_PATH)
    n = 0
    for dom, s in list(data.items()):
        if not isinstance(s, dict):
            continue
        reason = str(s.get("hard_ban_reason") or s.get("last_fail_reason") or "").lower()
        if s.get("hard_ban_until") and any(
            x in reason
            for x in (
                "error_generic",
                "something went wrong",
                "email_submit",
                "no_otp_page",
                "try again",
            )
        ):
            s.pop("hard_ban_until", None)
            s["soft_ban_until"] = time.time() + 10 * 60
            data[dom] = s
            n += 1
    if n:
        _save_json(DOMAIN_STATS_PATH, data)
        log.info("Cleared %s IP-level hard-bans on domains (kept soft cooldown)", n)
    return n


def mark_domain_used(domain: str) -> None:
    """Record that we just registered with this domain (anti-flag rotation)."""
    domain = (domain or "").strip().lower()
    if not domain:
        return
    data = _load_json(DOMAIN_RECENT_PATH)
    recent = list(data.get("recent") or [])
    recent.append({"domain": domain, "at": time.time()})
    recent = recent[-DOMAIN_RECENT_MAX:]
    data["recent"] = recent
    data["last"] = domain
    _save_json(DOMAIN_RECENT_PATH, data)
    # also bump last_used on stats
    stats = _load_json(DOMAIN_STATS_PATH)
    d = stats.get(domain) or {}
    d["last_used"] = time.time()
    d["used"] = int(d.get("used") or 0) + 1
    stats[domain] = d
    _save_json(DOMAIN_STATS_PATH, stats)


def _recent_domains(n: int = DOMAIN_AVOID_LAST_N) -> list[str]:
    data = _load_json(DOMAIN_RECENT_PATH)
    recent = list(data.get("recent") or [])
    out: list[str] = []
    for item in reversed(recent):
        if isinstance(item, dict):
            d = str(item.get("domain") or "").lower()
        else:
            d = str(item or "").lower()
        if d and d not in out:
            out.append(d)
        if len(out) >= n:
            break
    return out


def rank_domains(candidates: list[str]) -> list[str]:
    """
    Rank domains for OTP + anti-flag diversity.

    Goals:
      - avoid hammering one domain (mailhiha spam → flag risk)
      - deprioritize recent OTP fail/slow hard for ~25 min
      - still prefer historically healthy domains, but with exploration
      - rotate: last N used domains go to the bottom if alternatives exist
    """
    cands = [str(d).strip().lower() for d in candidates if str(d).strip()]
    # dedupe keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for d in cands:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    if not uniq:
        return []

    stats = _load_json(DOMAIN_STATS_PATH)
    recent = _recent_domains(DOMAIN_AVOID_LAST_N)
    recent_set = set(recent)
    now = time.time()
    scored: list[tuple[float, str]] = []

    for dom in uniq:
        s = stats.get(dom) or {}
        ok = int(s.get("ok") or 0)
        fail = int(s.get("fail") or 0)
        avg = float(s.get("avg_ok_sec") or 40)
        used = int(s.get("used") or 0)
        streak = int(s.get("streak_fail") or 0)
        last_fail = float(s.get("last_fail") or 0)
        last_used = float(s.get("last_used") or s.get("last") or 0)

        # Cap historical OK so one lucky domain cannot dominate forever
        ok_cap = min(ok, 8)
        fail_cap = min(fail, 12)

        score = ok_cap * 2.0 - fail_cap * 1.5 - avg * 0.04
        # never-used / low-used exploration bonus (diversity)
        if used == 0 and ok == 0 and fail == 0:
            score += 4.0
        elif used < 2:
            score += 2.0
        # true hard ban (disposable blocked)
        hard_until = float(s.get("hard_ban_until") or 0)
        if hard_until and now < hard_until:
            score -= 100.0
        # IP-level soft ban (error_generic) — short penalty, do not bury forever
        soft_until = float(s.get("soft_ban_until") or 0)
        if soft_until and now < soft_until:
            score -= 12.0
        # historical winners keep a floor so IP-level fails don't bury them under
        # never-used random domains
        if ok >= 5:
            score += min(ok, 20) * 0.35
        # recent fail cooldown (OTP timeout / slow)
        if last_fail and (now - last_fail) < DOMAIN_FAIL_COOLDOWN_SEC:
            left = DOMAIN_FAIL_COOLDOWN_SEC - (now - last_fail)
            score -= 20.0 + streak * 5.0 + (left / 60.0)
        reason = str(s.get("last_fail_reason") or "").lower()
        # IP-level generic: mild penalty only (was -25 + hard-ban → dead domains)
        if any(x in reason for x in ("email_submit", "error_generic", "something went wrong")):
            if hard_until and now < hard_until:
                score -= 25.0
            else:
                score -= 6.0
        # recently used → rotate away
        if dom in recent_set:
            # more recent in list = stronger penalty
            idx = recent.index(dom)  # 0 = most recent
            score -= 15.0 - idx * 2.5
        # soft recency: used in last 10 min even if not in recent list
        if last_used and (now - last_used) < 600:
            score -= 3.0
        # jitter so ties break randomly
        score += random.uniform(0, 1.2)
        scored.append((score, dom))

    scored.sort(key=lambda x: -x[0])
    ranked = [d for _, d in scored]

    # If we have enough domains, force-rotate: move last-used out of first slot
    if len(ranked) >= 2 and recent:
        last = recent[0]
        if ranked[0] == last:
            ranked = ranked[1:] + [last]

    return ranked


def pick_diverse_domain(candidates: list[str]) -> str:
    """
    Weighted random among top healthy domains (not always #1).
    Avoids last N used when alternatives exist.
    """
    ranked = rank_domains(candidates)
    if not ranked:
        raise ValueError("no domain candidates")
    recent = set(_recent_domains(DOMAIN_AVOID_LAST_N))
    # Prefer not-recent pool first
    fresh = [d for d in ranked if d not in recent]
    pool = fresh if fresh else ranked
    # Take top-K of pool for weighted pick
    k = min(6, len(pool))
    top = pool[:k]
    # weights: higher rank → higher weight, but still sample
    weights = [max(0.4, (k - i) ** 1.4) for i in range(k)]
    choice = random.choices(top, weights=weights, k=1)[0]
    mark_domain_used(choice)
    return choice
