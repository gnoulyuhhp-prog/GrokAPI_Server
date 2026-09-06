"""
Global stop control — press ESC in the tool console to abort everything.

- Soft: writes data/STOP (batch loop ends between accounts)
- Hard: in-process flag checked by sleeps / register_one → abort ASAP
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("grok_tool")

ROOT = Path(__file__).resolve().parents[2]
STOP_FILE = ROOT / "data" / "STOP"

_stop_event = threading.Event()
_reason = ""
_listener_started = False
_listener_lock = threading.Lock()
_listener_thread: Optional[threading.Thread] = None


class StopRequested(Exception):
    """Raised when user pressed ESC / STOP file / request_stop()."""

    def __init__(self, reason: str = "stop"):
        self.reason = reason or "stop"
        super().__init__(f"Stop requested: {self.reason}")


def stop_path() -> Path:
    return STOP_FILE


def is_stop_requested() -> bool:
    if _stop_event.is_set():
        return True
    try:
        if STOP_FILE.exists():
            return True
    except Exception:
        pass
    return False


def stop_reason() -> str:
    if _reason:
        return _reason
    if STOP_FILE.exists():
        return "STOP file"
    return ""


def request_stop(reason: str = "user", *, write_file: bool = True) -> None:
    """Signal all workers to stop. Idempotent."""
    global _reason
    if not _reason:
        _reason = reason
    _stop_event.set()
    if write_file:
        try:
            STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
            STOP_FILE.write_text(f"stop:{reason}\n", encoding="utf-8")
        except Exception as e:
            log.debug("write STOP file failed: %s", e)
    # One clear line for the user
    try:
        msg = f"\n🛑  DỪNG — đã nhận lệnh ({reason}). Đang dừng mọi việc...\n"
        sys.stdout.write(msg)
        sys.stdout.flush()
    except Exception:
        pass
    log.warning("STOP requested: %s", reason)


def clear_stop() -> None:
    """Clear stop state at start of a new run."""
    global _reason
    _stop_event.clear()
    _reason = ""
    try:
        if STOP_FILE.exists():
            STOP_FILE.unlink()
    except Exception:
        pass


def raise_if_stop() -> None:
    if is_stop_requested():
        raise StopRequested(stop_reason() or "stop")


def sleep_interruptible(seconds: float, *, chunk: float = 0.35) -> None:
    """Blocking sleep that aborts quickly when ESC/STOP is requested."""
    remaining = max(0.0, float(seconds))
    chunk = max(0.15, float(chunk))
    while remaining > 0:
        raise_if_stop()
        step = min(chunk, remaining)
        time.sleep(step)
        remaining -= step
    raise_if_stop()


async def interruptible_sleep(seconds: float, *, chunk: float = 0.35) -> None:
    """asyncio.sleep that aborts quickly when ESC/STOP is requested."""
    import asyncio

    remaining = max(0.0, float(seconds))
    chunk = max(0.15, float(chunk))
    while remaining > 0:
        raise_if_stop()
        step = min(chunk, remaining)
        await asyncio.sleep(step)
        remaining -= step
    raise_if_stop()


def _read_esc_windows() -> bool:
    try:
        import msvcrt  # type: ignore
    except ImportError:
        return False
    hit = False
    # Drain buffer; any ESC (0x1b) counts
    while msvcrt.kbhit():
        ch = msvcrt.getch()
        if ch in (b"\x1b", b"\x03"):  # ESC or Ctrl+C as raw
            hit = True
        # Extended keys: first byte 0x00 or 0xe0 then second — ignore
        if ch in (b"\x00", b"\xe0") and msvcrt.kbhit():
            msvcrt.getch()
    return hit


def _read_esc_posix() -> bool:
    try:
        import select
        import termios
        import tty
    except ImportError:
        return False
    if not sys.stdin.isatty():
        return False
    try:
        if not select.select([sys.stdin], [], [], 0)[0]:
            return False
        # Non-blocking single char
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch == "\x1b"
    except Exception:
        return False


def _listener_loop() -> None:
    log.info("ESC listener ON — nhấn phím ESC trong cửa sổ này để DỪNG ngay")
    while True:
        try:
            if _stop_event.is_set():
                # still watch for more ESC presses (no-op)
                time.sleep(0.5)
                continue
            hit = False
            if os.name == "nt":
                hit = _read_esc_windows()
            else:
                hit = _read_esc_posix()
            if hit:
                request_stop("ESC")
            time.sleep(0.08 if os.name == "nt" else 0.15)
        except Exception:
            time.sleep(0.5)


def start_esc_listener() -> None:
    """Start daemon thread that watches for ESC. Safe to call multiple times."""
    global _listener_started, _listener_thread
    with _listener_lock:
        if _listener_started:
            return
        # Need a real console for key reads
        try:
            if not sys.stdin.isatty() and os.name != "nt":
                log.debug("ESC listener skip: stdin not a TTY")
                # On Windows double-click console, stdin may still allow msvcrt
                if os.name != "nt":
                    return
        except Exception:
            pass
        t = threading.Thread(
            target=_listener_loop,
            name="esc-stop-listener",
            daemon=True,
        )
        t.start()
        _listener_thread = t
        _listener_started = True
        try:
            sys.stdout.write(
                "\n  ⌨️  Nhấn ESC = DỪNG ngay mọi việc (hoặc Ctrl+C)\n\n"
            )
            sys.stdout.flush()
        except Exception:
            pass
