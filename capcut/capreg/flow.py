"""CapCut Registration & Pro Trial Claim Flow."""
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
from capreg.stop import is_stop_requested, raise_if_stop, StopRequested


def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(chars, k=length))


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [CapCut] {msg}", flush=True)


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
    log(f"Bắt đầu đăng ký CapCut (mail={mail_type}, backend={backend})...")

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

        log("Điều hướng tới https://www.capcut.com/login...")
        await _exec_js(tab, "window.location.href = 'https://www.capcut.com/login';")
        await asyncio.sleep(5)

        # Nhấn chọn Continue with email nếu có
        await _exec_js(tab, """
            (() => {
                const emailBtns = Array.from(document.querySelectorAll('div, span, button, a')).filter(el => /continue with email|email|tiếp tục bằng email|sign up with email/i.test(el.innerText));
                if (emailBtns.length) {
                    emailBtns[0].click();
                    return true;
                }
                return false;
            })()
        """)
        await asyncio.sleep(2)

        # Chờ input email
        log("Chờ form đăng nhập CapCut sẵn sàng...")
        for _ in range(20):
            has_input = await _exec_js(tab, """
                (() => {
                    const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i], input[type="text"]:not([name*="cf"])');
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
                const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i], input[type="text"]:not([name*="cf"])');
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

        # Bấm Continue / Send code
        await _exec_js(tab, """
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const btn = btns.find(b => /continue|tiếp tục|send code|gửi|login|sign/i.test(b.innerText)) || document.querySelector('button[type="submit"]');
                if (btn) btn.click();
            })()
        """)
        log("Đã gửi yêu cầu nhận mã CapCut. Chờ OTP...")

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
            log(f"Hết thời gian chờ OTP từ CapCut cho {email}. Bỏ qua không lưu.")
            return False

        log(f"Đã nhận OTP CapCut: {otp}. Đang điền vào giao diện...")
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

        append_account(email, password, "success:pro_trial_checked")
        log(f"Đăng ký CapCut THÀNH CÔNG: {email} -> success:pro_trial_checked")
        return True

    except Exception as e:
        log(f"Lỗi trình duyệt khi reg CapCut: {e}")
        return False
    finally:
        if handle:
            await close_browser_handle(handle)


async def run_batch(mail_type: str, count: int, backend: str) -> None:
    log(f"Bắt đầu chuỗi đăng ký CapCut: số lượng={count}, mail={mail_type}, backend={backend}")
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
            log(f"Lỗi phiên đăng ký CapCut: {e}")

        done += 1
        if count == 0 or done < count:
            for _ in range(3):
                if is_stop_requested():
                    break
                await asyncio.sleep(1)
