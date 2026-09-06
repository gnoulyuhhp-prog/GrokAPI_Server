#!/usr/bin/env python3
"""
Day batch: reg + Sub2API until TARGET new FULL or stop clock.

Default: +50 FULL or stop 10:30 local time.
Reuses overnight_runner run_one / lock / cleanup.
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import grokreg.tools.overnight_runner as ov  # noqa: E402

ACCOUNTS = ROOT / "data" / "accounts.txt"
TARGET_NEW_FULL = int(os.environ.get("BATCH_TARGET", "50"))
# stop at HH:MM — default 10:30
STOP_HM = os.environ.get("BATCH_STOP", "10:30")


def count_full() -> int:
    if not ACCOUNTS.exists():
        return 0
    n = 0
    for ln in ACCOUNTS.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "added_sub2api" in ln:
            n += 1
    return n


def parse_stop(hm: str) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (hm or "10:30").strip())
    if not m:
        return 10, 30
    return int(m.group(1)), int(m.group(2))


def should_stop_time(h: int, mi: int) -> bool:
    n = datetime.now()
    return (n.hour, n.minute) >= (h, mi)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    stop_h, stop_m = parse_stop(STOP_HM)
    baseline = count_full()
    target_total = baseline + TARGET_NEW_FULL

    if not ov.acquire_lock():
        print("LOCK held — another runner active, exit")
        return 2

    ov.log("=" * 60)
    ov.log(
        f"BATCH START target_new={TARGET_NEW_FULL} baseline_full={baseline} "
        f"goal_total={target_total} stop={stop_h:02d}:{stop_m:02d}"
    )
    ov.log(f"pid={os.getpid()} HARD_TIMEOUT={ov.HARD_TIMEOUT}s")

    # detect VPN for log
    try:
        import grokreg.delivery.gsheets_export as gse

        vpn = gse.detect_exit_ip_country()
        ov.log(f"VPN/IP now: {vpn.get('label')}")
    except Exception as e:
        ov.log(f"VPN detect skip: {e}")

    try:
        import subprocess

        from grokreg.core import winhide

        subprocess.run(
            ["powercfg", "/change", "standby-timeout-ac", "0"],
            capture_output=True,
            timeout=5,
            **winhide.kwargs(),
        )
    except Exception:
        pass

    stats = {"runs": 0, "ok": 0, "full": 0, "fail": 0, "timeout": 0}
    run_id = 0
    stop_reason = "unknown"

    try:
        while True:
            if should_stop_time(stop_h, stop_m):
                stop_reason = f"time>={stop_h:02d}:{stop_m:02d}"
                break
            cur = count_full()
            gained = cur - baseline
            if gained >= TARGET_NEW_FULL:
                stop_reason = f"reached +{gained} FULL (target {TARGET_NEW_FULL})"
                break

            free = ov.free_ram_gb()
            # Soft threshold: only pause when critically low (was 1.2 — stuck all morning)
            if free < 0.55:
                ov.log(f"LOW RAM {free}GB — pause 2min")
                time.sleep(120)
                continue

            run_id += 1
            stats["runs"] += 1
            ov.log(
                f"progress full={cur} (+{gained}/{TARGET_NEW_FULL}) "
                f"runs={stats['runs']} ok={stats['ok']} fail={stats['fail']}"
            )
            status, elapsed = ov.run_one(run_id)

            if status and status.startswith("added_sub2api"):
                stats["full"] += 1
                stats["ok"] += 1
                pause = random.uniform(*ov.PAUSE_OK)
                ov.log(f"FULL OK — sleep {pause:.0f}s")
                time.sleep(pause)
            elif ov.is_success(status):
                stats["ok"] += 1
                pause = random.uniform(*ov.PAUSE_OK)
                ov.log(f"REG OK (not full sub2api) — sleep {pause:.0f}s status={status[:60]}")
                time.sleep(pause)
            else:
                stats["fail"] += 1
                if "timeout" in (status or "").lower():
                    stats["timeout"] += 1
                ov.log(f"FAIL — short pause status={status[:80]}")
                time.sleep(random.uniform(*ov.PAUSE_FAIL))

            if run_id % 8 == 0:
                runs = ROOT / "chrome_runs"
                if runs.exists():
                    dirs = sorted(
                        [p for p in runs.iterdir() if p.is_dir()],
                        key=lambda p: p.stat().st_mtime,
                    )
                    for d in dirs[:-5]:
                        try:
                            import shutil

                            shutil.rmtree(d, ignore_errors=True)
                        except Exception:
                            pass
    finally:
        pass

    final_full = count_full()
    gained = final_full - baseline
    ov.log("=" * 60)
    ov.log("BATCH SUMMARY")
    ov.log(f"  stop_reason={stop_reason}")
    ov.log(f"  baseline_full={baseline} final_full={final_full} gained={gained}")
    for k, v in stats.items():
        ov.log(f"  {k}={v}")
    ov.log(
        f"  success_rate={100 * stats['ok'] / max(1, stats['runs']):.1f}%"
    )
    ov.log("BATCH RUNNER STOPPED")

    # Push sheet (required)
    exit_code = 0
    try:
        import grokreg.tools.export_morning_report as emr

        rc = int(emr.main())
        if rc == 0:
            ov.log("Sheet push OK after batch")
        else:
            exit_code = 2
            ov.log("PUSH SHEET FAILED")
    except Exception as e:
        exit_code = 2
        ov.log(f"PUSH SHEET FAILED: {e!r}")

    ov.release_lock()
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        ov.log(f"FATAL {e!r}")
        ov.release_lock()
        raise
    finally:
        ov.release_lock()
