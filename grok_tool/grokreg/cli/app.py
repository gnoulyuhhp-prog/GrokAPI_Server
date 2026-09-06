"""Auto-split from main.py — modular package."""
from __future__ import annotations

import argparse
import asyncio
import copy
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


from grokreg.core.config import load_config
from grokreg.core.cleanup import kill_old_runs
from grokreg.reg.flow import register_one

def parse_provider_choice(raw: str | None) -> Optional[str]:
    """Map user input → provider mode | None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("1", "hotmail", "outlook", "ms", "h"):
        return "hotmail"
    if s in ("0", "temp", "tempmail", "auto_temp", "failover", "smart", "s"):
        return "auto_temp"
    if s in ("2", "azpopmail", "azpop", "a"):
        return "azpopmail"
    if s in (
        "3",
        "tmail_wibu",
        "tmailwibu",
        "wibu",
        "wibucrypto",
        "tmail",
        "t",
    ):
        return "tmail_wibu"
    if s in ("4", "racing", "race", "fastest", "r"):
        return "racing"
    if s in ("5", "tinyhost", "tiny"):
        return "tinyhost"
    if s in ("6", "tempmail_lol", "lol"):
        return "tempmail_lol"
    if s in ("7", "tempmail_vip", "vip"):
        return "tempmail_vip"
    if s in ("8", "mailtm", "mail.tm", "m"):
        return "mailtm"
    return None


def pick_email_provider(cli_choice: str | None = None) -> str:
    """
    User picks provider.
      CLI:  python main.py 0 | 1 | 2 | 3 | 4
            0 = auto_temp (azpop↔wibu failover)
            4 = racing (TinyHost / Lol / VIP fastest)
      Menu: interactive if no CLI choice
    """
    chosen = parse_provider_choice(cli_choice)
    if chosen:
        return chosen

    # Non-interactive (piped / no TTY): default auto_temp for runners
    if not sys.stdin.isatty():
        return "auto_temp"

    print()
    print("=" * 56)
    print("  GROK REGISTER — chọn nguồn email")
    print("=" * 56)
    print("  0) Temp smart  (azpop ↔ wibu, tự đổi khi lag)  [khuyên dùng]")
    print("  1) Hotmail     (hotmails.txt — 1 acc tạo 5 Grok)")
    print("  2) Temp only   azpopmail.com")
    print("  3) Temp only   tmail.wibucrypto.pro")
    print("  4) Temp Racing (đua TinyHost / TempMail.lol / VIP lấy nhanh nhất)")
    print("  5) TinyHost    (tinyhost.shop)")
    print("  6) TempMail.lol")
    print("=" * 56)
    labels = {
        "auto_temp": "Temp smart (azpop↔wibu failover)",
        "hotmail": "Hotmail",
        "azpopmail": "Temp only azpopmail.com",
        "tmail_wibu": "Temp only tmail.wibucrypto.pro",
        "racing": "Temp Racing (Fastest Inbox)",
        "tinyhost": "TinyHost",
        "tempmail_lol": "TempMail.lol",
        "tempmail_vip": "TempMailVIP",
        "mailtm": "Mail.tm",
    }
    while True:
        try:
            ans = input("Chọn [0-6] (Enter=0): ").strip() or "0"
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit("Đã hủy.")
        chosen = parse_provider_choice(ans)
        if chosen in labels:
            print(f"→ Dùng {labels[chosen]}\n")
            return chosen
        print("  Chỉ nhập từ 0 đến 6.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Grok register: 0=auto_temp failover | 1=Hotmail | "
            "2=azpopmail | 3=tmail.wibucrypto.pro"
        ),
    )
    p.add_argument(
        "choice",
        nargs="?",
        default=None,
        help="0 auto_temp | 1 hotmail | 2 azpop | 3 wibu",
    )
    p.add_argument(
        "--provider",
        "-p",
        dest="provider",
        default=None,
        help="hotmail | auto_temp | azpopmail | tmail_wibu | mailtm",
    )
    p.add_argument(
        "--count",
        "-n",
        type=int,
        default=None,
        help=(
            "How many accounts this run (default: config batch_count or 1). "
            "Use 0 = run forever until data/STOP or Ctrl+C"
        ),
    )
    p.add_argument(
        "--backend",
        "-b",
        dest="backend",
        default=None,
        choices=("browser", "protocol", "github", "auto"),
        help=(
            "browser=Chrome UI | protocol=HTTP (~30s, solver :5072) "
            "| auto=protocol rồi fallback Chrome"
        ),
    )
    p.add_argument(
        "--threads",
        "-t",
        type=int,
        default=None,
        help="Parallel workers for github HTTP backend (1-12)",
    )
    return p


def _successish(status: Any) -> bool:
    st = str(status or "")
    return (
        st == "success"
        or st.startswith("added_sub2api")
        or st.startswith("success_sub2api")
        or st.startswith("added_sub2api_untested")
    )


async def run_parallel_github(
    config: dict[str, Any],
    *,
    batch: int,
    threads: int,
    until_stop: bool = False,
) -> tuple[int, int]:
    """Run real concurrent HTTP registrations; each worker owns one OS thread."""
    from grokreg.protocol.worker import register_one_github

    worker_count = max(1, min(12, int(threads)))
    if not until_stop:
        worker_count = min(worker_count, max(1, int(batch)))

    counter_lock = asyncio.Lock()
    next_attempt = 1
    completed = 0
    ok_n = 0
    total_disp = 0 if until_stop else max(1, int(batch))
    stop_file = ROOT / "data" / "STOP"

    async def claim_attempt() -> int | None:
        nonlocal next_attempt
        async with counter_lock:
            if not until_stop and next_attempt > batch:
                return None
            current = next_attempt
            next_attempt += 1
            return current

    def run_one(worker_id: int, attempt: int):
        # Config is copied because protocol registration normalizes provider keys.
        worker_config = copy.deepcopy(config)
        slog.set_task(worker_id, attempt, 999 if until_stop else batch)
        slog.api_info(
            "🧵",
            f"Worker {worker_id}/{worker_count} bắt đầu account #{attempt}",
        )
        return register_one_github(worker_config)

    async def worker(worker_id: int) -> None:
        nonlocal completed, ok_n
        while not is_stop_requested() and not stop_file.exists():
            attempt = await claim_attempt()
            if attempt is None:
                return
            try:
                result = await asyncio.to_thread(run_one, worker_id, attempt)
                status = result.status
                if result.ok:
                    slog.api_ok(
                        f"Worker {worker_id} HTTP OK {result.email} "
                        f"in {result.duration_sec:.1f}s → {status}"
                    )
            except StopRequested:
                return
            except Exception as exc:
                log.exception("parallel worker %s crashed: %s", worker_id, exc)
                slog.api_err(f"Worker {worker_id} crashed: {exc}")
                status = f"error:{exc}"

            async with counter_lock:
                completed += 1
                if _successish(status):
                    ok_n += 1
                progress_done = completed
                progress_ok = ok_n
            slog.progress(progress_done, total_disp, progress_ok)

            if until_stop or completed < batch:
                try:
                    await interruptible_sleep(0.5 if _successish(status) else 1.5)
                except StopRequested:
                    return

    slog.api_info(
        "🧵",
        f"MULTI-THREAD THẬT: {worker_count} worker HTTP chạy đồng thời",
    )
    await asyncio.gather(*(worker(i + 1) for i in range(worker_count)))
    return ok_n, completed


async def main(argv: list[str] | None = None) -> None:  # noqa: C901
    args = build_arg_parser().parse_args(argv)
    cli = args.provider or args.choice
    provider = pick_email_provider(cli)

    # Parallel stress workers set GROK_SKIP_KILL_OLD=1 so siblings are not killed
    if os.environ.get("GROK_SKIP_KILL_OLD", "").strip() not in ("1", "true", "yes"):
        # Only kill orphaned Temp Chrome — never kill live CDP on our profile port
        kill_old_runs(also_chrome=True, keep_pid=os.getpid())
    else:
        log.info("GROK_SKIP_KILL_OLD=1 — skip killing other main.py / chrome")

    config = load_config()
    # User choice always wins — ignore config auto
    config["email_provider"] = provider
    # reg_backend: browser (default) | protocol | auto
    # CLI override: --backend protocol
    if getattr(args, "backend", None):
        config["reg_backend"] = str(args.backend).strip().lower()

    backend_now = str(
        config.get("reg_backend")
        or (config.get("protocol") or {}).get("mode")
        or "browser"
    ).strip().lower()
    if backend_now in ("protocol", "auto", "http", "pure_http", "github", "castle"):
        try:
            from services.solver_manager import start_async

            start_async(config)
            log.info("Turnstile solver: auto-start background (:5072)")
        except Exception as e:
            log.debug("solver auto-start: %s", e)

    # Optional per-worker overrides (stress_test.py / multi-instance)
    if os.environ.get("GROK_CHROME_PORT", "").strip():
        config["chrome_debug_port"] = int(os.environ["GROK_CHROME_PORT"])
    if os.environ.get("GROK_CHROME_PROFILE", "").strip():
        config["chrome_user_data_dir"] = os.environ["GROK_CHROME_PROFILE"]
        config["fresh_profile_per_account"] = False
    if os.environ.get("GROK_SUB2API", "").strip().lower() in ("0", "false", "no", "off"):
        config.setdefault("sub2api", {})
        config["sub2api"]["enabled"] = False
        log.info("GROK_SUB2API=0 — Sub2API import disabled for this run")

    # Durable Sub2API delivery worker (competitor pattern: reg ≠ upload)
    try:
        from grokreg.delivery.delivery_retry import ensure_worker, process_queue_once, queue_stats

        ensure_worker(config)
        drained = process_queue_once(config, limit=5)
        stats = queue_stats()
        if drained or stats.get("pending"):
            log.info(
                "delivery queue: drained=%s pending=%s done=%s failed=%s",
                drained,
                stats.get("pending"),
                stats.get("done"),
                stats.get("failed"),
            )
    except Exception as e:
        log.debug("delivery worker init: %s", e)

    # ESC / STOP control — clear leftover STOP, start keyboard listener
    clear_stop()
    start_esc_listener()

    # --count 0 (or negative) = loop until STOP file / ESC / Ctrl+C
    raw_count = args.count
    if raw_count is None:
        batch = int(config.get("batch_count") or 1)
    else:
        batch = int(raw_count)
    until_stop = batch <= 0
    if not until_stop:
        batch = max(1, batch)
    dmin = float(config.get("inter_success_delay_min") or 45)
    dmax = float(config.get("inter_success_delay_max") or 90)
    requested_threads = max(
        1,
        min(
            12,
            int(
                args.threads
                if args.threads is not None
                else (os.environ.get("GROK_THREADS") or config.get("threads") or 1)
            ),
        ),
    )
    if backend_now in ("github", "http", "pure_http"):
        threads = requested_threads if until_stop else min(requested_threads, batch)
    else:
        threads = 1

    stop_file = ROOT / "data" / "STOP"

    log.info(
        "User selected email_provider=%s | batch=%s until_stop=%s | fresh_profile=%s | humanize=%s",
        provider,
        "∞" if until_stop else batch,
        until_stop,
        config.get("fresh_profile_per_account", True),
        config.get("humanize", True),
    )

    # Styled banner (display only)
    slog.banner(999 if until_stop else batch, threads=threads)
    slog.api_info(
        "⌨️",
        "Nhấn ESC = DỪNG ngay mọi việc  |  Ctrl+C cũng được  |  menu [2] ghi data/STOP",
    )
    if until_stop:
        slog.api_info(
            "♾️",
            "Chạy liên tục đến khi ESC / Ctrl+C / data/STOP",
        )

    if requested_threads > threads:
        reason = (
            f"Số lượng={batch} nên chỉ cần {threads} worker"
            if backend_now in ("github", "http", "pure_http")
            else f"Backend {backend_now} dùng Chrome/Castle nên ép 1 luồng"
        )
        slog.api_info("⚠️", reason)

    if threads > 1 and backend_now in ("github", "http", "pure_http"):
        ok_n, attempts = await run_parallel_github(
            config,
            batch=batch,
            threads=threads,
            until_stop=until_stop,
        )
        log.info(
            "Parallel batch done: %s/%s success-ish with %s workers",
            ok_n,
            attempts,
            threads,
        )
        slog.api_ok(
            f"Kết thúc đa luồng: {ok_n} OK / {attempts} lượt · {threads} workers"
        )
        return

    ok_n = 0
    i = 0
    try:
        while True:
            if is_stop_requested():
                slog.api_info(
                    "🛑",
                    f"Dừng theo lệnh ({stop_reason() or 'STOP'}) — {i} lượt, ok={ok_n}",
                )
                break

            i += 1
            if not until_stop and i > batch:
                break
            # STOP file / ESC between accounts
            if is_stop_requested() or stop_file.exists():
                slog.api_info(
                    "🛑",
                    f"Gặp STOP/ESC — dừng sau {i - 1} lượt (ok={ok_n})",
                )
                try:
                    if stop_file.exists():
                        stop_file.unlink()
                except Exception:
                    pass
                break

            total_disp = 999 if until_stop else batch
            log.info(
                "======== ACCOUNT %s / %s ========",
                i,
                "∞" if until_stop else batch,
            )
            slog.set_task(task_id=i, cur=i, total=total_disp)
            try:
                raise_if_stop()
                backend = str(
                    config.get("reg_backend")
                    or (config.get("protocol") or {}).get("mode")
                    or "browser"
                ).strip().lower()
                if backend in ("github", "http", "pure_http"):
                    slog.api_info("⚡", "Backend GITHUB (HTTP thuần — 0 Chrome)")
                    from grokreg.protocol.worker import register_one_github

                    def _run_github():
                        return register_one_github(config)

                    result = await asyncio.to_thread(_run_github)
                    status = result.status
                    if result.ok:
                        slog.api_ok(
                            f"GitHub-HTTP OK {result.email} in {result.duration_sec:.1f}s → {status}"
                        )
                elif backend in ("protocol", "castle"):
                    slog.api_info("⚡", "Backend PROTOCOL (HTTP + Castle Chrome)")
                    from grokreg.protocol.worker import register_one_protocol

                    def _run_proto():
                        return register_one_protocol(config, castle=True)

                    result = await asyncio.to_thread(_run_proto)
                    status = result.status
                    if result.ok:
                        slog.api_ok(
                            f"Protocol OK {result.email} in {result.duration_sec:.1f}s → {status}"
                        )
                elif backend == "auto":
                    slog.api_info("⚡", "Backend AUTO — thử HTTP GitHub trước")
                    from grokreg.protocol.worker import register_one_github

                    result = await asyncio.to_thread(register_one_github, config)
                    if result.ok and (
                        result.status.startswith("added_sub2api")
                        or result.status == "success"
                        or result.status.startswith("success_sub2api")
                    ):
                        status = result.status
                        slog.api_ok(
                            f"Protocol OK {result.email} in {result.duration_sec:.1f}s"
                        )
                    else:
                        log.warning(
                            "protocol failed (%s) — fallback browser",
                            result.status,
                        )
                        slog.api_info("↩️", "Protocol fail — fallback browser UI")
                        status = await register_one(config)
                else:
                    status = await register_one(config)
            except StopRequested as e:
                status = "stopped"
                slog.api_info("🛑", f"ESC/STOP giữa reg — {e.reason}")
            except KeyboardInterrupt:
                request_stop("Ctrl+C")
                raise
            except Exception as e:
                log.exception("register_one crashed: %s", e)
                slog.api_err(f"register_one crashed: {e}")
                status = f"error:{e}"
            st = str(status or "")
            if st == "stopped":
                slog.api_info("🛑", f"Đã dừng (ok={ok_n} / lượt={i})")
                break
            successish = (
                st == "success"
                or st.startswith("added_sub2api")
                or st.startswith("success_sub2api")
                or st.startswith("added_sub2api_untested")
            )
            if successish:
                ok_n += 1
            elif st.startswith("error:"):
                slog.api_err(st)
            slog.progress(i, 0 if until_stop else batch, ok_n)

            # more work?
            more = until_stop or i < batch
            if more:
                if is_stop_requested() or stop_file.exists():
                    slog.api_info("🛑", "Gặp STOP/ESC — dừng ngay")
                    try:
                        if stop_file.exists():
                            stop_file.unlink()
                    except Exception:
                        pass
                    break
                is_http = backend in ("github", "http", "pure_http", "protocol")
                if successish:
                    pause = 0.5 if is_http else af.inter_account_cooldown(dmin, dmax)
                    log.info(
                        "Inter-success cooldown %.1fs before next account... (ESC=dừng)",
                        pause,
                    )
                else:
                    pause = 1.5 if is_http else af.human_delay(max(3.0, dmin * 0.5), min(dmax, 5.0))
                    log.info(
                        "Inter-attempt pause %.1fs before next account... (ESC=dừng)",
                        pause,
                    )
                try:
                    await interruptible_sleep(pause)
                except StopRequested:
                    slog.api_info("🛑", "ESC trong lúc chờ — dừng batch")
                    break
    except StopRequested as e:
        slog.api_info("🛑", f"ESC/STOP — dừng (đã chạy {i} lượt, ok={ok_n}) [{e.reason}]")
    except KeyboardInterrupt:
        request_stop("Ctrl+C", write_file=True)
        slog.api_info("🛑", f"Ctrl+C — dừng (đã chạy {i} lượt, ok={ok_n})")

    if until_stop:
        log.info("Loop done: %s success-ish / %s attempts", ok_n, i)
        slog.api_ok(f"Kết thúc loop: {ok_n} OK / {i} lượt")
    else:
        log.info("Batch done: %s/%s success-ish", ok_n, batch)


if __name__ == "__main__":
    asyncio.run(main())


