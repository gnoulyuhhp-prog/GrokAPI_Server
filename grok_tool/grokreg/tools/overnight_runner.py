#!/usr/bin/env python3
"""
Overnight 1-thread Grok reg (azpopmail). Stop at 06:00.
Stable: do NOT thrash-kill chrome mid-run; only kill previous main.py between runs.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# File lives in grokreg/tools/ — project root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
FIX_LOG = ROOT / "data" / "fix_log.txt"
LOCK = ROOT / "data" / "overnight.lock"
try:
    from grokreg.core import winhide

    PY = winhide.hidden_python(ROOT if (ROOT / "venv").is_dir() else ROOT.parent.parent)
except Exception:
    PY = ROOT / "venv" / "Scripts" / "pythonw.exe"
    if not PY.exists():
        PY = ROOT / "venv" / "Scripts" / "python.exe"
    if not PY.exists():
        PY = Path(sys.executable)

STOP_HOUR = 6
HARD_TIMEOUT = 360  # full reg+sub2api can take 3-5 min
PAUSE_OK = (50, 90)
PAUSE_FAIL = (10, 20)


def now() -> datetime:
    return datetime.now()


def log(msg: str) -> None:
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(FIX_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


def should_stop() -> bool:
    return now().hour >= STOP_HOUR


def free_ram_gb() -> float:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return round(st.ullAvailPhys / (1024**3), 2)
    except Exception:
        pass
    return 4.0


def kill_main_only() -> None:
    """Kill leftover main.py only — NEVER kill overnight_runner, NEVER taskkill all chrome."""
    my = os.getpid()
    try:
        from grokreg.core import winhide

        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                rf"""
