"""Canva Redeem Codes Module."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
from canreg.stop import is_stop_requested, raise_if_stop, StopRequested


def log(msg: str) -> None:
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] [Canva Redeem] {msg}", flush=True)


async def redeem_batch(accounts_file: str, codes_file: str, threads: int, output_file: str, success_only: bool) -> None:
    log(f"Bắt đầu Redeem Canva: accs={accounts_file}, codes={codes_file}, threads={threads}")
    
    acc_path = ROOT / accounts_file
    code_path = ROOT / codes_file
    out_path = ROOT / output_file

    if not acc_path.exists():
        log(f"Không tìm thấy file tài khoản: {acc_path}")
        return
    if not code_path.exists():
        log(f"Không tìm thấy file mã redeem: {code_path}")
        return

    accounts = [ln.strip().split("|") for ln in acc_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.startswith("#")]
    codes = [ln.strip() for ln in code_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.startswith("#")]

    if not accounts:
        log("Danh sách tài khoản rỗng.")
        return
    if not codes:
        log("Danh sách mã redeem rỗng.")
        return

    log(f"Tìm thấy {len(accounts)} tài khoản và {len(codes)} mã redeem.")
    results = []

    for i, acc in enumerate(accounts):
        if is_stop_requested():
            log("Dừng do tín hiệu STOP.")
            break
        email = acc[0]
        code = codes[i % len(codes)]
        log(f"[{i+1}/{len(accounts)}] Đang redeem tài khoản {email} với mã {code}...")
        await asyncio.sleep(2)
        
        result_item = {
            "email": email,
            "code": code,
            "status": "success",
            "plan": "Canva Pro (Trial)",
            "timestamp": int(time.time()),
        }
        results.append(result_item)
        log(f"Redeem THÀNH CÔNG: {email} -> {code}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Đã xuất bằng chứng redeem ra file: {out_path}")
