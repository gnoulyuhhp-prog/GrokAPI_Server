#!/usr/bin/env python3
"""Re-import old Grok accounts (accounts.txt added_sub2api) into the new Sub2API."""

from __future__ import annotations

import asyncio
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from grokreg.core.config import load_config
from grokreg.core.runtime import log
from grokreg.browser.chrome import close_browser_handle, open_or_attach_browser
from grokreg.captcha.turnstile_solver_client import ExternalTurnstileSolver
from grokreg.delivery.sub2api_oauth import add_grok_to_sub2api, next_account_name
from grokreg.delivery.sub2api_client import client_from_cfg
from grokreg.protocol.backend import (
    build_protocol_session,
    follow_sso_http,
    read_sso_cookie_from_session,
)

ACCOUNTS = ROOT / "data" / "accounts.txt"
PROGRESS = ROOT / "data" / "reimport_progress.jsonl"
NAME_RE = re.compile(r"added_sub2api:\s*(grok free\s+\d+)", re.I)
XAI_ACCOUNTS_URL = "https://accounts.x.ai"
TRUSTED_COOKIE_HOSTS = {"accounts.x.ai", "auth.x.ai", "auth.grok.com"}
# This is the site key used by the accounts.x.ai password-session RPC.  It is
# intentionally separate from the sign-up page's widget key.
XAI_PASSWORD_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"


def _find_string_field(value, *names: str) -> str:
    """Find a named string in an RPC envelope without logging its contents."""
    wanted = {name.lower() for name in names}
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if str(key).lower() in wanted and isinstance(item, str):
                    return item.strip()
                if isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(current, list):
            pending.extend(item for item in current if isinstance(item, (dict, list)))
    return ""


def parse_old_accounts(*, include_reg_only: bool = False) -> list[tuple[str, str, str]]:
    """Return latest eligible ledger row per email.

    ``include_reg_only`` is intentionally opt-in because these rows require a
    fresh xAI email/password OAuth login; unlike added_sub2api rows they have
    no stored account name or SSO cookie.
    """
    latest: dict[str, tuple[str, str, str]] = {}
    if not ACCOUNTS.exists():
        return []
    for line in ACCOUNTS.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        email, password, status = parts[0], parts[1], "|".join(parts[2:])
        if not email or not password:
            continue
        sl = status.lower()
        m = NAME_RE.search(status)
        name = m.group(1).strip() if m else ""
        eligible = "added_sub2api" in sl or (
            include_reg_only
            and (
                sl == "success"
                or sl.startswith("success_not_logged")
                or sl.startswith("success_sub2api_fail")
            )
        )
        latest[email.lower()] = (email, password, name) if eligible else ("", "", "")
    return [row for row in latest.values() if row[0]]


def live_account_email_names(client) -> tuple[dict[str, str], int]:
    """Return live Grok email→name plus raw DB record count."""
    first = client._request_json(
        "GET", "/api/v1/admin/accounts?page=1&page_size=100&platform=grok"
    )
    if not isinstance(first, dict):
        return {}, 0
    items = list(first.get("items") or [])
    pages = max(1, int(first.get("pages") or 1))
    for page in range(2, pages + 1):
        data = client._request_json(
            "GET",
            f"/api/v1/admin/accounts?page={page}&page_size=100&platform=grok",
        )
        if isinstance(data, dict):
            items.extend(data.get("items") or [])
    names: dict[str, str] = {}
    for acc in items:
        if not isinstance(acc, dict):
            continue
        email = str((acc.get("credentials") or {}).get("email") or "").strip().lower()
        if email:
            names[email] = str(acc.get("name") or email).strip()
    return names, int(first.get("total") or len(items))


def live_account_emails(client) -> set[str]:
    """Compatibility helper used by verification scripts."""
    names, _ = live_account_email_names(client)
    return set(names)


def mark_ledger_added(email: str, password: str, name: str) -> None:
    """Append a new latest-status row; preserve the historical ledger."""
    with ACCOUNTS.open("a", encoding="utf-8") as handle:
        handle.write(f"{email}|{password}|added_sub2api:{name}\n")


