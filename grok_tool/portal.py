#!/usr/bin/env python3
"""
Grok API Customer Portal & Balance Checker & 1-Click Codex Setup (Windows + Linux/macOS).
Accurately synced with Sub2API Quota Enforcement & Instant Real-Time Streaming.
Port: 8082
"""
import http.server
import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8082
BASE_URL = "https://grokapi.vorte.me/v1"
USD_TO_VND = 25600.0
API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_+=-]{16,256}")
SETUP_MODES = {
    "fast": {"effort": "low", "summary": "none", "verbosity": "low", "idle_timeout_ms": "60000", "max_completion_tokens": "4096"},
    "smart": {"effort": "medium", "summary": "auto", "verbosity": "medium", "idle_timeout_ms": "120000", "max_completion_tokens": "8192"},
    "thinking": {"effort": "high", "summary": "auto", "verbosity": "medium", "idle_timeout_ms": "300000", "max_completion_tokens": "16384"},
}


def normalize_setup_mode(mode: str) -> str:
    """Return a supported customer mode; Smart is the safe default."""
    normalized = (mode or "").strip().lower()
    return normalized if normalized in SETUP_MODES else "smart"


def is_valid_api_key(api_key: str) -> bool:
    """Only allow key characters that are safe in generated shell scripts and SQL."""
    return bool(API_KEY_RE.fullmatch(api_key.strip()))

def format_ts(ts_str: str) -> str:
    if not ts_str:
        return "—"
    try:
        clean = ts_str.split(".")[0]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%H:%M:%S %d/%m/%Y")
    except Exception:
        return ts_str[:19]

