"""
Styled terminal log for Grok register tool.
Display-only — does not change status / accounts / Sub2API / Sheet payloads.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Optional

try:
    from colorama import Fore, Style, init as colorama_init

    colorama_init(autoreset=True)
    _HAS_COLOR = True
except Exception:  # pragma: no cover
    _HAS_COLOR = False

    class _Dummy:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = _Dummy()  # type: ignore
    Style = _Dummy()  # type: ignore


# Windows consoles often default to cp1252 — force UTF-8 for Vietnamese + emoji
def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_ensure_utf8_stdio()

_lock = threading.Lock()
_ctx = threading.local()


def _ctx_get(name: str, default: Any) -> Any:
    return getattr(_ctx, name, default)


def set_task(task_id: int, cur: int, total: int, email: str = "") -> None:
    _ctx.task_id = max(1, int(task_id))
    _ctx.cur = max(1, int(cur))
    _ctx.total = max(1, int(total))
    if email:
        _ctx.email = email


def set_email(email: str) -> None:
    _ctx.email = email or ""


def _tid() -> int:
    return int(_ctx_get("task_id", 1) or 1)


def _out(line: str) -> None:
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            try:
                sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
            except Exception:
                try:
                    print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)
                except Exception:
                    pass


def banner(n_acc: int, threads: int = 1, mode: Optional[str] = None) -> None:
    """Start-of-run line."""
    mode = mode or "Ẩn cửa sổ Off-Screen (Bypass Cloudflare 100%)"
    text = (
        f"Bắt đầu: {n_acc} Acc | {threads} Luồng song song | Mode: {mode}"
    )
    _out(f"{Fore.CYAN}{Style.BRIGHT}{text}{Style.RESET_ALL}")


def task_header(email: str = "") -> None:
    """1] Task #1/1 · Email: ..."""
    em = email or _ctx_get("email", "") or ""
    if em:
        _ctx.email = em
    tid = _tid()
    cur = int(_ctx_get("cur", 1) or 1)
    total = int(_ctx_get("total", 1) or 1)
    prefix = f"{tid}]"
    email_col = f"{Fore.MAGENTA}{em}{Style.RESET_ALL}"
    _out(
        f"{Fore.WHITE}{Style.BRIGHT}{prefix} Task #{cur}/{total} · Email: {email_col}"
    )


def api(icon: str, msg: str, *, color: str = "blue") -> None:
    """
    1] [GROK-API] {icon} {msg}
    color: blue | green | yellow | red | magenta | white
    """
    tid = _tid()
    cmap = {
        "blue": Fore.CYAN,
        "cyan": Fore.CYAN,
        "green": Fore.GREEN,
        "yellow": Fore.YELLOW,
        "red": Fore.RED,
        "magenta": Fore.MAGENTA,
        "white": Fore.WHITE,
    }
    c = cmap.get(color, Fore.CYAN)
    tag = f"{Fore.BLUE}{Style.BRIGHT}[GROK-API]{Style.RESET_ALL}"
    # icon + message: tint by status color
    body = f"{c}{icon} {msg}{Style.RESET_ALL}"
    _out(f"{tid}] {tag} {body}")


def api_ok(msg: str) -> None:
    api("✅", msg, color="green")


def api_wait(msg: str) -> None:
    api("⏳", msg, color="yellow")


def api_err(msg: str) -> None:
    api("❌", msg, color="red")


def api_info(icon: str, msg: str) -> None:
    api(icon, msg, color="blue")


def progress(completed: int, total: int, ok: int) -> None:
    """Machine-readable batch progress consumed by the web console."""
    if os.environ.get("GROK_WEB_CONSOLE") != "1":
        return
    done = max(0, int(completed))
    target = max(0, int(total))
    success = max(0, int(ok))
    failed = max(0, done - success)
    _out(
        f"@@JOB_PROGRESS completed={done} total={target} "
        f"ok={success} failed={failed}"
    )


def success_block(
    email: str,
    password: str,
    user_id: str = "",
    sso_token: str = "",
) -> None:
    """Final success block matching sample."""
    api("🎉", "DỪNG TẠO TÀI KHOẢN GROK THÀNH CÔNG!", color="green")
    tid = _tid()
    tag = f"{Fore.BLUE}{Style.BRIGHT}[GROK-API]{Style.RESET_ALL}"
    def row(label: str, value: str) -> None:
        em_col = (
            f"{Fore.MAGENTA}{value}{Style.RESET_ALL}"
            if label.strip().lower().startswith("email")
            else f"{Fore.GREEN}{value}{Style.RESET_ALL}"
        )
        _out(f"{tid}] {tag}    {label}: {em_col}")

    row("Email  ", email)
    row("Pass   ", password)
    row("UserId ", user_id or "(n/a)")
    tok = sso_token or "(n/a)"
    if len(tok) > 24 and tok != "(n/a)":
        tok = tok[:20] + "..."
    row("SSO Token ", tok)


def sub2api_ok(email: str) -> None:
    _out(
        f"{Fore.YELLOW}{Style.BRIGHT}⚡{Style.RESET_ALL} "
        f"{Fore.CYAN}[Grok_Bot_Tool -> Sub2API]{Style.RESET_ALL} "
        f"Tự động nạp tài khoản {Fore.MAGENTA}{email}{Style.RESET_ALL} vào Sub2API thành công!"
    )


def sub2api_fail(email: str, reason: str = "") -> None:
    extra = f" ({reason})" if reason else ""
    _out(
        f"{Fore.RED}{Style.BRIGHT}❌{Style.RESET_ALL} "
        f"{Fore.CYAN}[Grok_Bot_Tool -> Sub2API]{Style.RESET_ALL} "
        f"Nạp tài khoản {Fore.MAGENTA}{email}{Style.RESET_ALL} vào Sub2API thất bại{extra}"
    )


def sheet_ok(email: str, sheet_name: str = "Grok Acc Trắng") -> None:
    _out(
        f"{Fore.GREEN}{Style.BRIGHT}✅{Style.RESET_ALL} "
        f"Đã đồng bộ tài khoản {Fore.MAGENTA}{email}{Style.RESET_ALL} "
        f"lên Google Sheet [{sheet_name}] thành công!"
    )


def sheet_fail(email: str, reason: str = "") -> None:
    extra = f" ({reason})" if reason else ""
    _out(
        f"{Fore.RED}{Style.BRIGHT}❌{Style.RESET_ALL} "
        f"Đồng bộ Google Sheet thất bại cho {Fore.MAGENTA}{email}{Style.RESET_ALL}{extra}"
    )


def quiet_technical_logs() -> None:
    """Hide noisy INFO/WARNING from grok-reg / pydoll — style_log is the user-facing channel."""
    import logging

    # ERROR+ only for technical logger so terminal stays style-clean
    logging.getLogger("grok-reg").setLevel(logging.ERROR)
    for name in ("pydoll", "websockets", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.ERROR)
