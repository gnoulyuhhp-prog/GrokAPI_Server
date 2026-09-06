#!/usr/bin/env python3
"""
Verify grok_tool ledger names against live Sub2API.

Sub2API admin search matches account NAME only (not credentials.email).
Tool names are ``grok free NNN`` — searching the Grok email returns empty.

Usage (from grok_tool/):
  python verify_sub2api.py
  python verify_sub2api.py --email jnkxzoy6oo@ames.name.ng
  python verify_sub2api.py --missing-only
  python verify_sub2api.py --reimport-missing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from grokreg.core.config import load_config  # noqa: E402
from grokreg.core.paths_cfg import ACCOUNTS  # noqa: E402
from grokreg.delivery.sub2api_client import (  # noqa: E402
    Sub2APIError,
    client_from_cfg,
    export_sso_to_sub2api,
)


def _parse_ledger(path: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        email, password, status = parts[0], parts[1], parts[2]
        if not email or "@" not in email:
            continue
        rows.append((email, password, status))
    return rows


def _name_from_status(status: str) -> str:
    st = status or ""
    if st.startswith("added_sub2api:") or st.startswith("added_sub2api_untested:"):
        return st.split(":", 1)[1].strip()
    return ""


def _load_sso_by_email() -> dict[str, tuple[str, str]]:
    """email → (sso, name) from durable queue (any status)."""
    from grokreg.delivery.delivery_retry import _load_queue

    out: dict[str, tuple[str, str]] = {}
    for rec in _load_queue():
        email = str(rec.get("email") or "").strip().lower()
        sso = str(rec.get("sso") or "").strip()
        if not email or not sso:
            continue
        out[email] = (sso, str(rec.get("name") or ""))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify added_sub2api names in live Sub2API")
    parser.add_argument("--email", action="append", default=[], help="Only these emails (repeatable)")
    parser.add_argument("--missing-only", action="store_true", help="Print only ledger rows not found by name")
    parser.add_argument(
        "--reimport-missing",
        action="store_true",
        help="Re-POST SSO for missing rows that still have a queued cookie",
    )
    parser.add_argument("--limit", type=int, default=0, help="Check only last N added_sub2api rows (0=all)")
    args = parser.parse_args()

    config = load_config()
    sub_cfg = dict(config.get("sub2api") or {})
    client = client_from_cfg(sub_cfg)
    ping = client.test_connection()
    if not ping.get("ok"):
        print(f"Sub2API connect FAIL: {ping.get('error')}")
        return 2
    print(
        f"Sub2API ok url={ping.get('base_url')} auth={ping.get('auth')} "
        f"groups={ping.get('group_count')}"
    )
    print("NOTE: admin search is NAME-only. Search 'grok free 1071', not the Grok email.")

    want = {e.strip().lower() for e in args.email if e.strip()}
    rows = _parse_ledger(ACCOUNTS())
    added = [(e, p, s, _name_from_status(s)) for e, p, s in rows if str(s).startswith("added_sub2api")]
    if want:
        added = [r for r in added if r[0].lower() in want]
    if args.limit and args.limit > 0:
        added = added[-args.limit :]

    queued = _load_sso_by_email() if args.reimport_missing else {}
    found = 0
    missing = 0
    reimported = 0
    for email, _password, status, name in added:
        if not name:
            missing += 1
            print(f"MISSING  {email}  status={status} (no name in ledger)")
            continue
        acc = client.find_account_by_name(name)
        if acc:
            found += 1
            if not args.missing_only:
                print(
                    f"OK       {email}  name={name!r}  id={acc.get('id')}  "
                    f"groups={acc.get('group_ids')}  status={acc.get('status')}"
                )
            continue
        missing += 1
        print(f"MISSING  {email}  name={name!r}")
        if not args.reimport_missing:
            continue
        sso_rec = queued.get(email.lower())
        if not sso_rec:
            print(f"         no queued SSO — login that Grok session and run continue_sub2api.py")
            continue
        sso, queued_name = sso_rec
        try:
            result = export_sso_to_sub2api(
                sub_cfg,
                sso,
                email=email,
                name=queued_name or name,
            )
            reimported += 1
            print(
                f"         reimported id={result.get('account_id')} name={result.get('name')!r}"
            )
        except Sub2APIError as exc:
            print(f"         reimport FAIL: {exc}")

    print(f"\nchecked={len(added)} found={found} missing={missing} reimported={reimported}")
    if missing and not args.reimport_missing:
        print("Tip: sort admin accounts by id/created_at DESC, or search the NAME column.")
        print("Reimport only if verify says MISSING:  python verify_sub2api.py --reimport-missing")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
