"""
One-account protocol HTTP registration (competitor pure-HTTP path).

Requires Turnstile external solver (local :5072 or YesCaptcha).
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from grokreg.core.helpers import (
    is_plausible_xai_otp,
    normalize_otp_for_input,
    random_name,
    resolve_password,
    save_account,
)
from grokreg.core.runtime import ROOT, log
import grokreg.core.style_log as slog
from grokreg.mail.mail_api import EmailSession, MailApiClient
from grokreg.mail.providers import (
    AzpopMailProvider,
    HotmailProvider,
    MailTmProvider,
    wait_otp_smart,
)
from grokreg.mail.tmail_wibu import TmailWibuProvider
from grokreg.protocol.backend import (
    ProtocolEnvironmentError,
    ProtocolRegistrationBackend,
    SignupParameterDiscovery,
    build_protocol_session,
    build_signup_payload,
    clear_identity_cookies,
    read_sso_cookie_from_session,
)
from grokreg.reg.flow import acquire_email_session

SIGNUP_URL = "https://accounts.x.ai/sign-up"


@dataclass
class ProtocolResult:
    ok: bool
    status: str
    email: str = ""
    password: str = ""
    sso: str = ""
    duration_sec: float = 0.0
    detail: str = ""


def _kick_solver_async(config: dict[str, Any]) -> None:
    """Warm the local :5072 solver while email/OTP is in flight."""
    try:
        from services.solver_manager import start_async

        start_async(config)
    except Exception as e:
        log.debug("[protocol] solver start_async: %s", e)


def _ensure_solver(config: dict[str, Any]) -> None:
    """Block until local solver / YesCaptcha is usable, or raise."""
    from grokreg.captcha.turnstile_solver_client import ExternalTurnstileSolver

    provider = ExternalTurnstileSolver.from_config(config)
    if provider.available():
        return

    log.info("[protocol] Turnstile solver offline — đang tự bật Camoufox :5072 …")
    last_error = ""
    try:
        from services.solver_manager import ensure_started

        status = ensure_started(config)
        last_error = str(status.get("last_error") or status.get("message") or "")
        if status.get("online") or status.get("provider") == "yescaptcha":
            log.info(
                "[protocol] solver ready url=%s pid=%s",
                status.get("url"),
                status.get("pid"),
            )
            return
    except Exception as e:
        last_error = str(e)
        log.warning("[protocol] auto-start solver failed: %s", e)

    # Another process (web console / CHAY_SOLVER) may still be booting.
    deadline = time.time() + 20
    while time.time() < deadline:
        if provider.available():
            log.info("[protocol] solver came online while waiting")
            return
        time.sleep(1)

    extra = f" — {last_error}" if last_error else ""
    raise RuntimeError(
        "Turnstile solver offline — không bật được local :5072. "
        "Chạy CHAY_SOLVER.bat hoặc điền turnstile.yescaptcha_key"
        f"{extra}"
    )


def _solve_turnstile(config: dict[str, Any], *, site_key: str, url: str) -> str:
    from grokreg.captcha.turnstile_solver_client import ExternalTurnstileSolver

    _ensure_solver(config)
    provider = ExternalTurnstileSolver.from_config(config)
    if not provider.available():
        raise RuntimeError(
            "Turnstile solver offline — bật CHAY_SOLVER (:5072) hoặc YesCaptcha "
            "(protocol bắt buộc external solver, giống đối thủ)"
        )
    return provider.solve(url=url, site_key=site_key)


def register_one_github(config: dict[str, Any]) -> ProtocolResult:
    """Exact GitHub grok-tool path: HTTP only, no Chrome."""
    return register_one_protocol(config, castle=False)


def register_one_protocol(config: dict[str, Any], *, castle: bool = False) -> ProtocolResult:
    """Register one Grok account via HTTP.

    castle=False — GitHub clone (0 Chrome).
    castle=True  — mint Castle via Chrome then HTTP verify/submit.
    """
    t0 = time.time()
    email_session: Optional[EmailSession] = None
    password = resolve_password(config)
    fixed_f = str(config.get("fixed_first_name") or "").strip()
    fixed_l = str(config.get("fixed_last_name") or "").strip()
    custom_first = config.get("first_names") or config.get("name_first_pool")
    custom_last = config.get("last_names") or config.get("name_last_pool")
    first_pool = (
        [str(x).strip() for x in custom_first if str(x).strip()]
        if isinstance(custom_first, list)
        else None
    )
    last_pool = (
        [str(x).strip() for x in custom_last if str(x).strip()]
        if isinstance(custom_last, list)
        else None
    )
    if fixed_f and fixed_l:
        first, last = fixed_f, fixed_l
    else:
        first, last = random_name(first_pool, last_pool)
        if fixed_f:
            first = fixed_f
        if fixed_l:
            last = fixed_l
    save_path = ROOT / str(config.get("save_file") or "data/accounts.txt")

    try:
        mode = "castle-chrome" if castle else "github-http"
        log.info("[protocol] === START %s ===", mode)
        slog.api_ok(f"Backend {mode}")
        _kick_solver_async(config)
        session = build_protocol_session(
            {"browser_proxy": config.get("proxy") or ""},
            user_agent=str((config.get("protocol") or {}).get("user_agent") or ""),
            impersonate=str((config.get("protocol") or {}).get("impersonate") or ""),
        )
        clear_identity_cookies(session)

        discovery = SignupParameterDiscovery(session)
        params = discovery.discover(SIGNUP_URL)
        backend = ProtocolRegistrationBackend(session, params)
        log.info(
            "[protocol] params sitekey=%s… action=%s…",
            params.site_key[:14],
            params.action_id[:12],
        )

        # Tmail không nhận thư xAI. Protocol dùng Azpop trừ khi user chọn Hotmail.
        prov = str(config.get("email_provider") or "").strip().lower()
        if prov not in ("hotmail", "outlook"):
            config["email_provider"] = "azpopmail"

        mailtm = MailTmProvider()
        azpop = AzpopMailProvider(config.get("azpopmail") or {})
        tmail = TmailWibuProvider(config.get("tmail_wibu") or {})
        email_session, hotmail = acquire_email_session(config, mailtm, azpop, tmail)
        email = email_session.address
        log.info("[protocol] email=%s provider=%s", email, email_session.provider)
        slog.api_ok(f"HTTP email: {email} ({email_session.provider})")

        castle_token = ""
        otp_raw = ""
        token = ""
        mail_api = MailApiClient(config)
        timeout_otp = int(config.get("timeout_otp") or 120)
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))

        if castle:
            email_already_sent = False
            log.info("[protocol] polling OTP + Castle CreateEmail (Chrome)…")
            with ThreadPoolExecutor(max_workers=2) as pool:
                otp_fut = pool.submit(
                    wait_otp_smart,
                    email_session,
                    mail_api,
                    mailtm,
                    hotmail,
                    timeout_otp + 90,
                    ignore_ids=set(),
                    since_iso=since_iso,
                    azpop=azpop,
                    tmail_wibu=tmail,
                )
                try:
                    from grokreg.protocol.castle import mint_castle

                    slog.api_ok("Castle: Chrome mint + send email")
                    mint = mint_castle(config, session, email=email)
                    castle_token = (mint.token or "").strip()
                    email_already_sent = bool(mint.email_sent)
                    if email_already_sent:
                        slog.api_ok(f"CreateEmail OK method={mint.method}")
                    elif castle_token:
                        slog.api_ok(f"Castle token len={len(castle_token)}")
                    elif mint.error:
                        slog.api_err(f"Castle mint fail: {mint.error}")
                except Exception as e:
                    log.warning("[protocol] castle mint failed: %s", e)

                if not email_already_sent:
                    log.info("[protocol] send_email_code castle=%s…", bool(castle_token))
                    slog.api_ok("HTTP CreateEmail")
                    backend.send_email_code(email, SIGNUP_URL, castle_token)

                ts_fut = pool.submit(
                    _solve_turnstile,
                    config,
                    site_key=params.site_key,
                    url=SIGNUP_URL,
                )
                otp_raw = otp_fut.result(timeout=max(40, timeout_otp + 20))
                token = ts_fut.result(timeout=max(30, timeout_otp))
        else:
            log.info("[protocol] send_email_code…")
            slog.api_ok("HTTP CreateEmail (GitHub — 0 Chrome)")
            backend.send_email_code(email, SIGNUP_URL)

            log.info("[protocol] polling OTP and solving Turnstile concurrently…")
            with ThreadPoolExecutor(max_workers=2) as pool:
                otp_fut = pool.submit(
                    wait_otp_smart,
                    email_session,
                    mail_api,
                    mailtm,
                    hotmail,
                    timeout_otp,
                    ignore_ids=set(),
                    since_iso=since_iso,
                    azpop=azpop,
                    tmail_wibu=tmail,
                )
                ts_fut = pool.submit(
                    _solve_turnstile,
                    config,
                    site_key=params.site_key,
                    url=SIGNUP_URL,
                )
                otp_raw = otp_fut.result(timeout=max(40, timeout_otp + 10))
                token = ts_fut.result(timeout=max(30, timeout_otp))

        if not otp_raw:
            status = "error:protocol_otp_timeout"
            save_account(save_path, email, password, status)
            return ProtocolResult(
                False, status, email, password, duration_sec=time.time() - t0
            )
        otp = normalize_otp_for_input(otp_raw)
        if not otp or not is_plausible_xai_otp(otp):
            status = "error:protocol_otp_empty"
            save_account(save_path, email, password, status)
            return ProtocolResult(
                False, status, email, password, duration_sec=time.time() - t0
            )
        log.info("[protocol] OTP raw=%s norm=%s — verify…", otp_raw, otp)
        slog.api_ok(f"OTP: {otp_raw} → {otp}")

        backend.verify_email_code(email, otp, SIGNUP_URL)
        log.info("[protocol] verify_email_code OK")
        slog.api_ok("Verify OTP OK")

        payload = build_signup_payload(
            email=email,
            password=password,
            given_name=first,
            family_name=last,
            email_validation_code=otp,
            turnstile_token=token,
            castle_request_token=castle_token,
        )
        log.info("[protocol] submit_signup…")
        response = backend.submit_signup(payload, SIGNUP_URL, token)
        result = backend.extract_sso(response)
        sso = (result.sso or "").strip() or read_sso_cookie_from_session(session)
        if not sso:
            status = "error:protocol_no_sso"
            save_account(save_path, email, password, status)
            return ProtocolResult(
                False,
                status,
                email,
                password,
                duration_sec=time.time() - t0,
                detail=f"http={getattr(response, 'status_code', '?')}",
            )

        duration = time.time() - t0
        log.info(
            "[protocol] SUCCESS email=%s sso_len=%s duration=%.1fs",
            email,
            len(sso),
            duration,
        )

        status = "success"
        sub = config.get("sub2api") or {}
        if sub.get("enabled", True) is not False:
            try:
                from grokreg.delivery.sub2api_oauth import add_grok_to_sub2api

                s2 = asyncio.run(
                    add_grok_to_sub2api(
                        None,
                        None,
                        config,
                        email,
                        password,
                        sso_cookie=sso,
                    )
                )
                if s2 and getattr(s2, "ok", False):
                    status = f"added_sub2api:{s2.name}"
                    log.info("[protocol] Sub2API OK name=%s", s2.name)
                elif s2:
                    status = (
                        f"success_sub2api_fail:"
                        f"{getattr(s2, 'stage', '?')}:"
                        f"{str(getattr(s2, 'message', ''))[:80]}"
                    )
                else:
                    status = "success_sub2api_fail:unknown"
            except Exception as e:
                log.exception("[protocol] Sub2API error: %s", e)
        save_account(save_path, email, password, status)
        if str(status).startswith(("added_sub2api", "success")):
            try:
                from grokreg.reg.flow import push_results_to_gsheet

                push_results_to_gsheet(config, email)
            except Exception as e:
                log.error("[protocol] Google Sheet push failed for %s: %s", email, e)
        if hotmail:
            try:
                hotmail.mark_used(email_session)
            except Exception as e:
                log.warning("[protocol] hotmail mark_used: %s", e)
        return ProtocolResult(
            True,
            status,
            email,
            password,
            sso=sso,
            duration_sec=duration,
            detail="protocol",
        )

    except ProtocolEnvironmentError as e:
        email = email_session.address if email_session else ""
        status = f"error:protocol_env:{e.reason}:{str(e)[:80]}"
        log.error("[protocol] environment: %s", e)
        if email:
            save_account(save_path, email, password, status)
        return ProtocolResult(
            False, status, email, password, duration_sec=time.time() - t0, detail=str(e)
        )
    except Exception as e:
        email = email_session.address if email_session else ""
        status = f"error:protocol:{str(e)[:100]}"
        log.exception("[protocol] fatal: %s", e)
        if email:
            save_account(save_path, email, password, status)
        return ProtocolResult(
            False, status, email, password, duration_sec=time.time() - t0, detail=str(e)
        )
