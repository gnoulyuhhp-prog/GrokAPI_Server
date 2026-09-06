"""Canva Registration Flow."""
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
from canreg.stop import is_stop_requested, raise_if_stop, StopRequested


def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(chars, k=length))


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [Canva] {msg}", flush=True)


def append_account(email: str, password: str, status: str) -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    acc_file = data_dir / "accounts.txt"
    line = f"{email}|{password}|{status}\n"
    with open(acc_file, "a", encoding="utf-8") as f:
        f.write(line)
    log(f"Lưu tài khoản: {email} | {status}")


async def register_single(mail_type: str, backend: str, password: str) -> bool:
    raise_if_stop()
    log(f"Bắt đầu đăng ký Canva (mail={mail_type}, backend={backend})...")

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

    try:
        email_session, hotmail = acquire_email_session(cfg, mailtm, azpop, tmail)
        email = email_session.address
        log(f"Email nhận mã: {email} ({email_session.provider})")
    except Exception as e:
        log(f"Lỗi tạo email: {e}")
        return False

    raise_if_stop()

    handle = None
    try:
        cfg["chrome_background"] = True
        handle = await open_or_attach_browser(cfg)
        tab = handle.tab

        log("Điều hướng tới https://www.canva.com/signup...")
        await _exec_js(tab, "window.location.href = 'https://www.canva.com/signup';")
        await asyncio.sleep(5)

        # Chờ nút Continue with email
        await _exec_js(tab, """
            (() => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"], a'));
                const btn = btns.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('google') || t.includes('apple') || t.includes('facebook')) return false;
                    return t.includes('continue with email') || t.includes('tiếp tục bằng email') || t.includes('email');
                });
                if (btn) btn.click();
            })()
        """)
        await asyncio.sleep(2)

        # Nhập email
        log(f"Nhập email: {email}")
        await _exec_js(tab, f"""
            (() => {{
                const input = document.querySelector('input[name="username"], input[type="email"], input[name="email"], input[placeholder*="email" i], input[type="text"]');
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
                const btns = Array.from(document.querySelectorAll('button'));
                const btn = btns.find(b => /continue|tiếp tục/i.test(b.innerText)) || document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            })()
        """)
        await asyncio.sleep(3)

        # Xử lý trường Tên nếu có (What's your name)
        await _exec_js(tab, """
            (() => {
                const nameInput = document.querySelector('input[name="name"], input[placeholder*="name" i], input[placeholder*="tên" i]');
                if (nameInput) {
                    nameInput.focus();
                    try {
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        nativeSetter.call(nameInput, 'Alex Morgan');
                    } catch (_) {
                        nameInput.value = 'Alex Morgan';
                    }
                    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
                    nameInput.dispatchEvent(new Event('change', { bubbles: true }));
                    const btns = Array.from(document.querySelectorAll('button'));
                    const btn = btns.find(b => /create|tạo|continue|tiếp tục/i.test(b.innerText)) || document.querySelector('button[type="submit"]');
                    if (btn) btn.click();
                }
            })()
        """)
        log("Đã gửi yêu cầu nhận mã Canva. Chờ OTP...")

        # Chờ OTP
        otp = wait_otp_smart(
            email_session,
            mail_api,
            mailtm,
            hotmail,
            timeout=100,
            azpop=azpop,
            tmail_wibu=tmail,
        )

        if not otp:
            log(f"Hết thời gian chờ OTP từ Canva cho {email}. Bỏ qua không lưu.")
            return False

        log(f"Đã nhận OTP Canva: {otp}. Đang điền vào giao diện...")
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
                const btn = document.querySelector('button[type="submit"]') || Array.from(document.querySelectorAll('button')).find(b => /continue|tiếp tục|verify/i.test(b.innerText));
                if (btn) btn.click();
            }})()
        """)
        await asyncio.sleep(5)

        append_account(email, password, "success:registered")
        log(f"Đăng ký Canva THÀNH CÔNG: {email}")
        return True

    except Exception as e:
        log(f"Lỗi trình duyệt khi reg Canva: {e}")
        return False
    finally:
        if handle:
            await close_browser_handle(handle)


async def run_batch(mail_type: str, count: int, backend: str) -> None:
    log(f"Bắt đầu chuỗi đăng ký Canva: số lượng={count}, mail={mail_type}, backend={backend}")
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
            log(f"Lỗi phiên đăng ký Canva: {e}")

        done += 1
        if count == 0 or done < count:
            for _ in range(3):
                if is_stop_requested():
                    break
                await asyncio.sleep(1)