$ErrorActionPreference='SilentlyContinue'
$keep={my}
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -match 'python' -and $_.CommandLine -match 'main\.py' -and $_.ProcessId -ne $keep -and $_.CommandLine -notmatch 'overnight'
}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}
""",
            ],
            capture_output=True,
            timeout=12,
            **winhide.kwargs(),
        )
    except Exception as e:
        log(f"kill_main_only: {e}")


def kill_debug_chrome() -> None:
    """Kill ONLY tool automation Chrome (chrome_runs / debug-port+grok_tool). Never user Chrome."""
    try:
        import grokreg.browser.chrome_cleanup as chrome_clean

        rep = chrome_clean.kill_tool_chrome(reason="overnight_pre_post_run")
        log(
            f"kill_debug_chrome: matched={rep.get('matched_before')} "
            f"killed={rep.get('killed_count')} remaining_tool={rep.get('remaining_tool')} "
            f"total_chrome={rep.get('total_chrome')}"
        )
    except Exception as e:
        log(f"kill_debug_chrome: {e}")


def acquire_lock() -> bool:
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            old = int(LOCK.read_text(encoding="utf-8").strip().split()[0])
            # Windows: OpenProcess to check live
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, old)  # PROCESS_QUERY_LIMITED
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return False  # still alive
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        except Exception:
            try:
                LOCK.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n".encode())
                os.close(fd)
                return True
            except Exception:
                return False
    except Exception:
        return True


def release_lock() -> None:
    try:
        if LOCK.exists() and str(os.getpid()) in LOCK.read_text(encoding="utf-8"):
            LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def parse_status(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        if "Done. status=" in line:
            return line.split("Done. status=", 1)[-1].strip()
        if "Saved" in line and "|" in line:
            return line.split("|")[-1].strip()
    return ""


def is_success(status: str) -> bool:
    s = (status or "").lower()
    return (
        s == "success"
        or s.startswith("added_sub2api")
        or s.startswith("success_sub2api")
        or "manual_check" in s
    )


def run_one(run_id: int) -> tuple[str, float]:
    log_dir = ROOT / "data" / "overnight_logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"run_{run_id}_{int(time.time())}.log"

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["GROK_SKIP_KILL_OLD"] = "1"

    # Between runs only: stop previous main + its debug chrome
    log(f"run#{run_id}: pre-clean main+debug chrome")
    kill_main_only()
    kill_debug_chrome()
    time.sleep(1.5)

    # 0 = auto_temp: prefer healthier of azpopmail ↔ tmail_wibu, switch on lag
    provider_arg = os.environ.get("GROK_EMAIL_PROVIDER", "0").strip() or "0"
    cmd = [str(PY), str(ROOT / "main.py"), provider_arg]
    t0 = time.time()
    status = ""
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            from grokreg.core import winhide

            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                **winhide.kwargs(new_group=True),
            )
            log(f"START run#{run_id} pid={proc.pid} log={log_path.name}")
            last_sz = 0
            last_prog = time.time()
            while True:
                ret = proc.poll()
                elapsed = time.time() - t0
                try:
                    sz = log_path.stat().st_size
                    if sz > last_sz:
                        last_sz = sz
                        last_prog = time.time()
                except Exception:
                    pass

                if ret is not None:
                    status = parse_status(log_path) or f"exit={ret}"
                    break

                # stall diagnose (no log growth 120s)
                if time.time() - last_prog > 120:
                    log(f"STALL run#{run_id} no log 120s — tail diagnose")
                    try:
                        tail = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-8:]
                        for ln in tail:
                            log(f"  | {ln[:160]}")
                    except Exception:
                        pass
                    last_prog = time.time()

                if elapsed > HARD_TIMEOUT:
                    log(f"TIMEOUT run#{run_id} >{HARD_TIMEOUT}s — kill main only")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    kill_main_only()
                    kill_debug_chrome()
                    status = parse_status(log_path) or "error:hard_timeout"
                    break
                time.sleep(2)
    except Exception as e:
        status = f"error:runner:{e}"
        log(status)

    elapsed = time.time() - t0
    # post clean
    kill_main_only()
    kill_debug_chrome()
    log(f"END run#{run_id} {elapsed:.0f}s status={status}")
    return status, elapsed


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not acquire_lock():
        print("LOCK held — another overnight running, exit")
        return 2

    log("=" * 60)
    log(
        "OVERNIGHT STABLE START - 1 thread temp auto_temp "
        f"(provider_arg={os.environ.get('GROK_EMAIL_PROVIDER', '0')}) - stop 06:00"
    )
    log(f"pid={os.getpid()} reason=stable_runner_v2")
    log(f"HARD_TIMEOUT={HARD_TIMEOUT}s per acc")

    try:
        from grokreg.core import winhide

        subprocess.run(
            ["powercfg", "/change", "standby-timeout-ac", "0"],
            capture_output=True,
            timeout=5,
            **winhide.kwargs(),
        )
    except Exception:
        pass

    stats = {"runs": 0, "ok": 0, "fail": 0, "timeout": 0}
    run_id = 0
    last_health = 0.0

    while not should_stop():
        if time.time() - last_health > 1800:
            free = free_ram_gb()
            log(f"HEALTH free_ram={free}GB")
            last_health = time.time()
            if free < 0.55:
                log(f"LOW RAM {free}GB — pause 2min")
                time.sleep(120)
                continue

        run_id += 1
        stats["runs"] += 1
        status, elapsed = run_one(run_id)

        if is_success(status):
            stats["ok"] += 1
            pause = random.uniform(*PAUSE_OK)
            log(f"SUCCESS — sleep {pause:.0f}s before next")
            time.sleep(pause)
        else:
            stats["fail"] += 1
            if "timeout" in (status or "").lower():
                stats["timeout"] += 1
            log(f"FAIL — short pause then retry with NEW mail")
            time.sleep(random.uniform(*PAUSE_FAIL))

        if run_id % 10 == 0:
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
                log(f"pruned chrome_runs (kept 5 of {len(dirs)})")

    log("=" * 60)
    log("OVERNIGHT SUMMARY")
    for k, v in stats.items():
        log(f"  {k}={v}")
    log(f"  success_rate={100*stats['ok']/max(1,stats['runs']):.1f}%")
    log("OVERNIGHT RUNNER STOPPED")

    # === REQUIRED: push Google Sheet (must succeed) ===
    # python export_morning_report.py — retries 3x; exit 2 = PUSH SHEET FAILED
    exit_code = 0
    try:
        import grokreg.tools.export_morning_report as emr

        rc = int(emr.main())
        if rc == 0:
            log("Morning report: Google Sheet push OK")
        else:
            exit_code = 2
            log("PUSH SHEET FAILED")
            log(f"CRITICAL export_morning_report exit={rc} — sheet not updated")
    except Exception as e:
        exit_code = 2
        log("PUSH SHEET FAILED")
        log(f"CRITICAL export exception: {e!r}")

    release_lock()
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"FATAL {e!r}")
        try:
            log("PUSH SHEET FAILED")
        except Exception:
            pass
        release_lock()
        raise
    finally:
        release_lock()
