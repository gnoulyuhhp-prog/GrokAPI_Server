"""Claude / Anthropic Registration & Session Capture Flow."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import string
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
GROK_TOOL_DIR = ROOT.parent / "grok_tool"
if str(GROK_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(GROK_TOOL_DIR))

from grokreg.core.config import load_config
from grokreg.core.runtime import log as grok_log
from grokreg.browser.chrome import open_or_attach_browser, close_browser_handle, navigate_signup_with_cf
from grokreg.browser.jsutil import _exec_js
from grokreg.mail.providers import MailApiClient, MailTmProvider, AzpopMailProvider, wait_otp_smart
from grokreg.mail.tmail_wibu import TmailWibuProvider
from grokreg.reg.flow import acquire_email_session
from claudereg.stop import is_stop_requested, raise_if_stop, StopRequested


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [Claude] {msg}", flush=True)


def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "@#$"
    return "".join(random.choices(chars, k=length))


def append_account(email: str, password_or_token: str, status: str) -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    acc_file = data_dir / "accounts.txt"
    line = f"{email}|{password_or_token}|{status}\n"
    with open(acc_file, "a", encoding="utf-8") as f:
        f.write(line)
    log(f"Lưu kết quả tài khoản: {email} | {status}")


async def register_single(mail_type: str, backend: str, password: str) -> bool:
    raise_if_stop()
    log(f"Bắt đầu đăng ký Claude (mail_type={mail_type}, backend={backend})...")

    # 1. Khởi tạo email THẬT từ provider
    cfg = load_config()
    if mail_type == "1":
        cfg["email_provider"] = "hotmail"
    elif mail_type == "2":
        cfg["email_provider"] = "azpopmail"
    elif mail_type == "3":
        cfg["email_provider"] = "tmail_wibu"
    else:
        cfg["email_provider"] = "auto_temp"

    mailtm = MailTmProvider()
    azpop = AzpopMailProvider(cfg.get("azpopmail") or {})
    tmail = TmailWibuProvider(cfg.get("tmail_wibu") or {})
    mail_api = MailApiClient(cfg)

    log("Đang tạo hộp thư email thực tế...")
    try:
        email_session, hotmail = acquire_email_session(cfg, mailtm, azpop, tmail)
        email = email_session.address
        log(f"Email nhận mã thành công: {email} ({email_session.provider})")
    except Exception as e:
        log(f"Lỗi tạo hòm thư email: {e}")
        return False

    raise_if_stop()

    # 2. Mặc định dùng Chrome ẩn (Bypass Cloudflare) cho Claude
    success, session_key = await register_browser(cfg, email, email_session, mail_api, mailtm, hotmail, azpop, tmail)

    if success:
        token_save = session_key or password
        status_str = "success:session_captured" if session_key else "success:registered"
        append_account(email, token_save, status_str)
        log(f"Đăng ký Claude THÀNH CÔNG: {email} -> {status_str}")
        return True
    else:
        log(f"Đăng ký Claude THẤT BẠI: {email} (bỏ qua, không lưu vào accounts.txt)")
        return False


async def register_browser(config: dict[str, Any], email: str, email_session: Any, mail_api: Any, mailtm: Any, hotmail: Any, azpop: Any, tmail: Any) -> tuple[bool, str]:
    log("Khởi động Chrome ẩn (Bypass Cloudflare) để đăng ký Claude...")
    handle = None
    try:
        cfg = dict(config)
        cfg["chrome_background"] = True
        handle = await open_or_attach_browser(cfg)
        tab = handle.tab

        log("Điều hướng tới https://claude.ai/login...")
        await navigate_signup_with_cf(tab, cfg, "https://claude.ai/login")
        
        # Chờ form login xuất hiện sau khi bypass Cloudflare
        log("Chờ form đăng nhập Claude sẵn sàng...")
        for _ in range(25):
            has_input = await _exec_js(tab, """
                (() => {
                    const input = document.querySelector('input[type="email"], input[name="email"], input[autocomplete="email"], input[placeholder*="email" i], input[type="text"]:not([name*="cf"])');
                    return !!input;
                })()
            """)
            if has_input:
                break
            await asyncio.sleep(1.5)

        # Điền email qua React native setter
        log(f"Nhập email: {email}")
        await _exec_js(tab, f"""
            (() => {{
                const input = document.querySelector('input[type="email"], input[name="email"], input[autocomplete="email"], input[placeholder*="email" i], input[type="text"]:not([name*="cf"])');
                if (input) {{
                    input.focus();
                    try {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        nativeSetter.call(input, '{email}');
                    }} catch (_) {{
                        input.value = '{email}';
                    }}
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
                return false;
            }})()
        """)
        await asyncio.sleep(1)

        # Bấm nút Continue with email / Submit
        await _exec_js(tab, """
            (() => {
                const btn = document.querySelector('button[type="submit"]') ||
                            document.querySelector('form button') ||
                            Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục|email|login|gửi/i.test(b.innerText));
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
        """)
        log("Đã gửi email đăng ký trên giao diện Claude. Chờ OTP...")

        # Chờ nhận OTP thực tế
        otp = wait_otp_smart(
            email_session,
            mail_api,
            mailtm,
            hotmail,
            timeout=120,
            azpop=azpop,
            tmail_wibu=tmail,
        )

        if not otp:
            log("Không nhận được OTP từ Claude.")
            return False, ""

        log(f"Đã nhận OTP: {otp}. Đang điền vào giao diện Claude...")
        await _exec_js(tab, f"""
            (() => {{
                const codeInputs = document.querySelectorAll('input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name="code"], input[placeholder*="code" i], input[maxlength="1"]');
                if (codeInputs.length >= 6) {{
                    for (let i = 0; i < 6; i++) {{
                        try {{
                            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                            nativeSetter.call(codeInputs[i], '{otp}'[i] || '');
                        }} catch (_) {{
                            codeInputs[i].value = '{otp}'[i] || '';
                        }}
                        codeInputs[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                        codeInputs[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }} else if (codeInputs.length === 1) {{
                    try {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        nativeSetter.call(codeInputs[0], '{otp}');
                    }} catch (_) {{
                        codeInputs[0].value = '{otp}';
                    }}
                    codeInputs[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
                    codeInputs[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                const btn = document.querySelector('button[type="submit"]') || Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục|verify|xác thực/i.test(b.innerText));
                if (btn) btn.click();
            }})()
        """)
        await asyncio.sleep(6)

        # Xử lý Onboarding nếu có (Name & 18+ policy)
        await _exec_js(tab, """
            (() => {
                const nameInput = document.querySelector('input[name="name"], input[placeholder*="name" i], input[autocomplete="name"]');
                if (nameInput) {
                    try {
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        nativeSetter.call(nameInput, 'Claude User');
                    } catch (_) {
                        nameInput.value = 'Claude User';
                    }
                    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
                    nameInput.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                checkboxes.forEach(c => { if (!c.checked) c.click(); });
                const btns = Array.from(document.querySelectorAll('button')).filter(b => /continue|tiếp tục|agree|đồng ý|start|bắt đầu|acknowledge/i.test(b.innerText));
                btns.forEach(b => b.click());
            })()
        """)
        await asyncio.sleep(4)

        # Bắt sessionKey cookies
        cookies = []
        try:
            from grokreg.delivery.sso_capture import _cdp_get_cookies
            cookies = await _cdp_get_cookies(tab)
        except Exception as e:
            log(f"Lỗi đọc cookies CDP: {e}")

        session_key = ""
        for c in cookies:
            if c.get("name") == "sessionKey":
                session_key = c.get("value", "")
                break

        if session_key:
            log(f"Đã bắt thành công Claude sessionKey: {session_key[:12]}...")
            return True, session_key
        else:
            log("Hoàn tất phiên đăng ký Claude trên trình duyệt.")
            return True, ""

    except Exception as e:
        log(f"Lỗi trình duyệt khi reg Claude: {e}")
        return False, ""
    finally:
        if handle:
            await close_browser_handle(handle)


async def run_batch(mail_type: str, count: int, backend: str) -> None:
    log(f"Bắt đầu chuỗi đăng ký Claude: số lượng={count}, mail={mail_type}, backend={backend}")
    done = 0
    while True:
        if is_stop_requested():
            log("Nhận tín hiệu STOP. Dừng chuỗi đăng ký.")
            break
        if count > 0 and done >= count:
            log(f"Đã hoàn thành chỉ tiêu {count} tài khoản.")
            break

        pwd = generate_password()
        try:
            await register_single(mail_type, backend, pwd)
        except StopRequested:
            log("Dừng do yêu cầu của người dùng.")
            break
        except Exception as e:
            log(f"Lỗi phiên đăng ký Claude: {e}")

        done += 1
        if count == 0 or done < count:
            for _ in range(3):
                if is_stop_requested():
                    break
                await asyncio.sleep(1)
