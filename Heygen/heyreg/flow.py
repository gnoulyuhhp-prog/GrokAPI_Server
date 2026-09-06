"""HeyGen Auto Registration & Magic Link Flow."""
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
from heyreg.stop import is_stop_requested, raise_if_stop, StopRequested


def generate_password(length: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choices(chars, k=length))


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [HeyGen] {msg}", flush=True)


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
    log(f"Bắt đầu đăng ký HeyGen (mail={mail_type}, backend={backend})...")

    # 1. Khởi tạo email
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

    # 2. Chạy Chrome ẩn cho HeyGen
    handle = None
    try:
        cfg["chrome_background"] = True
        handle = await open_or_attach_browser(cfg)
        tab = handle.tab

        log("Điều hướng tới https://app.heygen.com/signup...")
        await tab.go_to("https://app.heygen.com/signup")
        await asyncio.sleep(4)

        # Chờ input email
        log("Chờ form đăng ký HeyGen sẵn sàng...")
        for _ in range(20):
            has_input = await _exec_js(tab, """
                (() => {
                    const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i]');
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
                const input = document.querySelector('input[type="email"], input[name="email"], input[placeholder*="email" i], input[type="text"]');
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

        # Bấm Send a secure magic link (loại trừ Google, Apple, SSO)
        clicked_btn = await _exec_js(tab, """
            (() => {
                const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
                const btn = btns.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    if (t.includes('google') || t.includes('apple') || t.includes('sso')) return false;
                    return t.includes('magic link') || t.includes('send') || t.includes('tiếp tục') || t.includes('continue');
                }) || document.querySelector('button[type="submit"]');
                if (btn) {
                    btn.click();
                    return btn.innerText || 'clicked';
                }
                return null;
            })()
        """)
        log(f"Đã bấm nút gửi HeyGen: {clicked_btn}. Chờ Magic Link...")

        # Chờ Magic Link hoặc OTP từ hộp thư
        magic_link = None
        otp = None
        deadline = time.time() + 90
        username = email.split("@")[0]
        domain = email.split("@")[1] if "@" in email else ""

        while time.time() < deadline:
            raise_if_stop()
            try:
                if email_session.provider == "azpopmail":
                    msgs, _ = azpop._list_messages(username, domain)
                    for m in (msgs or []):
                        subj = str(m.get("subject") or "")
                        frm = str(m.get("from") or "")
                        if "heygen" in subj.lower() or "heygen" in frm.lower() or "magic link" in subj.lower():
                            mid = m.get("id")
                            body = azpop._message_body(username, domain, mid)
                            links = re.findall(r'href=["\'](https?://[^"\']+)["\']', body)
                            for l in links:
                                if "auth.heygen.com/magic-web" in l or "heygen.com/magic" in l:
                                    magic_link = l
                                    break
                if magic_link:
                    break
            except StopRequested:
                raise
            except Exception as e:
                log(f"Lỗi kiểm tra hòm thư HeyGen: {e}")

            await asyncio.sleep(3)

        if magic_link:
            log(f"Đã bắt được HeyGen Magic Link: {magic_link[:60]}...")
            log("Điều hướng tới Magic Link để hoàn tất xác thực...")
            await _exec_js(tab, f"window.location.href = '{magic_link}';")
            await asyncio.sleep(8)

            # Xử lý nút Onboarding nếu có
            await _exec_js(tab, """
                (() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const continueBtn = btns.find(b => /continue|get started|tiếp tục|bắt đầu|skip/i.test(b.innerText));
                    if (continueBtn) continueBtn.click();
                })()
            """)
            await asyncio.sleep(3)

            append_account(email, password, "success:magic_link_registered")
            log(f"Đăng ký HeyGen THÀNH CÔNG (Magic Link): {email}")
            return True

        log(f"Hết thời gian chờ Magic Link từ HeyGen cho {email}. Bỏ qua không lưu.")
        return False

    except Exception as e:
        log(f"Lỗi trình duyệt khi reg HeyGen: {e}")
        return False
    finally:
        if handle:
            await close_browser_handle(handle)


async def run_batch(mail_type: str, count: int, backend: str) -> None:
    log(f"Bắt đầu chuỗi đăng ký HeyGen: số lượng={count}, mail={mail_type}, backend={backend}")
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
            log(f"Lỗi phiên đăng ký HeyGen: {e}")

        done += 1
        if count == 0 or done < count:
            for _ in range(3):
                if is_stop_requested():
                    break
                await asyncio.sleep(1)
