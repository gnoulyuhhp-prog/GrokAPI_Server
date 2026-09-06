"""
Temp-mail failover: prefer the healthier provider, switch when lagging.

Providers:
  - azpopmail   (https://azpopmail.com/document)
  - tmail_wibu  (https://tmail.wibucrypto.pro Livewire)

Per-run pick (cannot swap mid-OTP after email is submitted on xAI).
After OTP timeout / create fail → mark lag → next run prefers the other.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("grok-reg")

ROOT = Path(__file__).resolve().parents[2]
STATS_PATH = ROOT / "data" / "temp_provider_stats.json"

PROVIDERS = ("azpopmail", "tmail_wibu", "tinyhost", "tempmail_lol", "tempmail_vip", "racing")
# After this many consecutive fails, hard-prefer the other for a while
STREAK_SWITCH = 1
# Soft cooldown after fail (seconds)
FAIL_COOLDOWN = 12 * 60


def _load() -> dict[str, Any]:
    try:
        if STATS_PATH.exists():
            data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"providers": {}, "last_used": "", "last_fail": ""}


def _save(data: dict[str, Any]) -> None:
    try:
        STATS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _slot(data: dict[str, Any], name: str) -> dict[str, Any]:
    prov = data.setdefault("providers", {})
    d = prov.get(name) or {
        "ok": 0,
        "fail": 0,
        "streak_fail": 0,
        "streak_ok": 0,
        "last_ok": 0.0,
        "last_fail": 0.0,
        "last_reason": "",
    }
    prov[name] = d
    return d


def mark_temp_result(provider: str, *, ok: bool, reason: str = "") -> None:
    """Record success/fail for ranking next pick."""
    name = (provider or "").strip().lower()
    if name not in PROVIDERS:
        return
    data = _load()
    d = _slot(data, name)
    now = time.time()
    if ok:
        d["ok"] = int(d.get("ok") or 0) + 1
        d["streak_ok"] = int(d.get("streak_ok") or 0) + 1
        d["streak_fail"] = 0
        d["last_ok"] = now
        d["last_reason"] = (reason or "ok")[:80]
        data["last_ok_provider"] = name
    else:
        d["fail"] = int(d.get("fail") or 0) + 1
        d["streak_fail"] = int(d.get("streak_fail") or 0) + 1
        d["streak_ok"] = 0
        d["last_fail"] = now
        d["last_reason"] = (reason or "fail")[:80]
        data["last_fail"] = name
        data["last_fail_reason"] = (reason or "")[:80]
    data["providers"][name] = d
    _save(data)
    log.info(
        "TempMail stats %s ok=%s fail=%s streak_fail=%s reason=%s",
        name,
        d["ok"],
        d["fail"],
        d["streak_fail"],
        (reason or "")[:40],
    )


def _score(name: str, d: dict[str, Any], now: float) -> float:
    ok = int(d.get("ok") or 0)
    fail = int(d.get("fail") or 0)
    streak = int(d.get("streak_fail") or 0)
    last_fail = float(d.get("last_fail") or 0)
    last_ok = float(d.get("last_ok") or 0)

    # Cap history so one old winner does not dominate forever
    score = min(ok, 10) * 2.5 - min(fail, 15) * 1.8
    # Exploration for never-used
    if ok == 0 and fail == 0:
        score += 3.0
    # Recent fail cooldown
    if last_fail and (now - last_fail) < FAIL_COOLDOWN:
        score -= 18.0 + streak * 6.0
    # Recent success bonus
    if last_ok and (now - last_ok) < 30 * 60:
        score += 4.0
    # Active fail streak → push away hard
    if streak >= STREAK_SWITCH:
        score -= 25.0 + streak * 3.0
    score += random.uniform(0, 0.8)
    return score


def pick_temp_provider(
    preferred_order: list[str] | None = None,
) -> str:
    """
    Choose azpopmail or tmail_wibu for this run.
    Prefer healthier; after lag/OTP fail, switch to the other.
    """
    order = preferred_order or list(PROVIDERS)
    order = [p for p in order if p in PROVIDERS]
    if not order:
        order = list(PROVIDERS)

    data = _load()
    now = time.time()
    last_fail = str(data.get("last_fail") or "")

    scored: list[tuple[float, str]] = []
    for name in order:
        d = _slot(data, name)
        s = _score(name, d, now)
        # If last run failed on X, slight boost to the other
        if last_fail and name != last_fail:
            s += 8.0
        if last_fail and name == last_fail:
            s -= 5.0
        scored.append((s, name))

    scored.sort(key=lambda x: -x[0])
    choice = scored[0][1]
    data["last_used"] = choice
    data["last_pick_scores"] = {n: round(s, 2) for s, n in scored}
    _save(data)

    log.info(
        "TempMail pick → %s  scores=%s",
        choice,
        {n: round(s, 1) for s, n in scored},
    )
    return choice


def note_from_status(provider: str, status: str) -> None:
    """Map final register status → mark_temp_result."""
    st = (status or "").lower()
    if not provider or provider not in PROVIDERS:
        return
    if st.startswith("added_sub2api") or st == "success" or st.startswith(
        "success_sub2api"
    ):
        mark_temp_result(provider, ok=True, reason=st[:60])
        return
    # lag / OTP / create issues → switch preference next run
    lag_keys = (
        "otp_timeout",
        "otp_timeout_resume",
        "hard_timeout",
        "azpop",
        "tmail",
        "mail",
        "create",
        "handshake",
        "connection",
        "signup_with_email",  # sometimes domain blocked UI
    )
    if any(k in st for k in lag_keys) or st.startswith("error:"):
        # only mark temp providers for temp-related fails
        if any(
            k in st
            for k in (
                "otp",
                "mail",
                "azpop",
                "tmail",
                "timeout",
                "handshake",
                "create",
                "connection",
            )
        ):
            mark_temp_result(provider, ok=False, reason=st[:80])