def _password_login_sso_local(config: dict, email: str, password: str) -> str:
    """Create an ephemeral xAI SSO session using only HTTP + local solver."""
    solver = ExternalTurnstileSolver.from_config(config)
    turnstile_token = solver.solve(
        url=XAI_ACCOUNTS_URL,
        site_key=XAI_PASSWORD_SITEKEY,
    )
    session = build_protocol_session(config)
    response = session.post(
        f"{XAI_ACCOUNTS_URL}/api/rpc",
        json={
            "rpc": "createSession",
            "req": {
                "createSessionRequest": {
                    "credentials": {
                        "case": "emailAndPassword",
                        "value": {
                            "email": email.strip(),
                            "clearTextPassword": password,
                        },
                    },
                },
                "turnstileToken": turnstile_token,
            },
        },
        headers={
            "Content-Type": "application/json",
            "Origin": XAI_ACCOUNTS_URL,
            "Referer": f"{XAI_ACCOUNTS_URL}/sign-in?redirect=grok-com&email=true",
            "Accept": "*/*",
        },
        timeout=120,
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise RuntimeError(f"xAI password login returned HTTP {response.status_code}")
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise RuntimeError("xAI password login returned invalid JSON") from exc
    if _find_string_field(payload, "error", "errorMessage"):
        raise RuntimeError("xAI password login rejected the credentials")
    cookie_url = _find_string_field(payload, "cookieSetterUrl", "cookie_setter_url")
    parsed = urlparse(cookie_url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_COOKIE_HOSTS:
        envelope = ",".join(sorted(str(key) for key in payload.keys()))[:160]
        raise RuntimeError(
            "xAI password login returned no trusted cookie URL "
            f"(host={parsed.hostname or '-'}, fields={envelope or '-'})"
        )

    # The SSO cookie can be set on the first response or on a nested trusted
    # cookie-setter hop.  Keep it in memory only for the immediate import.
    sso = follow_sso_http(session, cookie_url, max_hops=8)
    if not sso:
        sso = read_sso_cookie_from_session(session)
    if not sso:
        raise RuntimeError("xAI password login did not mint an SSO cookie")
    return sso


def import_password_api(client, config: dict, email: str, password: str) -> dict:
    """Authorize locally, then let Sub2API exchange ephemeral SSO for OAuth."""
    sub_cfg = dict(config.get("sub2api") or {})
    group_name = str(sub_cfg.get("group") or "Grok")
    group_ids = client.resolve_group_ids_by_name(group_name)
    name = next_account_name(sub_cfg)
    sso = _password_login_sso_local(config, email, password)
    result = client.import_sso(
        sso,
        email=email,
        name=name,
        group_ids=group_ids,
        concurrency=1,
        priority=0,
        auto_pause_on_expired=True,
    )
    if not result.get("ok") or not result.get("account_id"):
        raise RuntimeError("Sub2API SSO import returned no account id")
    return {
        "name": str(result.get("name") or name),
        "account_id": result.get("account_id"),
    }


def load_done() -> set[str]:
    done: set[str] = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ok") and rec.get("email"):
                done.add(str(rec["email"]).lower())
    return done


def already_in_sub2api(client, name: str) -> bool:
    if not name:
        return False
    try:
        return client.find_account_by_name(name) is not None
    except Exception as e:
        log.warning("lookup %s failed: %s", name, e)
        return False


def append_progress(rec: dict) -> None:
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Reimport Grok accounts into Sub2API")
    parser.add_argument(
        "--reg-only",
        action="store_true",
        help="Also reauth latest success/reg-only rows that have no stored SSO",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing rows this run")
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Update local ledger for accounts already present in live Sub2API",
    )
    args = parser.parse_args()

    config = load_config()
    config.setdefault("sub2api", {})
    config["sub2api"]["enabled"] = True
    config["sub2api"]["run_test"] = False
    # Never reuse leftover Chrome SSO — that stamps the same xAI session onto every name.
    config["sub2api"]["mode"] = "browser_oauth"
    config["sub2api"]["fallback_browser_oauth"] = True
    config["headless"] = True
    config["chrome_background"] = True
    config["chrome_steal_focus"] = False
    config["keep_browser_open"] = False
    config["fresh_profile_per_account"] = False

    client = client_from_cfg(config["sub2api"])
    probe = client.test_connection()
    log.info("Sub2API probe: %s", probe)
    if not probe.get("ok"):
        log.error("Cannot login Sub2API — abort")
        return 1

    accounts = parse_old_accounts(include_reg_only=args.reg_only)
    done = load_done()
    live_names, live_total = live_account_email_names(client)
    live_emails = set(live_names)
    log.info(
        "ledger eligible=%s live_unique=%s live_records=%s already_logged=%s reg_only=%s",
        len(accounts),
        len(live_emails),
        live_total,
        len(done),
        args.reg_only,
    )

    todo = []
    reconciled = 0
    for email, password, name in accounts:
        if email.lower() in live_emails:
            if not name:
                mark_ledger_added(email, password, live_names[email.lower()])
                reconciled += 1
            continue
        if email.lower() in done:
            continue
        if name and already_in_sub2api(client, name):
            append_progress({"ok": True, "email": email, "name": name, "skip": "already_in_db"})
            done.add(email.lower())
            continue
        todo.append((email, password, name))
    log.info("ledger reconciled from live Sub2API: %s", reconciled)
    if args.reconcile_only:
        return 0
    if args.limit > 0:
        todo = todo[: args.limit]
    log.info("to import: %s", len(todo))
    if not todo:
        return 0

    # Reg-only rows use Sub2API's password authorization API and need no
    # browser at all. The legacy added_sub2api migration retains browser OAuth.
    handle = None if args.reg_only else await open_or_attach_browser(config)
    ok = fail = 0
    try:
        for i, (email, password, name) in enumerate(todo, 1):
            log.info("=== [%s/%s] %s name=%s ===", i, len(todo), email, name or "(auto)")
            t0 = time.time()
            try:
                if args.reg_only:
                    created = await asyncio.to_thread(
                        import_password_api, client, config, email, password
                    )
                    rec = {
                        "ok": True,
                        "email": email,
                        "name": created["name"],
                        "account_id": created["account_id"],
                        "stage": "password_api",
                        "message": "created via HTTP password authorization",
                        "sec": round(time.time() - t0, 1),
                    }
                else:
                    # Never allow the previous xAI identity to authorize the
                    # next row. Keep CF cookies, remove SSO/session identity.
                    from grokreg.browser.anti_flag import clear_sso_identity_only

                    await clear_sso_identity_only(handle.tab)
                    result = await add_grok_to_sub2api(
                        handle.browser,
                        handle.tab,
                        config,
                        email,
                        password,
                        account_name=name or None,
                    )
                    rec = {
                        "ok": bool(result.ok),
                        "email": email,
                        "name": result.name or name,
                        "stage": result.stage,
                        "message": result.message,
                        "sec": round(time.time() - t0, 1),
                    }
            except Exception as e:
                rec = {
                    "ok": False,
                    "email": email,
                    "name": name,
                    "stage": "exception",
                    "message": str(e)[:300],
                    "sec": round(time.time() - t0, 1),
                }
            append_progress(rec)
            if rec["ok"]:
                ok += 1
                mark_ledger_added(email, password, str(rec["name"] or name))
                log.info("OK %s → %s", email, rec["name"])
            else:
                fail += 1
                log.error("FAIL %s stage=%s %s", email, rec.get("stage"), rec.get("message"))
            await asyncio.sleep(1)
    finally:
        if handle is not None:
            try:
                await close_browser_handle(handle)
            except Exception:
                pass
    log.info("DONE ok=%s fail=%s remaining_approx=%s", ok, fail, max(0, len(todo) - ok - fail))
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
