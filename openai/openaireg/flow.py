"""OpenAI / ChatGPT Registration & Session Capture Flow."""
from __future__ import annotations

import argparse
import asyncio
import json
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
from grokreg.browser.chrome import open_or_attach_browser, close_browser_handle, navigate_signup_with_cf
from grokreg.browser.jsutil import _exec_js
from grokreg.mail.providers import MailApiClient, MailTmProvider, AzpopMailProvider, wait_otp_smart
from grokreg.mail.tmail_wibu import TmailWibuProvider
from grokreg.reg.flow import acquire_email_session
from openaireg.stop import is_stop_requested, raise_if_stop, StopRequested


def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "@#$"
    return "".join(random.choices(chars, k=length))


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [OpenAI] {msg}", flush=True)


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
    log(f"Bắt đầu đăng ký OpenAI (mail_type={mail_type}, backend={backend})...")

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

    # 2. Xử lý đăng ký qua Chrome ẩn
    success, token = await register_browser(cfg, email, password, email_session, mail_api, mailtm, hotmail, azpop, tmail)

    if success:
        token_save = token or password
        status_str = "success:session_captured" if token else "success:registered"
        append_account(email, token_save, status_str)
        log(f"Đăng ký OpenAI THÀNH CÔNG: {email} -> {status_str}")
        return True
    else:
        log(f"Đăng ký OpenAI THẤT BẠI: {email} (bỏ qua, không lưu vào accounts.txt)")
        return False


async def register_browser(config: dict[str, Any], email: str, password: str, email_session: Any, mail_api: Any, mailtm: Any, hotmail: Any, azpop: Any, tmail: Any) -> tuple[bool, str]:
    log("Khởi động Chrome ẩn để đăng ký OpenAI / ChatGPT...")
    handle = None
    try:
        cfg = dict(config)
        cfg["chrome_background"] = True
        handle = await open_or_attach_browser(cfg)
        tab = handle.tab

        log("Điều hướng tới https://chatgpt.com/...")
        await navigate_signup_with_cf(tab, cfg, "https://chatgpt.com/")
        await asyncio.sleep(4)

        # Bấm Sign up nếu đang ở trang chọn Login / Sign up
        await _exec_js(tab, """
            (() => {
                const btns = Array.from(document.querySelectorAll('button, a'));
                const signupBtn = btns.find(b => /sign up|đăng ký|create account|log in/i.test(b.innerText));
                if (signupBtn) signupBtn.click();
            })()
        """)
        await asyncio.sleep(3)

        # Chờ form nhập email
        log("Chờ form đăng ký OpenAI sẵn sàng...")
        for _ in range(20):
            has_input = await _exec_js(tab, """
                (() => {
                    const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i], input#email-input');
                    return !!input;
                })()
            """)
            if has_input:
                break
            await asyncio.sleep(1.5)

        # Nhập email
        log(f"Nhập email: {email}")
        await _exec_js(tab, f"""
            (() => {{
                const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i], input#email-input, input[type="text"]');
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

        # Bấm Continue
        await _exec_js(tab, """
            (() => {
                const btn = document.querySelector('button[type="submit"]') ||
                            Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục/i.test(b.innerText));
                if (btn) btn.click();
            })()
        """)
        await asyncio.sleep(4)

        # Điền password nếu OpenAI yêu cầu mật khẩu
        await _exec_js(tab, f"""
            (() => {{
                const pwd = document.querySelector('input[type="password"], input[name="password"]');
                if (pwd) {{
                    pwd.focus();
                    try {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        nativeSetter.call(pwd, '{password}');
                    }} catch (_) {{
                        pwd.value = '{password}';
                    }}
                    pwd.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    pwd.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    const btn = document.querySelector('button[type="submit"]') ||
                                Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục/i.test(b.innerText));
                    if (btn) btn.click();
                }}
            }})()
        """)

        log("Đã gửi yêu cầu đăng ký OpenAI. Đang kiểm tra OTP / Link xác nhận...")
        # Chờ OTP
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
            log("Không nhận được mã xác thực từ OpenAI.")
            return False, ""

        log(f"Đã nhận mã OTP OpenAI: {otp}. Đang điền vào trang...")
        await _exec_js(tab, f"""
            (() => {{
                const codeInputs = document.querySelectorAll('input[inputmode="numeric"], input[autocomplete="one-time-code"], input[name="code"], input[placeholder*="code" i]');
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
                const btn = document.querySelector('button[type="submit"]') || Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục|verify/i.test(b.innerText));
                if (btn) btn.click();
            }})()
        """)
        await asyncio.sleep(6)

        # Xử lý nhập Tên & Ngày sinh nếu có
        await _exec_js(tab, """
            (() => {
                const nameInput = document.querySelector('input[name="name"], input[placeholder*="name" i]');
                if (nameInput) {
                    nameInput.value = 'OpenAI User';
                    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
                const bday = document.querySelector('input[name="birthday"], input[placeholder*="birth" i]');
                if (bday) {
                    bday.value = '01/01/2000';
                    bday.dispatchEvent(new Event('input', { bubbles: true }));
                }
                const btn = document.querySelector('button[type="submit"]') || Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục|agree|start/i.test(b.innerText));
                if (btn) btn.click();
            })()
        """)
        await asyncio.sleep(5)

        # Trích xuất cookies
        cookies = []
        try:
            from grokreg.delivery.sso_capture import _cdp_get_cookies
            cookies = await _cdp_get_cookies(tab)
        except Exception as e:
            log(f"Lỗi đọc CDP cookies: {e}")

        session_token = ""
        for c in cookies:
            if "session-token" in c.get("name", "") or c.get("name") == "accessToken":
                session_token = c.get("value", "")
                break

        if session_token:
            log(f"Đã bắt session token OpenAI: {session_token[:15]}...")
            return True, session_token

        return True, ""
    except Exception as e:
        log(f"Lỗi trình duyệt khi reg OpenAI: {e}")
        return False, ""
    finally:
        if handle:
            await close_browser_handle(handle)


async def run_batch(mail_type: str, count: int, backend: str) -> None:
    log(f"Bắt đầu chuỗi đăng ký OpenAI: số lượng={count}, mail={mail_type}, backend={backend}")
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
            log(f"Lỗi phiên đăng ký OpenAI: {e}")

        done += 1
        if count == 0 or done < count:
            for _ in range(3):
                if is_stop_requested():
                    break
                await asyncio.sleep(1)
