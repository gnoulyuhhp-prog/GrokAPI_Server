#!/usr/bin/env python3
"""Import SuperGrok hotmails into Sub2API group `supergrok` via browser OAuth."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from grokreg.core.config import load_config
from grokreg.core.runtime import log
from grokreg.browser.chrome import close_browser_handle, open_or_attach_browser
from grokreg.browser.jsutil import _exec_js
import grokreg.browser.anti_flag as af
from grokreg.delivery.sub2api_oauth import add_grok_to_sub2api
from grokreg.delivery.sub2api_client import client_from_cfg

ACCOUNTS = ROOT / "data" / "supergrok_accounts.txt"
PROGRESS = ROOT / "data" / "supergrok_import_progress.jsonl"


def load_accounts() -> list[tuple[str, str, str]]:
    out = []
    for i, line in enumerate(ACCOUNTS.read_text(encoding="utf-8").splitlines(), 1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        name = f"supergrok {i:03d}"
        out.append((parts[0], parts[1], name))
    return out


def load_done() -> set[str]:
    done = set()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ok") and rec.get("email"):
                done.add(str(rec["email"]).lower())
    return done


def append_progress(rec: dict) -> None:
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def main() -> int:
    config = load_config()
    config.setdefault("sub2api", {})
    config["sub2api"]["enabled"] = True
    config["sub2api"]["group"] = "supergrok"
    config["sub2api"]["group_ids"] = [3]
    config["sub2api"]["name_prefix"] = "supergrok"
    config["sub2api"]["name_include_email"] = True
    config["sub2api"]["run_test"] = False
    config["sub2api"]["mode"] = "browser_oauth"
    config["sub2api"]["fallback_browser_oauth"] = True
    config["keep_browser_open"] = True
    config["fresh_profile_per_account"] = False

    client = client_from_cfg(config["sub2api"])
    probe = client.test_connection()
    log.info("Sub2API probe: %s", probe)
    if not probe.get("ok"):
        log.error("Cannot login Sub2API")
        return 1

    accounts = load_accounts()
    done = load_done()
    todo = [a for a in accounts if a[0].lower() not in done]
    log.info("supergrok total=%s done=%s todo=%s", len(accounts), len(done), len(todo))
    if not todo:
        return 0

    handle = await open_or_attach_browser(config)
    ok = fail = 0
    try:
        for i, (email, password, name) in enumerate(todo, 1):
            log.info("=== [%s/%s] %s → %s ===", i, len(todo), email, name)
            t0 = time.time()
            try:
                try:
                    await handle.tab.go_to("https://accounts.x.ai/")
                    await asyncio.sleep(1.0)
                    guest = await af.ensure_guest_session(handle.tab, _exec_js)
                    log.info("wiped previous xAI SSO leftover=%s", guest.get("wiped"))
                    if guest.get("logged_in_after"):
                        await af.clear_sso_identity_only(handle.tab)
                        await handle.tab.go_to("https://accounts.x.ai/sign-in")
                        await asyncio.sleep(1.0)
                except Exception as wipe_err:
                    log.warning("SSO wipe before login failed: %s", wipe_err)
                result = await add_grok_to_sub2api(
                    handle.browser,
                    handle.tab,
                    config,
                    email,
                    password,
                    account_name=name,
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
                log.info("OK %s → %s", email, rec["name"])
            else:
                fail += 1
                log.error("FAIL %s stage=%s %s", email, rec.get("stage"), rec.get("message"))
            await asyncio.sleep(2)
    finally:
        try:
            await close_browser_handle(handle)
        except Exception:
            pass
    log.info("DONE ok=%s fail=%s", ok, fail)
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
