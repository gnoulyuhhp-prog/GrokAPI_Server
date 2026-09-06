#!/usr/bin/env python3
"""
Xuất báo cáo sáng overnight Grok reg.
  - Google Sheet (chính): config.google_sheets
  - TXT backup (tuỳ chọn): MORNING_REPORT.txt
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# grokreg/tools/ → project root (do not import grokreg.core — pulls pydoll)
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
ACCOUNTS = DATA / "accounts.txt"
TIMES = DATA / "account_times.json"
FIX_LOG = DATA / "fix_log.txt"
OVERNIGHT_LOGS = DATA / "overnight_logs"
OUT_DIR = DATA


def now() -> datetime:
    return datetime.now()


def _load_account_times() -> dict[str, str]:
    if not TIMES.exists():
        return {}
    try:
        raw = json.loads(TIMES.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k).lower(): str(v) for k, v in raw.items() if v}
    except Exception:
        pass
    return {}


def parse_accounts() -> list[dict]:
    rows = []
    if not ACCOUNTS.exists():
        return rows
    times = _load_account_times()
    for ln in ACCOUNTS.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or "|" not in ln:
            continue
        parts = ln.split("|")
        if len(parts) < 3:
            continue
        email, password, status = parts[0].strip(), parts[1].strip(), "|".join(parts[2:]).strip()
        ts = times.get(email.lower(), "")
        rows.append(
            {
                "email": email,
                "password": password,
                "status": status,
                "ts": ts,
                "raw": ln,
            }
        )
    return rows


def classify(status: str) -> str:
    s = (status or "").lower()
    if s.startswith("added_sub2api"):
        return "full_ok"
    if s == "success" or s.startswith("success_sub2api"):
        return "reg_ok"
    if "manual_check" in s:
        return "manual"
    if s.startswith("error:"):
        if "otp" in s:
            return "fail_otp"
        if "verification" in s or "cf_" in s or "turnstile" in s:
            return "fail_cf"
        if "email_field" in s or "signup" in s:
            return "fail_ui"
        if "argument already" in s or "chrome" in s:
            return "fail_browser"
        if "rate_limit" in s:
            return "fail_rate"
        if "timeout" in s:
            return "fail_timeout"
        return "fail_other"
    return "other"


def night_window(ref: datetime | None = None) -> tuple[datetime, datetime]:
    """Ca đêm: từ OVERNIGHT START đầu tiên trong ~12h trước mốc 6h, else 00:00."""
    ref = ref or now()
    end = ref.replace(hour=6, minute=0, second=0, microsecond=0)
    if ref.hour < 6:
        end = ref
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if FIX_LOG.exists():
        candidates = []
        for ln in FIX_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(
                r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*OVERNIGHT.*(START|STABLE)",
                ln,
            )
            if not m:
                continue
            try:
                t0 = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if end - timedelta(hours=14) <= t0 <= end + timedelta(hours=1):
                candidates.append(t0)
        if candidates:
            start = min(candidates)
    return start, end


def collect_night_emails(start: datetime, end: datetime) -> dict[str, str]:
    night: dict[str, str] = {}
    files: list[Path] = []
    if OVERNIGHT_LOGS.exists():
        files.extend(OVERNIGHT_LOGS.glob("*.log"))
    files.extend(ROOT.glob("run_night*.log"))

    for f in files:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < start - timedelta(minutes=30) or mtime > end + timedelta(hours=2):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        email = ""
        for ln in text.splitlines():
            if "Email" in ln and "@" in ln:
                mm = re.search(r"([\w.+-]+@[\w.-]+)", ln)
                if mm:
                    email = mm.group(1).lower()
            if "Saved" in ln and "|" in ln and "@" in ln:
                parts = ln.split("|")
                em = re.search(r"([\w.+-]+@[\w.-]+)", parts[0])
                if em and len(parts) >= 3:
                    email = em.group(1).lower()
                    night[email] = parts[-1].strip()
            if "Done. status=" in ln and email:
                night[email] = ln.split("Done. status=", 1)[-1].strip()
    return night


def bar(n: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "·" * width
    filled = int(round(width * n / total))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def short_reason(status: str) -> str:
    s = (status or "").lower()
    if "otp" in s:
        return "OTP / mail timeout"
    if "verification" in s or "cf_" in s:
        return "Cloudflare / verification"
    if "email_field" in s:
        return "UI không thấy field email"
    if "argument already" in s or "no-first-run" in s:
        return "Chrome args trùng"
    if "rate_limit" in s:
        return "Rate limit xAI"
    if "timeout" in s:
        return "Timeout"
    if s.startswith("error:"):
        return status[6:50]
    return status[:50]


def collect_report_data() -> dict[str, Any]:
    """Structured data for TXT + Google Sheet."""
    ref = now()
    start, end = night_window(ref)
    rows = parse_accounts()
    night_map = collect_night_emails(start, end)

    by_email: dict[str, dict] = {}
    for r in rows:
        key = r["email"].lower()
        if key in night_map:
            by_email[key] = r
    for em, st in night_map.items():
        if em not in by_email:
            by_email[em] = {"email": em, "password": "?", "status": st, "raw": ""}

    session = list(by_email.values())
    full_ok = [r for r in session if classify(r["status"]) == "full_ok"]
    reg_only = [r for r in session if classify(r["status"]) in ("reg_ok", "manual")]
    reg_ok = full_ok + reg_only
    fails = [r for r in session if classify(r["status"]).startswith("fail")]
    other = [r for r in session if r not in reg_ok and r not in fails]

    total = len(session)
    ok_n = len(reg_ok)
    full_n = len(full_ok)
    fail_n = len(fails)
    rate = (100.0 * ok_n / total) if total else 0.0

    starts = ends_n = 0
    if FIX_LOG.exists():
        for ln in FIX_LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", ln)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts < start - timedelta(minutes=5) or ts > end + timedelta(hours=1):
                continue
            if "START run#" in ln:
                starts += 1
            if "END run#" in ln:
                ends_n += 1

    all_buckets = Counter(classify(r["status"]) for r in rows)
    all_ok = sum(1 for r in rows if classify(r["status"]) in ("full_ok", "reg_ok", "manual"))
    all_fail = sum(v for k, v in all_buckets.items() if k.startswith("fail"))
    fail_groups = Counter(classify(r["status"]) for r in fails)

    return {
        "ref": ref,
        "start": start,
        "end": end,
        "rows": rows,
        "session": session,
        "full_ok": full_ok,
        "reg_only": reg_only,
        "reg_ok": reg_ok,
        "fails": fails,
        "other": other,
        "total": total,
        "ok_n": ok_n,
        "full_n": full_n,
        "fail_n": fail_n,
        "rate": rate,
        "starts": starts,
        "ends_n": ends_n,
        "all_buckets": all_buckets,
        "all_ok": all_ok,
        "all_fail": all_fail,
        "alltime_total": len(rows),
        "alltime_full": all_buckets.get("full_ok", 0),
        "alltime_ok": all_ok,
        "alltime_fail": all_fail,
        "fail_groups": fail_groups,
        "short_reason": short_reason,
    }


def build_report_text(data: dict[str, Any] | None = None) -> str:
    """
    Format cố định (sheet + TXT) — chỉ list acc FULL thành công:
      Header · KPI · FULL table · OPS
    Không list acc die/fail/REG.
    """
    d = data or collect_report_data()
    ref = d["ref"]
    start, end = d["start"], d["end"]
    full_ok = d["full_ok"]
    total, ok_n, full_n, fail_n, rate = (
        d["total"],
        d["ok_n"],
        d["full_n"],
        d["fail_n"],
        d["rate"],
    )
    starts, ends_n = d["starts"], d["ends_n"]
    rows = d["rows"]
    all_buckets, all_ok, all_fail = d["all_buckets"], d["all_ok"], d["all_fail"]
    reg_only_n = max(0, ok_n - full_n)

    L: list[str] = []
    L.append("GROK REG  ·  BÁO CÁO SÁNG (OVERNIGHT)  ·  " + start.strftime("%Y-%m-%d"))
    L.append(
        f"Ca đêm    {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}"
        f"            Xuất lúc    {ref.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    L.append("")
    L.append("FULL Sub2API    REG only    FAIL    TỔNG unique    TỶ LỆ OK %")
    L.append(f"{full_n}    {reg_only_n}    {fail_n}    {total}    {rate:.1f}")
    L.append("")
    L.append(
        f"FULL  (email | pass | sub2api_name)  ·  {full_n} acc"
    )
    L.append("#    Email    Password    Sub2API Name")
    if full_ok:
        for i, r in enumerate(full_ok, 1):
            name = ""
            if "added_sub2api" in r["status"] and ":" in r["status"]:
                name = r["status"].split(":", 1)[-1].strip()
            L.append(f"{i}    {r['email']}    {r['password']}    {name}")
    else:
        L.append("(trống)")
    L.append("")
    cut = max(0, starts - ends_n) if starts else 0
    L.append("OPS / ALL-TIME")
    L.append(
        f"Ca đêm runs: START={starts}  END={ends_n}"
        + (f"  ⚠ cut={cut}" if cut else "")
        + f"  |  accounts.txt: {len(rows)} dòng · FULL={all_buckets.get('full_ok', 0)}"
        f" · OK={all_ok} · FAIL={all_fail}"
    )
    return "\n".join(L) + "\n"


def build_report() -> str:
    """Backward-compatible alias."""
    return build_report_text()


def _safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            import sys

            sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            print(msg.encode("ascii", errors="replace").decode("ascii"), flush=True)


def _fix_log(msg: str) -> None:
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(FIX_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass
    _safe_print(line)


def _write_txt_backup(text: str) -> None:
    """Local TXT is backup only — never counts as export success."""
    day = now().strftime("%Y-%m-%d")
    out = OUT_DIR / f"morning_report_{day}.txt"
    latest = OUT_DIR / "MORNING_REPORT.txt"
    out.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    _safe_print(f"TXT backup: {out}")
    _safe_print(f"TXT backup: {latest}")


def push_sheet_with_retries(data: dict[str, Any], max_attempts: int = 3) -> str:
    """
    Push Google Sheet — bắt buộc thành công.
    Retry tối đa max_attempts. Mỗi lần fail ghi fix_log.
    Raises RuntimeError("PUSH SHEET FAILED: ...") nếu hết retry.
    """
    import time

    import grokreg.delivery.gsheets_export as gse

    last_err = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            _fix_log(f"SHEET PUSH attempt {attempt}/{max_attempts}...")
            msg = gse.export_to_google_sheets(data)
            if not msg or "disabled" in str(msg).lower():
                raise RuntimeError(f"sheet push rejected: {msg!r}")
            _fix_log(f"SHEET PUSH OK (attempt {attempt}): {str(msg)[:200]}")
            return str(msg)
        except Exception as e:
            last_err = str(e)
            _fix_log(f"SHEET PUSH FAIL attempt {attempt}/{max_attempts}: {last_err[:500]}")
            if attempt < max_attempts:
                # backoff 5s, 10s
                time.sleep(5 * attempt)
    raise RuntimeError(f"PUSH SHEET FAILED after {max_attempts} attempts: {last_err}")


def main() -> int:
    """
    Export morning report.
    SUCCESS = Google Sheet push OK (required).
    Local TXT is optional backup only — never enough alone.
    Exit 0 = sheet OK; exit 2 = PUSH SHEET FAILED (critical).
    """
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    data = collect_report_data()
    text = build_report_text(data)

    # Always keep local TXT as forensic backup (does NOT count as success)
    try:
        import grokreg.delivery.gsheets_export as gse

        gs = gse.load_gs_config()
        write_txt = bool(gs.get("write_txt_backup", True))
        max_attempts = int(gs.get("push_max_attempts", 3) or 3)
    except Exception:
        write_txt = True
        max_attempts = 3

    if write_txt:
        try:
            _write_txt_backup(text)
            _fix_log("TXT backup written (not sufficient alone — sheet required)")
        except Exception as e:
            _fix_log(f"TXT backup write error (non-fatal): {e}")

    # === REQUIRED: Google Sheet ===
    try:
        sheet_msg = push_sheet_with_retries(data, max_attempts=max_attempts)
        _safe_print(f"Google Sheet: {sheet_msg}")
        _fix_log(f"Morning report COMPLETE (Google Sheet OK): {sheet_msg[:160]}")
        return 0
    except Exception as e:
        err = str(e)
        if "PUSH SHEET FAILED" not in err:
            err = f"PUSH SHEET FAILED: {err}"
        _fix_log(err)
        _fix_log("CRITICAL: sheet push failed after retries — local TXT is backup only")
        _safe_print(err)
        _safe_print("PUSH SHEET FAILED")  # exact marker for monitors / overnight
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