def query_key_info(api_key: str) -> dict:
    clean_key = api_key.strip()
    if not is_valid_api_key(clean_key):
        return {"ok": False, "error": "API Key không hợp lệ (phải bắt đầu bằng sk-)"}
    
    # 1. Query Key Info from Postgres
    sql_key = f"SELECT id, name, status, quota, quota_used, created_at, last_used_at FROM api_keys WHERE key = '{clean_key}' AND deleted_at IS NULL LIMIT 1;"
    cmd_key = ["docker", "exec", "sub2api-postgres", "psql", "-U", "sub2api", "-d", "sub2api", "-t", "-A", "-F", "|", "-c", sql_key]
    try:
        res = subprocess.check_output(cmd_key, timeout=5).decode("utf-8", errors="ignore").strip()
    except Exception as e:
        return {"ok": False, "error": f"Lỗi truy vấn hệ thống: {e}"}
    
    if not res:
        return {"ok": False, "error": "Không tìm thấy API Key này trên hệ thống. Vui lòng kiểm tra lại!"}
    
    parts = res.split("|")
    key_id = parts[0]
    name = parts[1] if len(parts) > 1 else "Grok Key"
    status = parts[2] if len(parts) > 2 else "active"
    quota_usd = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
    quota_used_usd = float(parts[4]) if len(parts) > 4 and parts[4] else 0.0
    created_at = parts[5] if len(parts) > 5 else ""
    last_used = parts[6] if len(parts) > 6 else "Chưa sử dụng"
    
    # Base Token Calculation directly from Sub2API Quota ($1 = 500,000 tokens)
    max_tokens = round(quota_usd * 500000)
    remain_usd = max(0.0, quota_usd - quota_used_usd)
    remain_tokens = round(remain_usd * 500000) if quota_usd > 0 else 0
    used_tokens = max(0, max_tokens - remain_tokens)
    remain_pct = round((remain_usd / quota_usd) * 100, 1) if quota_usd > 0 else 0.0

    # 2. Query Usage Logs for this key
    logs = []
    sql_logs = f"SELECT model, input_tokens, output_tokens, total_cost, duration_ms, created_at FROM usage_logs WHERE api_key_id = {key_id} ORDER BY id DESC LIMIT 50;"
    cmd_logs = ["docker", "exec", "sub2api-postgres", "psql", "-U", "sub2api", "-d", "sub2api", "-t", "-A", "-F", "|", "-c", sql_logs]
    try:
        res_logs = subprocess.check_output(cmd_logs, timeout=5).decode("utf-8", errors="ignore").strip()
        if res_logs:
            for line in res_logs.splitlines():
                if not line.strip():
                    continue
                lp = line.split("|")
                if len(lp) >= 6:
                    model = lp[0]
                    in_tok = int(lp[1] or 0)
                    out_tok = int(lp[2] or 0)
                    cost_usd = float(lp[3] or 0.0)
                    cost_vnd = round(cost_usd * USD_TO_VND)
                    dur_ms = int(lp[4] or 0)
                    ts_raw = lp[5]
                    
                    logs.append({
                        "time": format_ts(ts_raw),
                        "model": model,
                        "status": "Oke",
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "tokens_display": f"{in_tok:,} / {out_tok:,}",
                        "cost_vnd": f"{cost_vnd}đ" if cost_vnd > 0 else "< 1đ",
                        "cost_usd": f"${cost_usd:.6f}",
                        "latency": f"{dur_ms / 1000:.3f}s" if dur_ms >= 1000 else f"{dur_ms}ms",
                    })
    except Exception as e:
        print("Error fetching logs:", e)

    return {
        "ok": True,
        "name": name,
        "key_masked": clean_key[:12] + "..." + clean_key[-6:],
        "full_key": clean_key,
        "status": status if remain_tokens > 0 else "exhausted",
        "quota_usd": quota_usd,
        "quota_used_usd": quota_used_usd,
        "max_tokens": max_tokens,
        "used_tokens": used_tokens,
        "remain_tokens": remain_tokens,
        "remain_pct": remain_pct,
        "created_at": format_ts(created_at),
        "last_used": format_ts(last_used) if last_used and last_used != "Chưa sử dụng" else "Chưa sử dụng",
        "models": ["grok-4.6"],
        "base_url": BASE_URL,
        "logs": logs,
    }

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Grok API — Tra Cứu Số Dư & Cài Đặt 1-Click</title>
  <style>
    :root { --muted:#94a3b8; --border:rgba(255,255,255,.1); --mono:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace; }
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; }
    body { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    /* Preserve the established portal layout; enhancements below are additive. */
    body { background: #090d16; color: #f1f5f9; min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 30px 16px; background-image: radial-gradient(circle at 50% 0%, rgba(56,189,248,.12) 0%, transparent 60%); }
    .container { width: 100%; max-width: 840px; }
    .header { text-align: center; margin-bottom: 24px; }
    .header h1 { font-size: 26px; font-weight: 800; color: #fff; display: flex; align-items: center; justify-content: center; gap: 10px; margin: 0; letter-spacing: 0; }
    .header p { font-size: 13px; color: #94a3b8; margin-top: 6px; }
    .card { background: rgba(15,23,42,.85); border: 1px solid rgba(255,255,255,.1); border-radius: 16px; padding: 22px; margin-bottom: 20px; backdrop-filter: blur(12px); box-shadow: 0 10px 30px rgba(0,0,0,.4); }
    .input-group { display: flex; gap: 8px; margin-top: 10px; }
    input[type="text"] { flex: 1; min-width: 0; width: auto; height: auto; background: rgba(0,0,0,.4); border: 1px solid rgba(255,255,255,.1); border-radius: 10px; padding: 12px 16px; color: #fff; font: 14px var(--mono); outline: none; }
    input[type="text"]:focus { border-color: #10b981; box-shadow: 0 0 0 2px rgba(16,185,129,.2); }
    .btn { min-height: 0; background: linear-gradient(135deg,#10b981 0%,#059669 100%); color: #fff; border: none; border-radius: 10px; padding: 12px 20px; font-weight: 700; font-size: 13px; cursor: pointer; white-space: nowrap; }
    .btn:hover { opacity: .92; transform: translateY(-1px); background: linear-gradient(135deg,#10b981 0%,#059669 100%); }
    .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }
    .stat-box { background: rgba(0,0,0,.3); border: 1px solid rgba(255,255,255,.1); border-radius: 12px; padding: 14px; }
    .stat-label { font-size: 11px; text-transform: uppercase; color: #94a3b8; font-weight: 600; }
    .stat-val { font-size: 20px; font-weight: 800; color: #fff; margin-top: 4px; }
    .battery-bar { width: 100%; height: 8px; background: rgba(255,255,255,.08); border-radius: 4px; overflow: hidden; margin-top: 14px; }
    .battery-fill { height: 100%; border-radius: 4px; transition: width .4s ease; }
    .code-box { background: rgba(0,0,0,.6); border: 1px solid rgba(56,189,248,.25); border-radius: 10px; padding: 12px 95px 12px 14px; font: 12px var(--mono); color: #38bdf8; word-break: break-all; margin-top: 8px; position: relative; min-height: 0; display: block; }
    .copy-btn { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); padding: 6px 12px; font-size: 11.5px; font-weight: 700; background: #2563eb; border: none; border-radius: 6px; color: #fff; cursor: pointer; white-space: nowrap; }
    .copy-btn:hover { background: #10b981; }
    .mode-row { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; margin-top: 10px; }
    .mode-btn { min-height: 0; text-align: center; background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.1); border-radius: 9px; color: #94a3b8; padding: 9px 7px; cursor: pointer; font-size: 11.5px; font-weight: 700; }
    .mode-btn.active { color: #fff; border-color: #38bdf8; background: rgba(56,189,248,.14); box-shadow: none; }
    .launch-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 8px; }
    .launch-box { padding: 10px 12px; border: 1px solid rgba(255,255,255,.09); border-radius: 9px; background: rgba(0,0,0,.18); }
    .launch-code { display: block; margin: 5px 0; padding: 6px 8px; border-radius: 6px; background: rgba(0,0,0,.45); color: #fff; overflow-wrap: anywhere; }
    .badge { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 700; letter-spacing: 0; }
    .badge::before { display: none; }
    .badge-active { background: rgba(16,185,129,.15); color: #10b981; border: 1px solid #10b981; }
    .badge-exhausted { background: rgba(239,68,68,.15); color: #ef4444; border: 1px solid #ef4444; }
    .table-wrap { width: 100%; overflow-x: auto; margin-top: 12px; border: 0; border-radius: 0; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; }
    th { padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,.1); background: transparent; color: #94a3b8; font-size: 11px; text-transform: uppercase; font-weight: 600; letter-spacing: 0; white-space: nowrap; }
    td { padding: 10px 8px; border-bottom: 1px solid rgba(255,255,255,.04); white-space: nowrap; }
    tbody tr:hover td { background: rgba(255,255,255,.03); }
    .tag-ok { color: #10b981; font-weight: 700; background: rgba(16,185,129,.12); padding: 2px 6px; border-radius: 4px; }
    .tok-val { font-family: var(--mono); font-weight: 600; color: #fff; }
    .cost-val { color: #f59e0b; font-weight: 700; }
    .lat-val { color: #38bdf8; font-family: var(--mono); }
    @media (max-width: 680px) {
      .launch-grid, .stat-grid { grid-template-columns: 1fr; }
      .mode-row { grid-template-columns: 1fr; }
      .input-group { display: flex; }
    }
    /* Additive polish: retain every legacy section while improving density and navigation. */
    .container { width: min(96vw, 1680px); max-width: none; display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 20px; align-items: start; }
    body { background-color: #0a0e16; background-image: radial-gradient(circle at 50% -10%, rgba(37,99,235,.12), transparent 48%); }
    .card { border-radius: 12px; background: rgba(15,21,33,.96); box-shadow: 0 12px 32px rgba(0,0,0,.26); }
    .container > .header, #key-lookup, #quick-config, #customer-shortcuts, #image-api-guide { grid-column: 1 / -1; }
    #result-box { grid-column: 1 / -1; min-width: 0; }
    #result-box[style*="display: block"] { display: block !important; }
    #result-box > .card { min-width: 0; margin-bottom: 0; }
    #result-box > .card + .card { margin-top: 20px; }
    #cline-guide { grid-column: 1 / -1; min-width: 0; }
    #more-integrations { grid-column: 1 / -1; min-width: 0; }
    #image-api-guide { min-width: 0; }
    .container > .card { margin-bottom: 0; }
    .quick-links { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px; margin: -6px 0 20px; border: 1px solid rgba(255,255,255,.09); border-radius: 10px; background: rgba(15,21,33,.82); position: sticky; top: 10px; z-index: 10; backdrop-filter: blur(12px); }
    .quick-link { display: inline-flex; align-items: center; min-height: 34px; padding: 0 12px; border: 1px solid rgba(255,255,255,.1); border-radius: 7px; color: #cbd5e1; background: rgba(255,255,255,.035); font-size: 11.5px; font-weight: 650; text-decoration: none; }
    .quick-link:hover { color: #fff; border-color: rgba(56,189,248,.5); background: rgba(56,189,248,.08); }
    .platform-note { margin-top: 6px; color: #94a3b8; font-size: 11px; }
    .client-grid { display: block; column-count: 3; column-gap: 12px; margin-top: 14px; }
    .client-card { display: inline-block; min-width: 0; width: 100%; margin: 0 0 12px; break-inside: avoid; border: 1px solid rgba(255,255,255,.09); border-radius: 10px; background: rgba(0,0,0,.18); overflow: hidden; vertical-align: top; }
    .client-card summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px; color: #fff; font-size: 12.5px; font-weight: 750; cursor: default; list-style: none; pointer-events: none; }
    .client-card summary::-webkit-details-marker { display: none; }
    .client-card summary::after { display: none; }
    .client-card summary > span:first-child { min-width: 0; overflow-wrap: anywhere; }
    .client-card > .client-card-body { display: block !important; padding: 0 14px 14px; color: #94a3b8; font-size: 11.5px; line-height: 1.65; }
    .client-card-body ol { margin: 8px 0 12px; padding-left: 18px; }
    .client-card-body a { color: #38bdf8; text-decoration: none; }
    .client-card-body a:hover { text-decoration: underline; }
    .client-tag { display: inline-flex; flex-shrink: 0; padding: 2px 7px; border-radius: 999px; background: rgba(56,189,248,.1); color: #7dd3fc; font-size: 10px; font-weight: 650; }
    .code-box pre { margin: 0; max-width: 100%; overflow: auto; }
    .code-box .copy-btn { top: 10px; transform: none; }
    .launch-box { position: relative; min-width: 0; }
    .launch-box > pre { max-width: 100%; overflow: auto; }
    .launch-box > .copy-btn { position: static; float: right; transform: none; margin: 10px 0 0 !important; }
    .launch-box::after { content: ""; display: block; clear: both; }
    #image-api-guide .launch-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    #image-api-guide .code-box { min-width: 0; max-width: 100%; }
    @media (max-width: 1180px) {
      .client-grid { column-count: 2; }
    }
    @media (max-width: 680px) {
      .container { width: min(100% - 24px, 1680px); grid-template-columns: 1fr; gap: 14px; }
      .container > .header, #key-lookup, #quick-config, #customer-shortcuts, #result-box, #cline-guide, #more-integrations, #image-api-guide { grid-column: 1; }
      .quick-links { position: static; overflow-x: auto; flex-wrap: nowrap; }
      .client-grid { column-count: 1; }
      #image-api-guide .launch-grid { grid-template-columns: 1fr; }
      .input-group { flex-direction: column; }
      .input-group .btn { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>⚡ GROK API PORTAL</h1>
      <p>Kiểm tra số dư Token, Lịch sử request & Cài đặt 1-Click Windows / Linux</p>
    </div>

    <!-- Search Card -->
    <div class="card" id="key-lookup">
      <label style="font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase;">Nhập API Key của bạn:</label>
      <div class="input-group">
        <input type="text" id="input-key" placeholder="sk-..." value="" autocomplete="off" />
        <button class="btn" onclick="checkBalance()">Tra Cứu</button>
      </div>
    </div>

    <!-- Result Container -->
    <div id="result-box" style="display: none;"></div>

    <!-- Instructions Card -->
    <div class="card" id="quick-config">
      <div style="font-size:15px;font-weight:800;margin-bottom:8px;color:#fff;">📌 CẤU HÌNH NHANH</div>
      <div style="font-size:12px;line-height:1.75;color:var(--muted);">
        <div><b style="color:#fff;">Provider:</b> OpenAI Compatible</div>
        <div><b style="color:#fff;">Base URL:</b> <code style="color:#fff;">https://grokapi.vorte.me/v1</code></div>
        <div><b style="color:#fff;">Model:</b> <code style="color:#38bdf8;">grok-4.6</code></div>
        <div><b style="color:#fff;">Dùng cho:</b> Codex, Cline, ZCode, Grok Build, chat/code và tạo ảnh.</div>
      </div>
      <div style="margin-top:9px;font-size:11px;color:#fbbf24;">
        Nhập API key hợp lệ và bấm <b>Tra Cứu</b> để mở hướng dẫn đầy đủ, lệnh cài đặt và ví dụ code.
      </div>
    </div>

    <nav class="quick-links" id="customer-shortcuts" aria-label="Điều hướng nhanh" style="display:none;">
      <a class="quick-link" href="#result-box">Số dư & cài đặt</a>
      <a class="quick-link" href="#usage-history">Lịch sử request</a>
      <a class="quick-link" href="#cline-guide">Cline / VS Code</a>
      <a class="quick-link" href="#more-integrations">Thêm ứng dụng</a>
      <a class="quick-link" href="#image-api-guide">Image API</a>
    </nav>

    <!-- Cline / VS Code Guide -->
    <div class="card" id="cline-guide" style="display:none;">
      <div style="font-size:15px;font-weight:800;color:#fff;margin-bottom:5px;">🧑‍💻 CÀI GROK API CHO CLINE TRONG VS CODE</div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:13px;line-height:1.6;">
        Cline dùng chuẩn OpenAI-compatible. API key được lưu trong Cline trên máy của bạn; không dán key vào source hoặc commit Git.
      </div>
      <div class="launch-grid">
        <div class="launch-box">
          <div style="font-weight:800;color:#38bdf8;margin-bottom:4px;">1 · Mở Cline</div>
          <div>Mở VS Code → Extensions, cài <b style="color:#fff;">Cline</b> nếu chưa có.</div>
          <div>Mở panel Cline, bấm biểu tượng <b style="color:#fff;">⚙ Settings</b> hoặc menu provider bên dưới khung chat.</div>
        </div>
        <div class="launch-box">
          <div style="font-weight:800;color:#34d399;margin-bottom:4px;">2 · Chọn Provider</div>
          <div><b style="color:#fff;">API Provider:</b> OpenAI Compatible</div>
          <div><b style="color:#fff;">Base URL:</b> <code style="color:#fff;">https://grokapi.vorte.me/v1</code></div>
          <div><b style="color:#fff;">Model ID:</b> <code style="color:#fff;">grok-4.6</code></div>
        </div>
      </div>
      <div style="margin-top:10px;padding:12px 14px;border:1px solid rgba(168,85,247,.3);border-radius:10px;background:rgba(168,85,247,.06);font-size:12px;line-height:1.7;color:var(--muted);">
        <div><b style="color:#c4b5fd;">3 · Nhập API Key:</b> dán key <code style="color:#fff;">sk-...</code> của bạn vào trường API Key trong Cline.</div>
        <div><b style="color:#c4b5fd;">4 · Xác minh:</b> bấm Save/Verify nếu phiên bản Cline có nút này, rồi gửi thử câu <code style="color:#fff;">Xin chào</code>.</div>
        <div><b style="color:#c4b5fd;">5 · Làm việc với project:</b> mở đúng folder bằng <code style="color:#fff;">File → Open Folder</code>, sau đó yêu cầu Cline đọc/sửa file trong workspace.</div>
      </div>
      <div style="margin-top:10px;font-size:11px;line-height:1.65;color:#94a3b8;">
        <b style="color:#fbbf24;">Lỗi thường gặp:</b>
        401 = kiểm tra lại API key · Model not found = nhập đúng <code style="color:#fff;">grok-4.6</code> ·
        Connection error = kiểm tra Base URL có đúng phần <code style="color:#fff;">/v1</code>.
        Nếu tên menu khác, hãy cập nhật Cline và tìm provider <b>OpenAI Compatible</b>.
      </div>
      <div style="margin-top:9px;font-size:11px;">
        <a href="https://docs.cline.bot/provider-config/openai-compatible" target="_blank" rel="noopener noreferrer" style="color:#38bdf8;">Mở tài liệu OpenAI Compatible chính thức của Cline ↗</a>
      </div>
    </div>

    <!-- Additional OpenAI-compatible clients and routers (additive only) -->
    <div class="card" id="more-integrations" style="display:none;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:5px;">
        <div>
          <div style="font-size:15px;font-weight:800;color:#fff;">🔌 DÙNG GROK API Ở NHIỀU NƠI HƠN</div>
          <div style="font-size:11.5px;color:var(--muted);margin-top:4px;">Gateway/router, coding agent và giao diện chat hỗ trợ OpenAI-compatible.</div>
        </div>
        <span class="client-tag">11 app + cấu hình chung</span>
      </div>
      <div style="margin:10px 0 2px;padding:10px 12px;border:1px solid rgba(56,189,248,.25);border-radius:8px;background:rgba(56,189,248,.05);font-size:11px;line-height:1.6;color:var(--muted);">
        <b style="color:#7dd3fc;">Phạm vi đã xác minh:</b> cú pháp config được parser kiểm tra, trường cấu hình đối chiếu tài liệu chính thức, và route Chat Completions/Responses/Models trên gateway live đã qua auth-gate probe. Các chức năng agent/tool cần test tool-call bằng key kiểm thử riêng trước khi gắn nhãn end-to-end.
      </div>

      <div class="client-grid">
        <details class="client-card" open>
          <summary><span>9Router · Upstream Provider</span><span class="client-tag">Router</span></summary>
          <div class="client-card-body">
            <ol>
              <li>Mở 9Router Dashboard → mục <b>Providers</b> (một số bản đặt trong <b>Settings → Providers</b>) → Add Provider.</li>
              <li>Chọn loại <b>OpenAI Compatible</b>, nhập Base URL và API key bên dưới.</li>
              <li>Thêm model ID <code>grok-4.6</code>, sau đó đưa model vào tier/combo mong muốn.</li>
            </ol>
            <div class="code-box"><pre id="cfg-9router">Name: Grok API
Type: openai-compatible
API Protocol: OpenAI Chat Completions
Base URL: https://grokapi.vorte.me/v1
API Key: dán-key-của-bạn
Model ID: grok-4.6</pre><button class="copy-btn" onclick="copyFromElem('cfg-9router', this)">📋 Copy</button></div>
            <div style="margin-top:8px;"><a href="https://github.com/decolua/9router" target="_blank" rel="noopener noreferrer">9Router canonical project ↗</a> · <a href="https://github.com/didin-lab/9router-setup-guide" target="_blank" rel="noopener noreferrer">Hướng dẫn community ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>OpenCode</span><span class="client-tag">Coding agent</span></summary>
          <div class="client-card-body">
            <ol>
              <li>Chạy <code>/connect</code> → <b>Other</b> → đặt provider ID là <code>grok-api</code> và nhập key.</li>
              <li>Thêm cấu hình stable V1 dưới đây vào <code>opencode.json</code>.</li>
              <li>Chạy <code>/models</code> và chọn <code>grok-api/grok-4.6</code>.</li>
            </ol>
            <div class="code-box"><pre id="cfg-opencode">{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "grok-api": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Grok API",
      "options": { "baseURL": "https://grokapi.vorte.me/v1" },
      "models": { "grok-4.6": { "name": "Grok 4.6" } }
    }
  },
  "model": "grok-api/grok-4.6"
}</pre><button class="copy-btn" onclick="copyFromElem('cfg-opencode', this)">📋 Copy</button></div>
            <p><b>OpenCode V2:</b> dùng cấu hình sau thay cho block V1.</p>
            <div class="code-box"><pre id="cfg-opencode-v2">{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "grok-api": {
      "name": "Grok API",
      "env": ["GROK_API_KEY"],
      "package": "@opencode-ai/ai/providers/openai-compatible",
      "settings": { "baseURL": "https://grokapi.vorte.me/v1" },
      "models": { "grok-4.6": { "name": "Grok 4.6" } }
    }
  },
  "model": "grok-api/grok-4.6"
}</pre><button class="copy-btn" onclick="copyFromElem('cfg-opencode-v2', this)">📋 Copy V2</button></div>
            <div style="margin-top:8px;"><a href="https://opencode.ai/docs/providers/" target="_blank" rel="noopener noreferrer">OpenCode stable ↗</a> · <a href="https://opencode.ai/v2/docs/providers" target="_blank" rel="noopener noreferrer">OpenCode V2 ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Roo Code</span><span class="client-tag">VS Code</span></summary>
          <div class="client-card-body">
            <ol><li>Mở Roo Code Settings.</li><li>Chọn đúng provider <b>OpenAI Compatible</b>.</li><li>Nhập key, Base URL và model bên dưới. Agent tools yêu cầu model/gateway hỗ trợ tool calling.</li></ol>
            <div class="code-box"><pre id="cfg-roo">Base URL: https://grokapi.vorte.me/v1
Model: grok-4.6
API Key: dán-key-của-bạn</pre><button class="copy-btn" onclick="copyFromElem('cfg-roo', this)">📋 Copy</button></div>
            <div style="margin-top:8px;"><a href="https://roocodeinc.github.io/Roo-Code/providers/openai/" target="_blank" rel="noopener noreferrer">Tài liệu Roo Code ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Continue</span><span class="client-tag">VS Code / JetBrains</span></summary>
          <div class="client-card-body">
            <ol><li>Mở cấu hình Continue.</li><li>Dùng provider <code>openai</code> và đổi <code>apiBase</code>.</li><li>Chọn model <code>grok-4.6</code>.</li></ol>
            <div class="code-box"><pre id="cfg-continue">name: Grok API
version: 1.0.0
schema: v1
models:
  - name: Grok 4.6
    provider: openai
    model: grok-4.6
    apiBase: https://grokapi.vorte.me/v1
    apiKey: ${{ secrets.GROK_API_KEY }}
    useResponsesApi: false
    capabilities: [tool_use]</pre><button class="copy-btn" onclick="copyFromElem('cfg-continue', this)">📋 Copy</button></div>
            <p>Lưu <code>GROK_API_KEY=...</code> trong <code>&lt;workspace&gt;/.env</code>, <code>&lt;workspace&gt;/.continue/.env</code> hoặc <code>%USERPROFILE%/.continue/.env</code>; gitignore file và restart IDE.</p>
            <div style="margin-top:8px;"><a href="https://docs.continue.dev/reference" target="_blank" rel="noopener noreferrer">Tài liệu Continue ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Open WebUI</span><span class="client-tag">Chat UI</span></summary>
          <div class="client-card-body">
            <ol><li>Vào <b>Settings → Admin → Connections</b>.</li><li>Thêm OpenAI API Connection.</li><li>Nhập URL, key; nếu không tự thấy model thì thêm <code>grok-4.6</code> vào Model IDs Filter.</li></ol>
            <p>Hướng dẫn này xác nhận phần chat. RAG/embeddings cần endpoint embedding riêng và chưa nằm trong phạm vi này.</p>
            <div class="code-box"><pre id="cfg-openwebui">URL: https://grokapi.vorte.me/v1
API Key: dán-key-của-bạn
Model IDs Filter: grok-4.6</pre><button class="copy-btn" onclick="copyFromElem('cfg-openwebui', this)">📋 Copy</button></div>
            <div style="margin-top:8px;"><a href="https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/" target="_blank" rel="noopener noreferrer">Tài liệu Open WebUI ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>LibreChat</span><span class="client-tag">Self-hosted chat</span></summary>
          <div class="client-card-body">
            <ol><li>Thêm <code>GROK_API_KEY=...</code> vào <code>.env</code>.</li><li>Merge block custom endpoint dưới đây vào <code>librechat.yaml</code> hiện có.</li><li>Mount file cấu hình và restart LibreChat.</li></ol>
            <div class="code-box"><pre id="cfg-librechat">endpoints:
  custom:
    - name: "Grok API"
      apiKey: "${GROK_API_KEY}"
      baseURL: "https://grokapi.vorte.me/v1"
      models:
        default: ["grok-4.6"]
        fetch: false
      titleConvo: true
      titleModel: "grok-4.6"</pre><button class="copy-btn" onclick="copyFromElem('cfg-librechat', this)">📋 Copy</button></div>
            <div style="margin-top:8px;"><a href="https://www.librechat.ai/docs/quick_start/custom_endpoints" target="_blank" rel="noopener noreferrer">Tài liệu LibreChat ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Cấu hình chung cho mọi app</span><span class="client-tag">Universal</span></summary>
          <div class="client-card-body">
            <p>Đa số app hỗ trợ <b>OpenAI Chat Completions-compatible</b> có thể dùng bốn giá trị này. Streaming, tools, Responses API và model discovery vẫn phụ thuộc từng app.</p>
            <div class="code-box"><pre id="cfg-universal">Provider: OpenAI Compatible
Base URL: https://grokapi.vorte.me/v1
API Key: dán-key-của-bạn
Model: grok-4.6
Auth: Authorization: Bearer &lt;API_KEY&gt;</pre><button class="copy-btn" onclick="copyFromElem('cfg-universal', this)">📋 Copy</button></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Aider</span><span class="client-tag">Terminal coding</span></summary>
          <div class="client-card-body">
            <p>Đặt hai biến môi trường rồi chạy Aider với prefix model <code>openai/</code>.</p>
            <div class="code-box"><pre id="cfg-aider"># Linux / macOS
export OPENAI_API_BASE="https://grokapi.vorte.me/v1"
export OPENAI_API_KEY="dán-key-của-bạn"
aider --model openai/grok-4.6

# Windows PowerShell
$env:OPENAI_API_BASE="https://grokapi.vorte.me/v1"
$env:OPENAI_API_KEY="dán-key-của-bạn"
aider --model openai/grok-4.6</pre><button class="copy-btn" onclick="copyFromElem('cfg-aider', this)">📋 Copy</button></div>
            <div style="margin-top:8px;"><a href="https://aider.chat/docs/llms/openai-compat.html" target="_blank" rel="noopener noreferrer">Tài liệu Aider ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Zed Editor</span><span class="client-tag">Editor agent</span></summary>
          <div class="client-card-body">
            <p>Vào Agent Settings → Add Provider → OpenAI Compatible, hoặc thêm block dưới đây vào settings.</p>
            <div class="code-box"><pre id="cfg-zed">{
  "language_models": {
    "openai_compatible": {
      "grok-api": {
        "api_url": "https://grokapi.vorte.me/v1",
        "available_models": [{
          "name": "grok-4.6",
          "display_name": "Grok 4.6",
          "max_tokens": 131072,
          "capabilities": {
            "tools": true,
            "chat_completions": true,
            "parallel_tool_calls": false
          }
        }]
      }
    }
  }
}</pre><button class="copy-btn" onclick="copyFromElem('cfg-zed', this)">📋 Copy</button></div>
            <p>Nhập key trong UI của Zed; không đặt key trong <code>settings.json</code>.</p>
            <div><a href="https://zed.dev/docs/ai/use-api-access" target="_blank" rel="noopener noreferrer">Tài liệu Zed ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>OpenAI SDK · Python</span><span class="client-tag">SDK</span></summary>
          <div class="client-card-body">
            <p>Cài thư viện: <code>python -m pip install -U openai</code>. Đặt key trong biến môi trường <code>GROK_API_KEY</code>.</p>
            <div class="code-box"><pre id="cfg-openai-python">import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["GROK_API_KEY"],
    base_url="https://grokapi.vorte.me/v1",
)
response = client.chat.completions.create(
    model="grok-4.6",
    messages=[{"role": "user", "content": "Xin chào"}],
)
print(response.choices[0].message.content)</pre><button class="copy-btn" onclick="copyFromElem('cfg-openai-python', this)">📋 Copy</button></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>LangChain</span><span class="client-tag">Framework</span></summary>
          <div class="client-card-body">
            <p>Cài thư viện: <code>python -m pip install -U langchain-openai</code>. Đặt key trong biến môi trường <code>GROK_API_KEY</code>.</p>
            <div class="code-box"><pre id="cfg-langchain">import os
from langchain_openai import ChatOpenAI

model = ChatOpenAI(
    model="grok-4.6",
    base_url="https://grokapi.vorte.me/v1",
    api_key=os.environ["GROK_API_KEY"],
)
print(model.invoke("Xin chào").content)</pre><button class="copy-btn" onclick="copyFromElem('cfg-langchain', this)">📋 Copy</button></div>
            <p>LangChain ChatOpenAI dùng schema chuẩn; các trường mở rộng riêng như <code>reasoning_content</code> có thể không được giữ lại.</p>
            <div style="margin-top:8px;"><a href="https://docs.langchain.com/oss/python/langchain/models" target="_blank" rel="noopener noreferrer">Tài liệu LangChain ↗</a></div>
          </div>
        </details>

        <details class="client-card" open>
          <summary><span>Flowise / n8n / automation</span><span class="client-tag">Workflow</span></summary>
          <div class="client-card-body">
            <p><b>Flowise:</b> dùng ChatOpenAI Custom, model <code>grok-4.6</code>, key và Base Path. <b>n8n:</b> tạo HTTP Request <code>POST https://grokapi.vorte.me/v1/chat/completions</code>, chọn Header Auth Credential với tên <code>Authorization</code> và giá trị <code>Bearer &lt;API_KEY&gt;</code>, rồi dán JSON body:</p>
            <div class="code-box"><pre id="cfg-automation">{
  "model": "grok-4.6",
  "messages": [
    {"role": "user", "content": "Xin chào"}
  ],
  "stream": false
}</pre><button class="copy-btn" onclick="copyFromElem('cfg-automation', this)">📋 Copy JSON</button></div>
            <p>Agent flow chỉ được coi là đạt sau khi native tool calling chạy thành công; chat text đơn thuần chưa đủ.</p>
            <div style="margin-top:8px;"><a href="https://docs.flowiseai.com/integrations/langchain/chat-models/azure-chatopenai" target="_blank" rel="noopener noreferrer">Tài liệu Flowise custom Base Path ↗</a></div>
          </div>
        </details>
      </div>
      <div style="margin-top:12px;padding:11px 13px;border:1px solid rgba(245,158,11,.28);border-radius:9px;background:rgba(245,158,11,.05);font-size:11px;line-height:1.65;color:var(--muted);">
        <b style="color:#fbbf24;">Bảo mật:</b> dùng secret store hoặc biến môi trường của app; không đặt key thật trong repository, ảnh chụp, file config chia sẻ hay workflow export. Các app này sẽ gửi prompt và phần source được chọn tới gateway đã cấu hình.
      </div>
    </div>

    <!-- Image Generation API Guide -->
    <div class="card" id="image-api-guide" style="display:none;">
      <div style="font-size:15px;font-weight:800;color:#fff;margin-bottom:5px;">🎨 TẠO ẢNH BẰNG GROK API</div>
      <div style="font-size:12px;color:var(--muted);line-height:1.65;">
        Endpoint OpenAI-compatible: <code style="color:#fff;">POST https://grokapi.vorte.me/v1/images/generations</code><br>
        Model nhanh: <code style="color:#38bdf8;">grok-imagine-image</code> ·
        Model chất lượng: <code style="color:#c4b5fd;">grok-imagine-image-quality</code><br>
        Ví dụ dưới đây yêu cầu <code style="color:#fff;">b64_json</code>, sau đó giải mã Base64 thành file ảnh.
      </div>

      <div style="margin-top:14px;font-size:12px;font-weight:800;color:#38bdf8;">1 · Linux / macOS / WSL (curl + jq)</div>
      <div class="code-box" style="align-items:flex-start;overflow:auto;">
        <pre id="image-curl" style="margin:0;white-space:pre;line-height:1.55;color:#e2e8f0;">export GROK_API_KEY='dán-key-tại-máy-của-bạn'

curl -sS https://grokapi.vorte.me/v1/images/generations \\
  -H "Authorization: Bearer $GROK_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "grok-imagine-image-quality",
    "prompt": "A cinematic futuristic Saigon skyline at sunset, detailed, realistic lighting",
    "n": 1,
    "response_format": "b64_json"
  }' &gt; image-response.json

jq -r '.data[0].b64_json' image-response.json | base64 --decode &gt; grok-image.png</pre>
        <button class="copy-btn" onclick="copyFromElem('image-curl', this)">📋 Copy</button>
      </div>

      <div style="margin-top:14px;font-size:12px;font-weight:800;color:#60a5fa;">2 · Windows PowerShell</div>
      <div class="code-box" style="align-items:flex-start;overflow:auto;">
        <pre id="image-powershell" style="margin:0;white-space:pre;line-height:1.55;color:#e2e8f0;">$env:GROK_API_KEY = "dán-key-tại-máy-của-bạn"
$headers = @{ Authorization = "Bearer $env:GROK_API_KEY" }
$body = @{
  model = "grok-imagine-image-quality"
  prompt = "A premium product photo on a dark studio background, cinematic lighting"
  n = 1
  response_format = "b64_json"
} | ConvertTo-Json

$result = Invoke-RestMethod `
  -Uri "https://grokapi.vorte.me/v1/images/generations" `
  -Method Post -Headers $headers -ContentType "application/json" -Body $body

[IO.File]::WriteAllBytes(
  "$PWD\\grok-image.png",
  [Convert]::FromBase64String($result.data[0].b64_json)
)</pre>
        <button class="copy-btn" onclick="copyFromElem('image-powershell', this)">📋 Copy</button>
      </div>

      <div class="launch-grid" style="margin-top:14px;">
        <div class="launch-box" style="overflow:auto;">
          <div style="font-weight:800;color:#fbbf24;margin-bottom:6px;">3 · Python</div>
          <pre id="image-python" style="margin:0;white-space:pre;line-height:1.5;color:#e2e8f0;">import os, base64, requests

r = requests.post(
    "https://grokapi.vorte.me/v1/images/generations",
    headers={"Authorization": f"Bearer {os.environ['GROK_API_KEY']}"},
    json={
        "model": "grok-imagine-image-quality",
        "prompt": "A friendly robot coding in a modern office",
        "n": 1,
        "response_format": "b64_json",
    },
    timeout=180,
)
r.raise_for_status()
with open("grok-image.png", "wb") as f:
    f.write(base64.b64decode(r.json()["data"][0]["b64_json"]))</pre>
          <button class="copy-btn" onclick="copyFromElem('image-python', this)" style="margin-top:8px;">📋 Copy Python</button>
        </div>
        <div class="launch-box" style="overflow:auto;">
          <div style="font-weight:800;color:#34d399;margin-bottom:6px;">4 · Node.js 18+</div>
          <pre id="image-node" style="margin:0;white-space:pre;line-height:1.5;color:#e2e8f0;">import { writeFile } from "node:fs/promises";

const r = await fetch(
  "https://grokapi.vorte.me/v1/images/generations",
  {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.GROK_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "grok-imagine-image-quality",
      prompt: "A colorful isometric developer workspace",
      n: 1,
      response_format: "b64_json",
    }),
  },
);
if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
const data = await r.json();
await writeFile("grok-image.png", Buffer.from(data.data[0].b64_json, "base64"));</pre>
          <button class="copy-btn" onclick="copyFromElem('image-node', this)" style="margin-top:8px;">📋 Copy Node.js</button>
        </div>
      </div>

      <div style="margin-top:14px;padding:12px 14px;border:1px solid rgba(245,158,11,.28);border-radius:10px;background:rgba(245,158,11,.05);font-size:11px;line-height:1.7;color:var(--muted);">
        <div><b style="color:#fbbf24;">Viết prompt tốt:</b> mô tả chủ thể → bối cảnh → phong cách → ánh sáng → góc máy → chi tiết cần tránh.</div>
        <div><b style="color:#fbbf24;">Chọn model:</b> dùng <code style="color:#fff;">grok-imagine-image</code> để thử nhanh; dùng <code style="color:#fff;">grok-imagine-image-quality</code> cho ảnh cuối.</div>
        <div><b style="color:#fbbf24;">Thời gian:</b> tạo ảnh có thể lâu hơn chat; client nên đặt timeout 120–180 giây và không spam retry song song.</div>
        <div><b style="color:#fbbf24;">Tính phí:</b> ảnh được Sub2API ghi nhận theo usage/pricing ảnh, không phải chỉ theo số token chữ.</div>
        <div><b style="color:#fbbf24;">Bảo mật:</b> không đặt API key trong URL, prompt, source code hoặc repository.</div>
      </div>

      <div style="margin-top:10px;font-size:11px;line-height:1.7;color:#94a3b8;">
        <b style="color:#ef4444;">Lỗi:</b>
        401 = key sai/hết hiệu lực · 403 = group/key chưa được cấp quyền tạo ảnh ·
        429 = quá nhanh/quá nhiều ảnh đồng thời, chờ theo Retry-After ·
        5xx/timeout = không gửi hàng loạt request giống nhau; chờ rồi thử lại một lần.
        Nếu response không có <code style="color:#fff;">data[0].b64_json</code>, lưu response JSON để gửi hỗ trợ cùng mã request, tuyệt đối không gửi raw API key.
      </div>
    </div>
  </div>

  <script>
    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function setCustomerDocsVisible(visible) {
      document.getElementById('customer-shortcuts').style.display = visible ? 'flex' : 'none';
      document.getElementById('cline-guide').style.display = visible ? 'block' : 'none';
      document.getElementById('more-integrations').style.display = visible ? 'block' : 'none';
      document.getElementById('image-api-guide').style.display = visible ? 'block' : 'none';
    }

    const params = new URLSearchParams(window.location.search);
    const initialKey = params.get('key') || '';
    if (initialKey) {
      document.getElementById('input-key').value = initialKey;
      checkBalance();
    }

    async function checkBalance() {
      const key = document.getElementById('input-key').value.trim();
      if (!key) return;
      const resBox = document.getElementById('result-box');
      resBox.style.display = 'block';
      setCustomerDocsVisible(false);
      resBox.innerHTML = '<div class="card" style="text-align:center;color:var(--muted);">⏳ Đang tra cứu số dư & lịch sử request...</div>';

      try {
        const r = await fetch('/api/check-key?key=' + encodeURIComponent(key));
        const d = await r.json();
        if (!d.ok) {
          resBox.innerHTML = '<div class="card" style="border-color:#ef4444;background:rgba(239,68,68,0.05);color:#ef4444;font-size:13px;">❌ ' + escapeHtml(d.error) + '</div>';
          return;
        }

        // Full documentation is customer-only: reveal it only after the key
        // has been validated successfully by the balance endpoint.
        setCustomerDocsVisible(true);

        const isExhausted = d.remain_tokens <= 0;
        const pctColor = d.remain_pct > 50 ? '#10b981' : (d.remain_pct > 15 ? '#f59e0b' : '#ef4444');

        const logsHtml = (d.logs && d.logs.length) ? d.logs.map(l => `
          <tr>
            <td style="color:var(--muted);">${escapeHtml(l.time)}</td>
            <td><strong style="color:#38bdf8;">${escapeHtml(l.model)}</strong></td>
            <td><span class="tag-ok">${escapeHtml(l.status)}</span></td>
            <td class="tok-val">${escapeHtml(l.tokens_display)}</td>
            <td class="cost-val">${escapeHtml(l.cost_vnd)} <small style="color:var(--muted); font-size:10px;">(${escapeHtml(l.cost_usd)})</small></td>
            <td class="lat-val">${escapeHtml(l.latency)}</td>
          </tr>
        `).join('') : '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:18px;">Chưa có lượt request nào. Hãy thử chat 1 câu!</td></tr>';

        resBox.innerHTML = `
          <div class="card" id="account-overview">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <div>
                <span class="badge ${isExhausted ? 'badge-exhausted' : 'badge-active'}">${isExhausted ? 'HẾT TOKEN' : 'ACTIVE'}</span>
                <strong style="margin-left:8px; font-size:16px;">${escapeHtml(d.name)}</strong>
              </div>
              <span style="font-size:12px; color:var(--muted); font-family:monospace;">${escapeHtml(d.key_masked)}</span>
            </div>

            <div class="stat-grid">
              <div class="stat-box">
                <div class="stat-label">🔋 Token Còn Lại</div>
                <div class="stat-val" style="color:${pctColor};">${d.remain_tokens.toLocaleString()} <small style="font-size:12px;color:var(--muted);">/ ${d.max_tokens.toLocaleString()}</small></div>
                <div style="font-size:11px;color:var(--muted);margin-top:4px;">Còn lại ${d.remain_pct}% ($${d.quota_usd - d.quota_used_usd > 0 ? (d.quota_usd - d.quota_used_usd).toFixed(4) : '0.0000'})</div>
              </div>

              <div class="stat-box">
                <div class="stat-label">⚡ Đã Tiêu Thụ</div>
                <div class="stat-val">${d.used_tokens.toLocaleString()} <small style="font-size:12px;color:var(--muted);">tokens</small></div>
                <div style="font-size:11px;color:var(--muted);margin-top:4px;">$${d.quota_used_usd.toFixed(4)} USD</div>
              </div>
            </div>

            <div class="battery-bar">
              <div class="battery-fill" style="width: ${d.remain_pct}%; background:${pctColor};"></div>
            </div>

            <!-- Install mode -->
            <div id="install-options" style="margin-top:20px; border-top:1px solid var(--border); padding-top:16px;">
              <div style="font-size:12px; font-weight:700; color:#fff;">🎚️ Chọn chế độ mặc định:</div>
              <div class="mode-row">
                <button id="mode-fast" class="mode-btn" onclick="setInstallMode('fast', '${d.full_key}')">⚡ Fast<br><small>nhanh, tiết kiệm</small></button>
                <button id="mode-smart" class="mode-btn active" onclick="setInstallMode('smart', '${d.full_key}')">🧠 Smart<br><small>cân bằng, đề xuất</small></button>
                <button id="mode-thinking" class="mode-btn" onclick="setInstallMode('thinking', '${d.full_key}')">🔬 Thinking<br><small>suy luận sâu</small></button>
              </div>
              <p id="mode-note" style="font-size:11px; color:var(--muted); margin-top:7px;">Smart: cân bằng tốc độ và chất lượng cho sử dụng hằng ngày.</p>
            </div>

            <!-- Windows 1-Click Command -->
            <div style="margin-top:16px;">
              <div style="font-size:12px; font-weight:700; color:#38bdf8; margin-bottom:6px;">⚡ Lệnh 1-Click Windows (Codex App / ZCode / Grok Build):</div>
              <div class="code-box">
                <span id="cmd-win">irm "https://grokapi.vorte.me/setup-windows?key=${encodeURIComponent(d.full_key)}&mode=smart" | iex</span>
                <button class="copy-btn" onclick="copyFromElem('cmd-win', this)">📋 Copy</button>
              </div>
            </div>

            <!-- Linux/WSL 1-Click Command -->
            <div style="margin-top:14px;">
              <div style="font-size:12px; font-weight:700; color:#10b981; margin-bottom:6px;">🐧 Lệnh 1-Click Cài Đặt Cho Linux / WSL (Terminal):</div>
              <div class="code-box" style="border-color:rgba(16,185,129,0.3); color:#34d399;">
                <span id="cmd-linux">curl -fsSL "https://grokapi.vorte.me/setup-linux?key=${encodeURIComponent(d.full_key)}&mode=smart" | bash</span>
                <button class="copy-btn" onclick="copyFromElem('cmd-linux', this)">📋 Copy</button>
              </div>
            </div>

            <!-- macOS 1-Click Command (additive; legacy Linux command remains unchanged) -->
            <div style="margin-top:14px;">
              <div style="font-size:12px; font-weight:700; color:#c4b5fd; margin-bottom:6px;"> Lệnh 1-Click Cài Đặt Cho macOS (Terminal):</div>
              <div class="code-box" style="border-color:rgba(168,85,247,0.3); color:#c4b5fd;">
                <span id="cmd-mac">curl -fsSL "https://grokapi.vorte.me/setup-mac?key=${encodeURIComponent(d.full_key)}&mode=smart" | bash</span>
                <button class="copy-btn" onclick="copyFromElem('cmd-mac', this)">📋 Copy</button>
              </div>
              <p class="platform-note">Linux, WSL và macOS đều giữ đủ ba mức Fast / Smart / Thinking. Sau khi chạy, mở terminal mới và tạo thread mới.</p>
              <div style="margin-top:14px; padding:13px 15px; border:1px solid rgba(56,189,248,0.22); border-radius:10px; background:rgba(56,189,248,0.05); font-size:12px; line-height:1.65; color:var(--muted);">
                <div style="font-weight:800; color:#fff; margin-bottom:2px;">📖 CÁCH CÀI VÀ MỞ GROK BUILD BẰNG API</div>
                <div>Chỉ làm theo <b style="color:#fff;">một</b> cột đúng với nơi bạn chạy Grok Build:</div>
                <div class="launch-grid">
                  <div class="launch-box">
                    <div style="font-weight:800;color:#38bdf8;">🪟 Windows PowerShell</div>
                    <div>1. Copy và chạy <b>lệnh Windows</b> ở trên.</div>
                    <div>2. Chờ báo hoàn tất, đóng PowerShell và mở PowerShell mới.</div>
                    <div>3. Khởi chạy bằng:</div>
                    <code id="launch-win" class="launch-code">grok -m sub2api-grok --effort medium</code>
                  </div>
                  <div class="launch-box">
                    <div style="font-weight:800;color:#34d399;">🐧 Ubuntu / WSL</div>
                    <div>1. Mở WSL, copy và chạy <b>lệnh Linux</b> ở trên.</div>
                    <div>2. Chờ báo hoàn tất rồi gõ <code style="color:#fff;">exec bash</code>.</div>
                    <div>3. Khởi chạy bằng:</div>
                    <code id="launch-wsl" class="launch-code">grok -m sub2api-grok --effort medium</code>
                  </div>
                  <div class="launch-box">
                    <div style="font-weight:800;color:#c4b5fd;"> macOS</div>
                    <div>1. Mở Terminal và chạy <b>lệnh macOS</b> ở trên.</div>
                    <div>2. Chờ hoàn tất rồi mở Terminal mới.</div>
                    <div>3. Khởi chạy bằng:</div>
                    <code id="launch-mac" class="launch-code">grok -m sub2api-grok --effort medium</code>
                  </div>
                </div>
                <div style="margin-top:7px;"><b style="color:#38bdf8;">Đổi chế độ:</b> Fast = <code style="color:#fff;">low</code>, Smart = <code style="color:#fff;">medium</code>, Thinking = <code style="color:#fff;">high</code>. Có thể gõ <code style="color:#fff;">/effort</code> ngay trong Grok Build để đổi.</div>
                <div style="margin-top:4px;"><b style="color:#fbbf24;">Nếu Grok mở trang đăng nhập:</b> đóng Grok, mở terminal mới và chạy lại đúng lệnh <code style="color:#fff;">grok -m sub2api-grok</code>.</div>
                <div style="margin-top:4px; color:#94a3b8;">Không cần tài khoản Grok/OAuth. Muốn đọc file, hãy nói rõ đường dẫn hoặc tên file.</div>
              </div>
            </div>
          </div>

          <!-- Detailed Request Logs Card -->
          <div class="card" id="usage-history">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
              <div>
                <div style="font-size:15px; font-weight:800; color:#fff;">📋 Logs gần nhất</div>
                <div style="font-size:12px; color:var(--muted);">Chi tiết từng lượt gọi API và số token tiêu hao</div>
              </div>
              <span class="badge badge-active">${(d.logs || []).length} Requests</span>
            </div>

            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Thời gian</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th>Token (In / Out)</th>
                    <th>Cost</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody>
                  ${logsHtml}
                </tbody>
              </table>
            </div>
          </div>
        `;
      } catch (err) {
        resBox.innerHTML = '<div class="card" style="color:#ef4444;">Lỗi kết nối tới máy chủ kiểm tra: ' + escapeHtml(err.message) + '</div>';
      }
    }

    async function copyFromElem(elemId, btn) {
      const el = document.getElementById(elemId);
      if (!el) return;
      const text = el.innerText || el.textContent;
      
      let success = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          success = true;
        }
      } catch (e) {}

      if (!success) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.top = '-9999px';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        try {
          document.execCommand('copy');
          success = true;
        } catch (err) {}
        document.body.removeChild(ta);
      }

      if (btn) {
        const orig = btn.innerText;
        btn.innerText = '✅ Đã chép!';
        btn.style.background = '#10b981';
        btn.style.color = '#fff';
        setTimeout(() => {
          btn.innerText = orig;
          btn.style.background = '';
          btn.style.color = '';
        }, 2000);
      }
    }

    function setInstallMode(mode, key) {
      const notes = {
        fast: 'Fast: phản hồi nhanh, ít reasoning, phù hợp chat và tác vụ đơn giản.',
        smart: 'Smart: cân bằng tốc độ và chất lượng cho sử dụng hằng ngày.',
        thinking: 'Thinking: reasoning cao cho bài khó; sẽ chậm và tốn token hơn.'
      };
      ['fast', 'smart', 'thinking'].forEach(m => {
        const btn = document.getElementById('mode-' + m);
        if (btn) btn.classList.toggle('active', m === mode);
      });
      document.getElementById('mode-note').textContent = notes[mode];
      const encodedKey = encodeURIComponent(key);
      document.getElementById('cmd-win').textContent = `irm "https://grokapi.vorte.me/setup-windows?key=${encodedKey}&mode=${mode}" | iex`;
      document.getElementById('cmd-linux').textContent = `curl -fsSL "https://grokapi.vorte.me/setup-linux?key=${encodedKey}&mode=${mode}" | bash`;
      document.getElementById('cmd-mac').textContent = `curl -fsSL "https://grokapi.vorte.me/setup-mac?key=${encodedKey}&mode=${mode}" | bash`;
      const effort = {fast: 'low', smart: 'medium', thinking: 'high'}[mode];
      document.getElementById('launch-win').textContent = `grok -m sub2api-grok --effort ${effort}`;
      document.getElementById('launch-wsl').textContent = `grok -m sub2api-grok --effort ${effort}`;
      document.getElementById('launch-mac').textContent = `grok -m sub2api-grok --effort ${effort}`;
    }
  </script>
</body>
</html>
"""

def generate_codex_ps_script_legacy(key: str, default_model: str, small_model: str, medium_model: str, large_model: str) -> str:
    base = BASE_URL
    return f"""# Grok API & Codex App 1-Click Auto Setup Script (Windows PowerShell)
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   ⚡ GROK API & CODEX APP 1-CLICK AUTO SETUP (WINDOWS)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan

$apiKey = "{key}"
$baseUrl = "{base}"
$defaultModel = "{default_model}"
$smallModel = "{small_model}"
$mediumModel = "{medium_model}"
$largeModel = "{large_model}"

if (-not $apiKey) {{
    Write-Host "[!] Loi: Thieu API Key trong duong dan." -ForegroundColor Red
    return
}}

Write-Host "[..] Dang thiet lap bien moi truong he thong Windows..." -ForegroundColor Gray
[System.Environment]::SetEnvironmentVariable('GROK_DEPLOYMENT_KEY', $apiKey, 'User')
[System.Environment]::SetEnvironmentVariable('OPENAI_BASE_URL', $baseUrl, 'User')
[System.Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $apiKey, 'User')
[System.Environment]::SetEnvironmentVariable('CODEX_API_KEY', $apiKey, 'User')
[System.Environment]::SetEnvironmentVariable('XAI_BASE_URL', $baseUrl, 'User')
[System.Environment]::SetEnvironmentVariable('XAI_API_KEY', $apiKey, 'User')
$env:GROK_DEPLOYMENT_KEY = $apiKey
$env:OPENAI_BASE_URL = $baseUrl
$env:OPENAI_API_KEY = $apiKey
$env:XAI_BASE_URL = $baseUrl
$env:XAI_API_KEY = $apiKey
Write-Host "[OK] Da thiet lap API Gateway thanh cong!" -ForegroundColor Green
Write-Host "[OK] Da luu API Key vao he thong Windows thanh cong!" -ForegroundColor Green

# Configure Codex Desktop / CLI App (~/.codex)
$codexDir = Join-Path $env:USERPROFILE ".codex"
if (-not (Test-Path $codexDir)) {{
    New-Item -ItemType Directory -Path $codexDir -Force | Out-Null
}}

$configToml = @"
model_provider = "grok"
model = "$defaultModel"
model_small = "$smallModel"
model_medium = "$mediumModel"
model_large = "$largeModel"

[model_providers.grok]
name = "Grok API"
base_url = "$baseUrl"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
"@

Set-Content -Path (Join-Path $codexDir "config.toml") -Value $configToml -Encoding UTF8

$authJson = @"
{{
  "OPENAI_API_KEY": "$apiKey",
  "CODEX_API_KEY": "$apiKey",
  "api_key": "$apiKey",
  "grok": {{
    "api_key": "$apiKey"
  }}
}}
"@

Set-Content -Path (Join-Path $codexDir "auth.json") -Value $authJson -Encoding UTF8
Write-Host "[OK] Da tu dong cau hinh file ~/.codex/config.toml cho Codex Desktop App!" -ForegroundColor Green

# Configure Grok Build CLI (~/.grok)
$grokDir = Join-Path $env:USERPROFILE ".grok"
if (-not (Test-Path $grokDir)) {{
    New-Item -ItemType Directory -Path $grokDir -Force | Out-Null
}}

$grokConfigToml = @"
[models]
default = "grok-4.6"

[model.grok-4.6]
model = "$defaultModel"
base_url = "$baseUrl"
name = "Grok 4.6 Flagship"
env_key = "XAI_API_KEY"

[model.grok-2]
model = "grok-2"
base_url = "$baseUrl"
name = "Grok 2 Flash"
env_key = "XAI_API_KEY"
"@

Set-Content -Path (Join-Path $grokDir "config.toml") -Value $grokConfigToml -Encoding UTF8
Set-Content -Path (Join-Path $grokDir "auth.json") -Value $authJson -Encoding UTF8
Write-Host "[OK] Da tu dong cau hinh ~/.grok/config.toml cho Grok Build CLI!" -ForegroundColor Green

$desktop = [Environment]::GetFolderPath("Desktop")
$batPath = "$desktop\\Chat_Grok.bat"

$chatScript = @"
@echo off
chcp 65001 >nul
title Grok 4.6 AI Terminal
python -c "import urllib.request, json, os, sys; BASE='$baseUrl'; KEY='$apiKey'; MODEL='$defaultModel'; print('=== 🚀 GROK 4.6 AI DA KET NOI (Go quit de thoat, /clear de xoa chat) ===\n');\
while True:\
    try:\
        q = input('👤 Ban: ');\
    except (EOFError, KeyboardInterrupt):\
        print('\nTam biet!'); break;\
    if q.lower() in ['quit', 'exit']: break;\
    if not q.strip(): continue;\
    if q.lower() in ['/clear', '/cls', '/reset', '/new']:\
        os.system('cls');\
        print('=== 🚀 GROK 4.6 AI DA KET NOI (Go quit de thoat, /clear de xoa chat) ===\n'); continue;\
    if q.startswith('/model '):\
        MODEL = q.split(' ', 1)[1].strip();\
        print(f'⚡ Da doi sang model: {{MODEL}}\n'); continue;\
    try:\
        msgs = [{{'role': 'system', 'content': 'You are Grok 4.6, the latest flagship AI developed by xAI. You are extremely intelligent, fast, and helpful.'}}, {{'role': 'user', 'content': q}}];\
        req = urllib.request.Request(f'{{BASE}}/chat/completions', headers={{'Authorization': f'Bearer {{KEY}}', 'Content-Type': 'application/json'}}, data=json.dumps({{'model': MODEL, 'messages': msgs, 'stream': True}}).encode('utf-8'));\
        print(f'🤖 Grok 4.6: ', end='', flush=True);\
        with urllib.request.urlopen(req, timeout=90) as resp:\
            for line in resp:\
                l = line.decode('utf-8').strip();\
                if not l or not l.startswith('data:'): continue;\
                d = l[5:].strip();\
                if d == '[DONE]': break;\
                try:\
                    c = json.loads(d).get('choices',[{{}}])[0].get('delta',{{}}).get('content','');\
                    if c: print(c, end='', flush=True);\
                except: pass;\
        print('\n')\
    except Exception as e: print(f'\nLoi: {{e}}\n')\
"
pause
"@

Set-Content -Path $batPath -Value $chatScript -Encoding UTF8
Write-Host "[OK] Da tao icon 'Chat_Grok.bat' tren Desktop!" -ForegroundColor Green

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "🎉 CAI DAT HOAN TAT 100%! Ban co the mo Codex Desktop App hoac Chatbot ngay!" -ForegroundColor Yellow
"""

def generate_codex_bash_script_legacy(key: str, default_model: str, small_model: str, medium_model: str, large_model: str) -> str:
    base = BASE_URL
    return f"""#!/usr/bin/env bash
# Grok API & Codex 1-Click Auto Setup Script for Linux / macOS
set -e

API_KEY="{key}"
BASE_URL="{base}"
DEFAULT_MODEL="{default_model}"
SMALL_MODEL="{small_model}"
MEDIUM_MODEL="{medium_model}"
LARGE_MODEL="{large_model}"

echo -e "\\033[1;36m============================================================\\033[0m"
echo -e "\\033[1;33m   ⚡ GROK API & CODEX 1-CLICK AUTO SETUP (LINUX / MACOS)\\033[0m"
echo -e "\\033[1;36m============================================================\\033[0m"

if [ -z "$API_KEY" ]; then
    echo -e "\\033[1;31m[!] Loi: Thieu API Key trong duong dan cai dat.\\033[0m"
    exit 1
fi

# 1. Configure Shell RC files
SHELL_FILES=("$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile")
ENV_BLOCK="
# Grok API & Grok Build Environment Variables
export XAI_API_KEY=\"$API_KEY\"
export XAI_API_BASE_URL=\"$BASE_URL\"
export GROK_XAI_API_BASE_URL=\"$BASE_URL\"
export GROK_MODELS_BASE_URL=\"$BASE_URL\"
export GROK_DEPLOYMENT_KEY=\"$API_KEY\"
export OPENAI_BASE_URL=\"$BASE_URL\"
export OPENAI_API_KEY=\"$API_KEY\"
export CODEX_API_KEY=\"$API_KEY\"
"

for rc in "${{SHELL_FILES[@]}}"; do
    if [ -f "$rc" ]; then
        grep -v "OPENAI_BASE_URL" "$rc" | grep -v "OPENAI_API_KEY" | grep -v "CODEX_API_KEY" | grep -v "XAI_BASE_URL" | grep -v "XAI_API_KEY" > "$rc.tmp" 2>/dev/null || true
        mv "$rc.tmp" "$rc" 2>/dev/null || true
        echo "$ENV_BLOCK" >> "$rc"
    elif [ "$rc" = "$HOME/.bashrc" ]; then
        echo "$ENV_BLOCK" >> "$rc"
    fi
done

echo -e "\\033[1;32m[OK] Da thiet lap API Gateway thanh cong!\\033[0m"

# 2. Configure Codex Desktop & CLI (~/.codex)
CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"

cat <<EOF > "$CODEX_DIR/config.toml"
model_provider = "grok"
model = "$DEFAULT_MODEL"
model_small = "$SMALL_MODEL"
model_medium = "$MEDIUM_MODEL"
model_large = "$LARGE_MODEL"

[model_providers.grok]
name = "Grok API"
base_url = "$BASE_URL"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
EOF

cat <<EOF > "$CODEX_DIR/auth.json"
{{
  "OPENAI_API_KEY": "$API_KEY",
  "CODEX_API_KEY": "$API_KEY",
  "api_key": "$API_KEY",
  "grok": {{
    "api_key": "$API_KEY"
  }}
}}
EOF

echo -e "\\033[1;32m[OK] Da tu dong cau hinh ~/.codex/config.toml cho Codex Desktop App!\\033[0m"

# Configure Grok Build CLI (~/.grok)
GROK_DIR="$HOME/.grok"
mkdir -p "$GROK_DIR"

cat <<EOF > "$GROK_DIR/config.toml"
[models]
default = "grok-4.6"

[model.grok-4.6]
model = "$DEFAULT_MODEL"
base_url = "$BASE_URL"
name = "Grok 4.6 Flagship"
env_key = "XAI_API_KEY"

[model.grok-2]
model = "grok-2"
base_url = "$BASE_URL"
name = "Grok 2 Flash"
env_key = "XAI_API_KEY"
EOF

cat <<EOF > "$GROK_DIR/auth.json"
{{
  "OPENAI_API_KEY": "$API_KEY",
  "XAI_API_KEY": "$API_KEY",
  "api_key": "$API_KEY"
}}
EOF
echo -e "\\033[1;32m[OK] Da tu dong cau hinh ~/.grok/config.toml cho Grok Build CLI!\\033[0m"

echo -e "\\033[1;36m============================================================\\033[0m"
echo -e "\\033[1;33m🎉 CAI DAT HOAN TAT 100% TREN WSL / LINUX / MACOS!\\033[0m"
echo -e "\\033[1;37m👉 De ap dung ngay, go lenh: \\033[1;32msource ~/.bashrc\\033[0m"
echo -e "\\033[1;37m👉 Mo Grok Build TUI bang cach go chu: \\033[1;32mgrok\\033[0m"
"""

def generate_codex_ps_script(key: str, mode: str = "smart") -> str:
    """Generate the Windows installer without overwriting existing Codex/Grok settings."""
    mode = normalize_setup_mode(mode)
    settings = SETUP_MODES[mode]
    template = r'''# Grok API one-click setup for Codex App + official Grok Build
$ErrorActionPreference = "Stop"
$apiKey = "__API_KEY__"
$baseUrl = "__BASE_URL__"
$model = "grok-4.6"
$defaultMode = "__MODE__"
$defaultEffort = "__EFFORT__"
$defaultSummary = "__SUMMARY__"
$defaultVerbosity = "__VERBOSITY__"
$idleTimeoutMs = __IDLE_TIMEOUT_MS__
$maxCompletionTokens = __MAX_COMPLETION_TOKENS__

Write-Host "Grok API - Codex App + Grok Build setup" -ForegroundColor Cyan
if ($apiKey -notmatch '^sk-[A-Za-z0-9_+=-]{16,256}$') {
    throw "API key khong hop le."
}

# Verify the key without spending completion tokens.
try {
    $headers = @{ Authorization = "Bearer $apiKey" }
    Invoke-RestMethod -Uri "$baseUrl/models" -Headers $headers -Method Get -TimeoutSec 20 | Out-Null
    Write-Host "[OK] API key va Base URL hop le." -ForegroundColor Green
} catch {
    throw "Khong xac thuc duoc API key: $($_.Exception.Message)"
}

[Environment]::SetEnvironmentVariable("SUB2API_API_KEY", $apiKey, "User")
$env:SUB2API_API_KEY = $apiKey

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Update-CodexConfig([string]$Path) {
    $content = if (Test-Path -LiteralPath $Path) { [IO.File]::ReadAllText($Path) } else { "" }
    $content = [regex]::Replace(
        $content,
        '(?ms)^\s*\[model_providers\.grokapi\]\s*\r?\n.*?(?=^\s*\[|\z)',
        ''
    )

    $section = [regex]::Match($content, '(?m)^\s*\[')
    if ($section.Success) {
        $head = $content.Substring(0, $section.Index)
        $tail = $content.Substring($section.Index)
    } else {
        $head = $content
        $tail = ""
    }

    $managedKeys = '^(model|model_provider|model_reasoning_effort|model_reasoning_summary|model_verbosity|model_context_window|web_search|personality)\s*='
    $keptHead = @($head -split '\r?\n' | Where-Object { $_ -notmatch $managedKeys }) -join "`n"
    $prefix = @"
model = "$model"
model_provider = "grokapi"
model_reasoning_effort = "$defaultEffort"
model_reasoning_summary = "$defaultSummary"
model_verbosity = "$defaultVerbosity"
model_context_window = 131072
web_search = "disabled"
personality = "none"
"@
    $provider = @"

[model_providers.grokapi]
name = "Grok API"
base_url = "$baseUrl"
env_key = "SUB2API_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = $idleTimeoutMs
"@
    $updated = $prefix.Trim() + "`n"
    if ($keptHead.Trim()) { $updated += $keptHead.Trim() + "`n" }
    if ($tail.Trim()) { $updated += "`n" + $tail.Trim() + "`n" }
    $updated += $provider
    Write-Utf8NoBom $Path ($updated.Trim() + "`n")
}

function Update-GrokConfig([string]$Path) {
    $content = if (Test-Path -LiteralPath $Path) { [IO.File]::ReadAllText($Path) } else { "" }
    $content = [regex]::Replace(
        $content,
        '(?ms)^\s*\[model\."sub2api-grok"\]\s*\r?\n.*?(?=^\s*\[|\z)',
        ''
    )

    $modelsPattern = '(?ms)^\s*\[models\]\s*\r?\n.*?(?=^\s*\[|\z)'
    if ([regex]::IsMatch($content, $modelsPattern)) {
        $content = [regex]::Replace($content, $modelsPattern, {
            param($match)
            $body = [regex]::Replace($match.Value, '(?m)^\s*default\s*=.*\r?\n?', '')
            $body = [regex]::Replace($body, '(?m)^\s*default_reasoning_effort\s*=.*\r?\n?', '')
            $body = [regex]::Replace($body, '^\s*\[models\]\s*\r?\n?', '')
            return "[models]`ndefault = `"sub2api-grok`"`ndefault_reasoning_effort = `"$defaultEffort`"`n" + $body.Trim() + "`n`n"
        })
    } else {
        $content = "[models]`ndefault = `"sub2api-grok`"`ndefault_reasoning_effort = `"$defaultEffort`"`n`n" + $content.Trim()
    }

    $customModel = @"

[model."sub2api-grok"]
model = "$model"
base_url = "$baseUrl"
name = "Grok 4.6 via API"
description = "Grok 4.6 through grokapi.vorte.me"
env_key = "SUB2API_API_KEY"
api_backend = "responses"
context_window = 131072
max_completion_tokens = $maxCompletionTokens
supports_reasoning_effort = true
reasoning_effort = "$defaultEffort"
"@
    Write-Utf8NoBom $Path ($content.Trim() + $customModel + "`n")
}

function Update-ZCodeConfig([string]$Path) {
    $config = if (Test-Path -LiteralPath $Path) {
        [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    } else {
        [PSCustomObject]@{ provider = [PSCustomObject]@{} }
    }
    if (-not $config.PSObject.Properties['provider']) {
        $config | Add-Member -NotePropertyName provider -NotePropertyValue ([PSCustomObject]@{})
    }

    $existingName = $null
    foreach ($property in $config.provider.PSObject.Properties) {
        $candidate = $property.Value
        if ($candidate.name -eq 'Grok API' -or $candidate.options.baseURL -eq $baseUrl) {
            $existingName = $property.Name
            break
        }
    }
    if (-not $existingName) { $existingName = 'grokapi' }

    function New-ReasoningPatch([string]$effort) {
        return [ordered]@{
            openai = [ordered]@{
                set = @([ordered]@{ path = @('reasoningEffort'); value = $effort })
            }
        }
    }
    $reasoning = [ordered]@{
        # `variants` drives the visible ZCode selector. `levels` contains the
        # provider patches that make each selection change the real request.
        enabled = $true
        variants = @('low', 'medium', 'high')
        defaultVariant = $defaultEffort
        levels = [ordered]@{
            low = New-ReasoningPatch 'low'
            medium = New-ReasoningPatch 'medium'
            high = New-ReasoningPatch 'high'
        }
        defaultLevel = $defaultEffort
    }
    $modelConfig = [ordered]@{
        reasoning = $reasoning
        # This is a per-response ceiling, not the customer's purchased-token
        # quota. Keep enough room for Grok 4.6 reasoning even in Fast mode.
        limit = [ordered]@{ context = 256000; output = 32768 }
        modalities = [ordered]@{ input = @('text', 'image', 'video'); output = @('text') }
        zcode = [ordered]@{ modalitiesConfigured = $true }
    }
    $providerConfig = [ordered]@{
        name = 'Grok API'
        kind = 'openai'
        options = [ordered]@{ apiKey = $apiKey; baseURL = $baseUrl; apiKeyRequired = $true }
        source = 'custom'
        models = [ordered]@{ 'grok-4.6' = $modelConfig }
    }

    if ($config.provider.PSObject.Properties[$existingName]) {
        $config.provider.PSObject.Properties.Remove($existingName)
    }
    $config.provider | Add-Member -NotePropertyName $existingName -NotePropertyValue $providerConfig
    Write-Utf8NoBom $Path (($config | ConvertTo-Json -Depth 100) + "`n")
}

$codexDir = Join-Path $env:USERPROFILE ".codex"
$grokDir = Join-Path $env:USERPROFILE ".grok"
$zcodeDir = Join-Path $env:USERPROFILE ".zcode\v2"
[IO.Directory]::CreateDirectory($codexDir) | Out-Null
[IO.Directory]::CreateDirectory($grokDir) | Out-Null
[IO.Directory]::CreateDirectory($zcodeDir) | Out-Null
Update-CodexConfig (Join-Path $codexDir "config.toml")
Update-GrokConfig (Join-Path $grokDir "config.toml")
Update-ZCodeConfig (Join-Path $zcodeDir "config.json")

$profiles = @{
    "grok-fast.config.toml" = @"
model = "grok-4.6"
model_provider = "grokapi"
model_reasoning_effort = "low"
model_reasoning_summary = "none"
model_verbosity = "low"
"@
    "grok-smart.config.toml" = @"
model = "grok-4.6"
model_provider = "grokapi"
model_reasoning_effort = "medium"
model_reasoning_summary = "auto"
model_verbosity = "medium"
"@
    "grok-thinking.config.toml" = @"
model = "grok-4.6"
model_provider = "grokapi"
model_reasoning_effort = "high"
model_reasoning_summary = "auto"
model_verbosity = "medium"
"@
}
foreach ($profile in $profiles.GetEnumerator()) {
    Write-Utf8NoBom (Join-Path $codexDir $profile.Key) ($profile.Value.Trim() + "`n")
}

$agentsPath = Join-Path $codexDir "AGENTS.md"
$agents = if (Test-Path -LiteralPath $agentsPath) { [IO.File]::ReadAllText($agentsPath) } else { "" }
$agents = [regex]::Replace(
    $agents,
    '(?ms)^<!-- BEGIN GROKAPI (?:FAST MODE|CHAT RULES) -->.*?^<!-- END GROKAPI (?:FAST MODE|CHAT RULES) -->\s*',
    ''
)
$chatRules = @"
<!-- BEGIN GROKAPI CHAT RULES -->
# Grok API chat and file rules

- Answer greetings and ordinary questions directly without inspecting the workspace or calling tools.
- Use tools only when the user asks for an action, current external information, or a named local file.
- When a file is attached or named, read only that file first; do not scan the repository.
- Do not turn a general question into a code-edit task unless the user asks for code changes.
<!-- END GROKAPI CHAT RULES -->
"@
Write-Utf8NoBom $agentsPath (($agents.Trim() + "`n`n" + $chatRules.Trim()).Trim() + "`n")

Write-Host "[OK] Da cau hinh Codex App -> grok-4.6." -ForegroundColor Green
Write-Host "[OK] Da cau hinh Grok Build -> sub2api-grok." -ForegroundColor Green
Write-Host "[OK] Da cau hinh ZCode -> grok-4.6; Low/Medium/High gui reasoning that." -ForegroundColor Green
Write-Host "[OK] Che do mac dinh: $defaultMode ($defaultEffort reasoning)." -ForegroundColor Green
Write-Host "[OK] Da tao profile: grok-fast, grok-smart, grok-thinking." -ForegroundColor Green

if (-not (Get-Command grok -ErrorAction SilentlyContinue)) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "[..] Dang cai Grok Build chinh thuc tu xAI..." -ForegroundColor Yellow
        npm install -g @xai-official/grok
    } else {
        Write-Warning "Chua co Grok Build va npm. Hay cai Node.js 22+, sau do chay: npm install -g @xai-official/grok"
    }
}

Write-Host "`nHOAN TAT." -ForegroundColor Cyan
Write-Host "- Dong/mo lai Codex App, tao task moi."
Write-Host "- Kiem tra ~/.codex/config.toml: model=grok-4.6, provider=grokapi."
Write-Host "- Codex CLI: codex --profile grok-fast | grok-smart | grok-thinking"
Write-Host "- Mo terminal moi va chay: grok inspect"
Write-Host "- Grok Build: dung /effort trong TUI, hoac grok --effort low|medium|high"
'''
    return (template.replace("__API_KEY__", key)
            .replace("__BASE_URL__", BASE_URL)
            .replace("__MODE__", mode)
            .replace("__EFFORT__", settings["effort"])
            .replace("__SUMMARY__", settings["summary"])
            .replace("__VERBOSITY__", settings["verbosity"])
            .replace("__IDLE_TIMEOUT_MS__", settings["idle_timeout_ms"])
            .replace("__MAX_COMPLETION_TOKENS__", settings["max_completion_tokens"]))


def generate_codex_bash_script(key: str, mode: str = "smart") -> str:
    """Generate the Linux/WSL installer and preserve unrelated TOML/shell settings."""
    mode = normalize_setup_mode(mode)
    settings = SETUP_MODES[mode]
    template = r'''#!/usr/bin/env bash
set -euo pipefail

API_KEY='__API_KEY__'
BASE_URL='__BASE_URL__'
MODEL='grok-4.6'
DEFAULT_MODE='__MODE__'
DEFAULT_EFFORT='__EFFORT__'
DEFAULT_SUMMARY='__SUMMARY__'
DEFAULT_VERBOSITY='__VERBOSITY__'
IDLE_TIMEOUT_MS='__IDLE_TIMEOUT_MS__'
MAX_COMPLETION_TOKENS='__MAX_COMPLETION_TOKENS__'
export SUB2API_API_KEY="$API_KEY"

case "$API_KEY" in
  sk-*) ;;
  *) echo "API key khong hop le." >&2; exit 1 ;;
esac

echo "Grok API - Codex + Grok Build setup"
curl -fsS --max-time 20 -H "Authorization: Bearer $API_KEY" "$BASE_URL/models" >/dev/null
echo "[OK] API key va Base URL hop le."

mkdir -p "$HOME/.config/grokapi" "$HOME/.codex" "$HOME/.grok"
ENV_FILE="$HOME/.config/grokapi/env"
printf 'export SUB2API_API_KEY=%q\n' "$API_KEY" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -e "$rc" ] || touch "$rc"
  sed '/# BEGIN GROKAPI/,/# END GROKAPI/d' "$rc" > "$rc.grokapi.tmp"
  mv "$rc.grokapi.tmp" "$rc"
  printf '\n# BEGIN GROKAPI\n[ -f "$HOME/.config/grokapi/env" ] && . "$HOME/.config/grokapi/env"\n# END GROKAPI\n' >> "$rc"
done

command -v python3 >/dev/null 2>&1 || {
  echo "Can python3 de giu nguyen cau hinh TOML hien co." >&2
  exit 1
}

python3 - "$HOME/.codex/config.toml" "$HOME/.grok/config.toml" "$BASE_URL" "$MODEL" "$DEFAULT_EFFORT" "$DEFAULT_SUMMARY" "$DEFAULT_VERBOSITY" "$IDLE_TIMEOUT_MS" "$MAX_COMPLETION_TOKENS" <<'PY'
from pathlib import Path
import re
import sys

codex_path, grok_path, base_url, model, effort, summary, verbosity, idle_timeout_ms, max_completion_tokens = sys.argv[1:]

def read(path):
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""

def write(path, text):
    Path(path).write_text(text.rstrip() + "\n", encoding="utf-8")

codex = read(codex_path)
codex = re.sub(r'(?ms)^\s*\[model_providers\.grokapi\]\s*\n.*?(?=^\s*\[|\Z)', '', codex)
match = re.search(r'(?m)^\s*\[', codex)
head, tail = (codex[:match.start()], codex[match.start():]) if match else (codex, '')
managed = re.compile(r'^\s*(model|model_provider|model_reasoning_effort|model_reasoning_summary|model_verbosity|model_context_window|web_search|personality)\s*=')
head = '\n'.join(line for line in head.splitlines() if not managed.match(line)).strip()
prefix = f"""model = "{model}"
model_provider = "grokapi"
model_reasoning_effort = "{effort}"
model_reasoning_summary = "{summary}"
model_verbosity = "{verbosity}"
model_context_window = 131072
web_search = "disabled"
personality = "none"
"""
provider = f"""[model_providers.grokapi]
name = "Grok API"
base_url = "{base_url}"
env_key = "SUB2API_API_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = {idle_timeout_ms}"""
write(codex_path, '\n\n'.join(part for part in (prefix, head, tail.strip(), provider) if part))

grok = read(grok_path)
grok = re.sub(r'(?ms)^\s*\[model\."sub2api-grok"\]\s*\n.*?(?=^\s*\[|\Z)', '', grok)
models_re = re.compile(r'(?ms)^\s*\[models\]\s*\n.*?(?=^\s*\[|\Z)')
models_match = models_re.search(grok)
if models_match:
    block = re.sub(r'(?m)^\s*default\s*=.*\n?', '', models_match.group(0))
    block = re.sub(r'(?m)^\s*default_reasoning_effort\s*=.*\n?', '', block)
    block = re.sub(r'^\s*\[models\]\s*\n?', '', block).strip()
    replacement = f'[models]\ndefault = "sub2api-grok"\ndefault_reasoning_effort = "{effort}"\n' + (block + '\n' if block else '') + '\n'
    grok = grok[:models_match.start()] + replacement + grok[models_match.end():]
else:
    grok = f'[models]\ndefault = "sub2api-grok"\ndefault_reasoning_effort = "{effort}"\n\n' + grok
custom = f"""[model."sub2api-grok"]
model = "{model}"
base_url = "{base_url}"
name = "Grok 4.6 via API"
description = "Grok 4.6 through grokapi.vorte.me"
env_key = "SUB2API_API_KEY"
api_backend = "responses"
context_window = 131072
max_completion_tokens = {max_completion_tokens}
supports_reasoning_effort = true
reasoning_effort = "{effort}"
"""
write(grok_path, grok.rstrip() + '\n\n' + custom)

profiles = {
    'grok-fast.config.toml': ('low', 'none', 'low'),
    'grok-smart.config.toml': ('medium', 'auto', 'medium'),
    'grok-thinking.config.toml': ('high', 'auto', 'medium'),
}
for filename, (profile_effort, profile_summary, profile_verbosity) in profiles.items():
    profile_text = '\n'.join((
        'model = "grok-4.6"',
        'model_provider = "grokapi"',
        f'model_reasoning_effort = "{profile_effort}"',
        f'model_reasoning_summary = "{profile_summary}"',
        f'model_verbosity = "{profile_verbosity}"',
    ))
    write(Path(codex_path).parent / filename, profile_text)

agents_path = Path.home() / '.codex' / 'AGENTS.md'
agents = read(agents_path)
agents = re.sub(
    r'(?ms)^<!-- BEGIN GROKAPI (?:FAST MODE|CHAT RULES) -->.*?^<!-- END GROKAPI (?:FAST MODE|CHAT RULES) -->\s*',
    '',
    agents,
)
chat_rules = """<!-- BEGIN GROKAPI CHAT RULES -->
# Grok API chat and file rules

- Answer greetings and ordinary questions directly without inspecting the workspace or calling tools.
- Use tools only when the user asks for an action, current external information, or a named local file.
- When a file is attached or named, read only that file first; do not scan the repository.
- Do not turn a general question into a code-edit task unless the user asks for code changes.
<!-- END GROKAPI CHAT RULES -->"""
write(agents_path, agents.strip() + '\n\n' + chat_rules)
PY

echo "[OK] Da cau hinh Codex + Grok Build: $DEFAULT_MODE ($DEFAULT_EFFORT reasoning)."
echo "[OK] Da tao profile: grok-fast, grok-smart, grok-thinking."

if ! command -v grok >/dev/null 2>&1; then
  echo "[..] Dang cai Grok Build chinh thuc tu xAI..."
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://x.ai/cli/install.sh -o "$installer"
  bash "$installer"
  rm -f "$installer"
  trap - EXIT
fi

echo
echo "HOAN TAT. Mo terminal moi, chay: grok inspect"
echo "Dong/mo lai Codex App va tao thread moi."
echo "Codex CLI: codex --profile grok-fast | grok-smart | grok-thinking"
echo "Grok Build: dung /effort trong TUI, hoac grok --effort low|medium|high"
'''
    return (template.replace("__API_KEY__", key)
            .replace("__BASE_URL__", BASE_URL)
            .replace("__MODE__", mode)
            .replace("__EFFORT__", settings["effort"])
            .replace("__SUMMARY__", settings["summary"])
            .replace("__VERBOSITY__", settings["verbosity"])
            .replace("__IDLE_TIMEOUT_MS__", settings["idle_timeout_ms"])
            .replace("__MAX_COMPLETION_TOKENS__", settings["max_completion_tokens"]))


class PortalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        
        if path in ("/check", "/balance"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if path == "/api/check-key":
            key = qs.get("key", [""])[0]
            data = query_key_info(key)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if path in ("/setup-windows", "/setup-codex-windows", "/api/v1/setup-codex-windows"):
            key = qs.get("key", [""])[0].strip()
            if not is_valid_api_key(key):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Invalid API key")
                return

            mode = normalize_setup_mode(qs.get("mode", ["smart"])[0])
            ps_script = generate_codex_ps_script(key, mode)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-powershell; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(ps_script.encode("utf-8"))
            return

        if path in ("/setup-linux", "/setup-codex-linux", "/api/v1/setup-codex-linux", "/setup-mac"):
            key = qs.get("key", [""])[0].strip()
            if not is_valid_api_key(key):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Invalid API key")
                return

            mode = normalize_setup_mode(qs.get("mode", ["smart"])[0])
            bash_script = generate_codex_bash_script(key, mode)
            self.send_response(200)
            self.send_header("Content-Type", "text/x-shellscript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(bash_script.encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), PortalHandler)
    print(f"Portal running on port {PORT}")
    server.serve_forever()
