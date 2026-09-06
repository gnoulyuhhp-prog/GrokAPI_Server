"""
Nexus Ops — multi-tool automation command center.
Run:  python -m web_console.app
   or CHAY_WEB.bat
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_console import __version__
from web_console.job_manager import JobManager
from web_console.plugins import all_plugins, get_plugin
from web_console.sub2api_sales_client import CapacityUnavailable, Sub2APISalesClient

STATIC = Path(__file__).resolve().parent / "static"
TEMPLATES = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Nexus Ops", version=__version__)
jobs = JobManager(ROOT)

_api_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_api_cache_lock = threading.RLock()
_api_cache_key_locks: dict[tuple[Any, ...], threading.Lock] = {}

# Commercial planning only: 50K/account is not an upstream xAI quota.
SALES_TOKENS_PER_ACTIVE_ACCOUNT = max(
    1_000,
    int(os.getenv("SUB2API_SALES_TOKENS_PER_ACCOUNT") or "50000"),
)
# Current custom Sub2API retail display contract: quota is USD-denominated.
SUB2API_TOKEN_DISPLAY_PER_USD = max(
    1_000,
    int(os.getenv("SUB2API_TOKEN_DISPLAY_PER_USD") or "500000"),
)
SALES_MIN_PACKAGE_TOKENS = 500_000
SALES_MAX_PACKAGE_TOKENS = 150_000_000
SALES_PACKAGE_TOKEN_STEP = 500_000


def ttl_cache(seconds: float):
    """Small in-process cache for slow read-only VPS proxy endpoints."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with _api_cache_lock:
                cached = _api_cache.get(key)
                if cached and now - cached[0] < seconds:
                    return cached[1]
                key_lock = _api_cache_key_locks.setdefault(key, threading.Lock())
            with key_lock:
                with _api_cache_lock:
                    cached = _api_cache.get(key)
                    if cached and time.monotonic() - cached[0] < seconds:
                        return cached[1]
                value = fn(*args, **kwargs)
                with _api_cache_lock:
                    _api_cache[key] = (time.monotonic(), value)
            return value
        return wrapped
    return decorate

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class StartBody(BaseModel):
    tool_id: str = "grok"
    params: dict[str, Any] = Field(default_factory=dict)


class StopBody(BaseModel):
    job_id: Optional[str] = None


class Sub2ConfigBody(BaseModel):
    enabled: bool = True
    mode: str = "auto"
    url: str = ""
    group: str = "grok free"
    name_prefix: str = "grok free"
    user: str = ""
    password: Optional[str] = None
    api_token: Optional[str] = None


class GoogleSheetsConfigBody(BaseModel):
    enabled: bool = False
    spreadsheet_id: str = ""
    webapp_url: Optional[str] = None


class ConfigUpdateBody(BaseModel):
    sub2api: Sub2ConfigBody
    google_sheets: GoogleSheetsConfigBody
    force_guest_on_start: bool = True
    open_grok_after_success: bool = True
    fixed_password: Optional[str] = None


class HotmailImportBody(BaseModel):
    text: str = ""
    mode: str = "append"  # append | replace


class GenerateKeysRequest(BaseModel):
    token_amount: int = 10000
    count: int = 1
    name_prefix: str = "Grok"
    group_name: str = "Grok"
    request_id: str = ""


def get_sub2api_sales_settings() -> tuple[int, int, str]:
    from grokreg.core.config import load_config

    cfg = load_config()
    s2 = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
    base_url = str(s2.get("sub2api_url") or cfg.get("sub2api_url") or "https://grokapi.vorte.me").rstrip("/")
    group_ids = s2.get("group_ids") if isinstance(s2.get("group_ids"), list) else []
    group_id = int(os.getenv("SUB2API_GROK_GROUP_ID") or s2.get("sales_group_id") or (group_ids[0] if group_ids else 34))
    user_id = int(os.getenv("SUB2API_SALES_USER_ID") or s2.get("sales_user_id") or 0)
    return group_id, user_id, base_url


def get_sub2api_sales_client() -> Sub2APISalesClient:
    from grokreg.core.config import load_config

    cfg = load_config()
    s2 = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
    _, _, base_url = get_sub2api_sales_settings()
    admin_api_key = os.getenv("SUB2API_ADMIN_API_KEY") or s2.get("sub2api_api_token") or cfg.get("sub2api_token") or ""
    sales_secret = os.getenv("SUB2API_SALES_SECRET") or s2.get("sales_secret") or ""
    try:
        return Sub2APISalesClient(base_url, str(admin_api_key), str(sales_secret))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Chưa cấu hình Sub2API Sales API cho web console") from exc


def resolve_brand_icon(tool_id: str, explicit: str = "") -> str:
    """Official publisher icon. Drop brands/{id}.svg|png|webp — future tools inherit."""
    if explicit:
        return str(explicit)
    brands = STATIC / "img" / "brands"
    for ext in (".svg", ".png", ".webp"):
        if (brands / f"{tool_id}{ext}").is_file():
            return f"/static/img/brands/{tool_id}{ext}"
    return ""


def _tool_public(p) -> dict[str, Any]:
    m = p.meta
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "icon": m.icon,
        "brand_icon": resolve_brand_icon(m.id, getattr(m, "brand_icon", "") or ""),
        "status": m.status,
        "color": m.color,
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "default": f.default,
                "hint": f.hint,
                "min": f.min,
                "max": f.max,
                "options": [
                    {"value": o.value, "label": o.label, "hint": o.hint}
                    for o in (f.options or [])
                ],
            }
            for f in (m.fields or [])
        ],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    html = TEMPLATES / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>Missing templates/index.html</h1>", status_code=500)
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__, "root": str(ROOT)}


@app.get("/api/tools")
def list_tools():
    return {"tools": [_tool_public(p) for p in all_plugins().values()]}


@app.get("/api/tools/{tool_id}")
def tool_detail(tool_id: str):
    try:
        p = get_plugin(tool_id)
    except KeyError:
        raise HTTPException(404, "tool not found")
    return _tool_public(p)


@app.get("/api/tools/{tool_id}/stats")
def tool_stats(tool_id: str):
    try:
        p = get_plugin(tool_id)
    except KeyError:
        raise HTTPException(404, "tool not found")
    return p.stats(ROOT)


@app.get("/api/tools/{tool_id}/results")
def tool_results(tool_id: str, limit: int = Query(100, ge=1, le=2000)):
    try:
        p = get_plugin(tool_id)
    except KeyError:
        raise HTTPException(404, "tool not found")
    return {"results": p.parse_results(ROOT, limit=limit)}


@app.get("/api/tools/{tool_id}/hotmails")
def tool_hotmails(tool_id: str):
    try:
        p = get_plugin(tool_id)
    except KeyError:
        raise HTTPException(404, "tool not found")
    fn = getattr(p, "hotmail_pool", None)
    if not callable(fn):
        raise HTTPException(404, "tool không hỗ trợ Hotmail pool")
    return fn(ROOT)


@app.post("/api/tools/{tool_id}/hotmails")
def tool_hotmails_import(tool_id: str, body: HotmailImportBody):
    try:
        p = get_plugin(tool_id)
    except KeyError:
        raise HTTPException(404, "tool not found")
    fn = getattr(p, "import_hotmails", None)
    if not callable(fn):
        raise HTTPException(404, "tool không hỗ trợ nhập Hotmail")
    try:
        return fn(ROOT, body.text or "", body.mode or "append")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.list_jobs(), "current": (jobs.current().snapshot() if jobs.current() else None)}


@app.get("/api/jobs/current")
def current_job(tool_id: Optional[str] = None, log_from: int = 0):
    if tool_id:
        j = jobs.get_latest_for_tool(tool_id)
        if j:
            return j.snapshot(log_from=log_from)
        return {"status": "idle", "tool_id": tool_id, "logs": [], "running": False}
    j = jobs.current()
    if not j:
        allj = jobs.list_jobs(1)
        if allj:
            last = jobs.get(allj[0]["id"])
            if last:
                return last.snapshot(log_from=log_from)
        return {"status": "idle", "logs": [], "running": False}
    return j.snapshot(log_from=log_from)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, log_from: int = 0):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j.snapshot(log_from=log_from)


@app.post("/api/jobs/start")
def start_job(body: StartBody):
    try:
        job = jobs.start(body.tool_id, body.params)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "job": job.snapshot()}


@app.post("/api/jobs/stop")
def stop_job(body: StopBody = StopBody()):
    return jobs.stop(body.job_id)


@app.delete("/api/jobs/{job_id}/logs")
def clear_job_logs(job_id: str):
    try:
        job, removed = jobs.clear_logs(job_id)
    except KeyError as e:
        raise HTTPException(404, "job not found") from e
    return {"ok": True, "removed": removed, "job": job.snapshot()}


@app.delete("/api/tools/{tool_id}/logs")
def clear_tool_logs(tool_id: str):
    removed = jobs.clear_tool_logs(tool_id)
    latest = jobs.get_latest_for_tool(tool_id)
    return {"ok": True, "removed": removed, "tool_id": tool_id, "job": (latest.snapshot() if latest else None)}


@app.get("/api/logs/stream")
async def log_stream(tool_id: Optional[str] = None):
    """SSE stream of job logs (optionally filtered by tool_id)."""

    async def gen():
        last_seq = 0
        last_status = ""
        while True:
            j = jobs.get_latest_for_tool(tool_id) if tool_id else jobs.current()
            if j is None:
                if not tool_id:
                    allj = jobs.list_jobs(1)
                    if allj:
                        j = jobs.get(allj[0]["id"])
            if j:
                cur_seq = j._log_seq
                cur_status = j.status
                if cur_seq != last_seq or cur_status != last_status:
                    last_seq = cur_seq
                    last_status = cur_status
                    payload = j.snapshot()
                    yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.8)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/config/summary")
def get_config_summary():
    from grokreg.core.config import load_config

    cfg = load_config()
    s2 = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
    gs = cfg.get("google_sheets") if isinstance(cfg.get("google_sheets"), dict) else {}
    has_pass = bool(s2.get("sub2api_pass") or cfg.get("sub2api_password"))
    has_tok = bool(s2.get("sub2api_api_token") or cfg.get("sub2api_token"))
    has_web = bool(gs.get("webapp_url") or cfg.get("google_sheets_webapp_url"))
    fixed_p = bool(cfg.get("fixed_password"))
    return {
        "sub2api": {
            "enabled": s2.get("enabled", cfg.get("enable_sub2api", True)),
            "mode": s2.get("mode", cfg.get("sub2api_mode", "auto")),
            "url": s2.get("sub2api_url", cfg.get("sub2api_url", "https://grokapi.vorte.me")),
            "group": s2.get("group", cfg.get("sub2api_group", "Grok")),
            "name_prefix": s2.get("name_prefix", cfg.get("sub2api_name_prefix", "grok free")),
            "user": s2.get("sub2api_user", cfg.get("sub2api_user", "")),
            "has_password": has_pass,
            "password_set": has_pass,
            "has_token": has_tok,
            "api_token_set": has_tok,
        },
        "google_sheets": {
            "enabled": gs.get("enabled", cfg.get("enable_google_sheets", False)),
            "spreadsheet_id": gs.get("spreadsheet_id", cfg.get("google_sheets_spreadsheet_id", "")),
            "webapp_url": gs.get("webapp_url", cfg.get("google_sheets_webapp_url", "")),
            "has_webapp": has_web,
            "webapp_set": has_web,
        },
        "force_guest_on_start": cfg.get("force_guest_on_start", True),
        "open_grok_after_success": cfg.get("open_grok_after_success", True),
        "fixed_password": cfg.get("fixed_password"),
        "fixed_password_set": fixed_p,
    }


@app.put("/api/config")
def update_config_api(body: ConfigUpdateBody):
    from grokreg.core.config import load_config, save_config

    cfg = load_config()
    s2 = cfg.setdefault("sub2api", {})
    s2["enabled"] = body.sub2api.enabled
    s2["mode"] = body.sub2api.mode
    s2["sub2api_url"] = body.sub2api.url
    s2["group"] = body.sub2api.group
    s2["name_prefix"] = body.sub2api.name_prefix
    s2["sub2api_user"] = body.sub2api.user
    if body.sub2api.password:
        s2["sub2api_pass"] = body.sub2api.password
    if body.sub2api.api_token:
        s2["sub2api_api_token"] = body.sub2api.api_token

    gs = cfg.setdefault("google_sheets", {})
    gs["enabled"] = body.google_sheets.enabled
    gs["spreadsheet_id"] = body.google_sheets.spreadsheet_id
    if body.google_sheets.webapp_url:
        gs["webapp_url"] = body.google_sheets.webapp_url

    cfg["enable_sub2api"] = body.sub2api.enabled
    cfg["enable_google_sheets"] = body.google_sheets.enabled
    cfg["force_guest_on_start"] = body.force_guest_on_start
    cfg["open_grok_after_success"] = body.open_grok_after_success
    if body.fixed_password:
        cfg["fixed_password"] = body.fixed_password

    save_config(cfg)
    return {"ok": True, "message": "Đã lưu thiết lập thành công"}


@app.post("/api/sub2api/keys/generate")
def generate_sub2api_keys(req: GenerateKeysRequest):
    if (
        req.token_amount < SALES_MIN_PACKAGE_TOKENS
        or req.token_amount > SALES_MAX_PACKAGE_TOKENS
        or req.token_amount % SALES_PACKAGE_TOKEN_STEP != 0
        or req.count < 1
        or req.count > 100
    ):
        raise HTTPException(
            status_code=400,
            detail="Mỗi key phải từ 500K đến 150M token, theo bước 500K; số key từ 1 đến 100",
        )
    
    group_id, user_id, base_url = get_sub2api_sales_settings()
    if user_id <= 0:
        raise HTTPException(status_code=503, detail="Chưa cấu hình Sub2API Sales user")
    sales = get_sub2api_sales_client()
    count = req.count
    token_display = f"{req.token_amount // 1_000_000}m" if req.token_amount % 1_000_000 == 0 else f"{req.token_amount // 1000}k"
    quota_usd = round(req.token_amount / float(SUB2API_TOKEN_DISPLAY_PER_USD), 8)
    reference = f"nexus-retail-{uuid.uuid4().hex}"
    requested_total = req.token_amount * count

    try:
        reservation = sales.reserve(
            reference,
            "batch_new_key",
            group_id,
            requested_total,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    except CapacityUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INSUFFICIENT_GROK_CAPACITY",
                "requested_tokens": requested_total,
                "available_tokens": exc.available_tokens,
                "suggested_tokens": exc.suggested_tokens,
                "minimum_tokens": exc.minimum_tokens,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Không thể giữ chỗ capacity trên Sub2API") from exc

    items = []
    for index in range(1, count + 1):
        suffix = f"_{index:02d}" if count > 1 else ""
        key_name = f"{req.name_prefix}_{token_display}{suffix}".strip()
        items.append({
            "user_id": user_id,
            "name": key_name,
            "requested_tokens": req.token_amount,
        })

    try:
        fulfilled = sales.fulfill_batch(
            int(reservation["id"]),
            group_id,
            items,
            idempotency_key=reference,
        )
    except Exception as exc:
        # Do not release here: a retry with the same idempotency key must be able
        # to observe a completed upstream transaction instead of issuing twice.
        raise HTTPException(status_code=502, detail="Sub2API fulfill thất bại; chưa trả key một phần") from exc

    created_keys = []
    for key_data in fulfilled.get("api_keys") or []:
        created_keys.append({
            **key_data,
            "tokens": req.token_amount,
            "tokens_display": token_display,
            "quota_usd": quota_usd,
            "group": req.group_name or "Grok",
        })

    if len(created_keys) != count:
        raise HTTPException(status_code=502, detail="Sub2API trả về batch không đầy đủ")

    with _api_cache_lock:
        _api_cache.clear()

    return {
        "ok": True,
        "base_url": f"{base_url}/v1",
        "keys": created_keys,
        "errors": [],
        "count": len(created_keys),
        "token_amount": req.token_amount,
    }


@app.get("/api/sub2api/pool/stats")
@ttl_cache(8)
def get_sub2api_pool_stats():
    try:
        group_id, _, base_url = get_sub2api_sales_settings()
        capacity = get_sub2api_sales_client().availability(group_id)
        active_accounts = max(0, int(capacity.get("active_accounts") or 0))
        token_per_acc = max(0, int(capacity.get("tokens_per_active_account") or 0))
        total_max_tokens = max(0, int(capacity.get("capacity_tokens") or 0))
        remaining_tokens = max(0, int(capacity.get("available_tokens") or 0))
        suggested_tokens = max(0, int(capacity.get("suggested_tokens") or 0))
        outstanding_tokens = max(0, int(capacity.get("outstanding_tokens") or 0))
        reserved_tokens = max(0, int(capacity.get("reserved_tokens") or 0))
        used_tokens = max(0, total_max_tokens - remaining_tokens)
        remaining_percent = round((remaining_tokens / total_max_tokens) * 100, 2) if total_max_tokens else 0.0
        return {
            "connected": True,
            "base_url": base_url,
            "total_accounts": active_accounts,
            "active_accounts": active_accounts,
            "token_per_acc": token_per_acc,
            "total_max_tokens": total_max_tokens,
            "remaining_tokens": remaining_tokens,
            "suggested_tokens": suggested_tokens,
            "minimum_tokens": max(0, int(capacity.get("minimum_tokens") or 0)),
            "outstanding_tokens": outstanding_tokens,
            "reserved_tokens": reserved_tokens,
            "used_tokens": used_tokens,
            "remaining_percent": remaining_percent,
            "safe_keys": {
                "10k": remaining_tokens // 10000,
                "50k": remaining_tokens // 50000,
                "65k": remaining_tokens // 65000,
                "100k": remaining_tokens // 100000,
                "500k": remaining_tokens // 500000,
                "1m": remaining_tokens // 1000000,
            },
        }
    except Exception as exc:
        return {
            "connected": False,
            "error": "authoritative capacity unavailable",
            "total_accounts": 0,
            "active_accounts": 0,
            "token_per_acc": 0,
            "total_max_tokens": 0,
            "remaining_tokens": 0,
            "suggested_tokens": 0,
            "used_tokens": 0,
            "remaining_percent": 0.0,
            "safe_keys": {"10k": 0, "50k": 0, "65k": 0, "100k": 0, "500k": 0, "1m": 0},
        }


@app.get("/api/sub2api/keys/list")
@ttl_cache(10)
def list_sub2api_keys(page: int = 1, page_size: int = 50):
    import requests
    from grokreg.core.config import load_config

    cfg = load_config()
    s2 = cfg.get("sub2api") if isinstance(cfg.get("sub2api"), dict) else {}
    base_url = (s2.get("sub2api_url") or cfg.get("sub2api_url") or "https://grokapi.vorte.me").rstrip("/")
    user = s2.get("sub2api_user") or cfg.get("sub2api_user")
    pwd = s2.get("sub2api_pass") or cfg.get("sub2api_password")

    if not user or not pwd:
        return {"items": [], "total": 0}

    try:
        r = requests.post(f"{base_url}/api/v1/auth/login", json={"email": user, "password": pwd}, timeout=10)
        auth_token = r.json().get("data", {}).get("access_token")
        if not auth_token:
            return {"items": [], "total": 0}
        headers = {"Authorization": f"Bearer {auth_token}"}
        kr = requests.get(f"{base_url}/api/v1/keys?page={page}&page_size={page_size}", headers=headers, timeout=10)
        data = kr.json().get("data", {})

        items = data.get("items", [])

        def enrich(item):
            key_str = item.get("key")
            if key_str:
                try:
                    cr = requests.get(f"{base_url}/api/check-key?key={key_str}", timeout=2)
                    cd = cr.json()
                    if cd.get("ok"):
                        item["actual_used_tokens"] = cd.get("used_tokens")
                        item["actual_remain_tokens"] = cd.get("remain_tokens")
                        item["actual_remain_pct"] = cd.get("remain_pct")
                except Exception:
                    pass
            return item

        # Checking every key sequentially made one page take N × 2 seconds.
        # A modest pool keeps the endpoint responsive without flooding the VPS.
        if items:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(items))) as executor:
                list(executor.map(enrich, items))
        return data
    except Exception:
        return {"items": [], "total": 0}


@app.get("/setup-windows", response_class=PlainTextResponse)
@app.get("/api/v1/setup-windows", response_class=PlainTextResponse)
def get_setup_windows_script(key: str = "", model: str = "grok-4.6", base: str = "https://grokapi.vorte.me/v1"):
    ps_script = f"""# Grok API 1-Click Auto Setup Script
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "   ⚡ GROK API 1-CLICK AUTO-SETUP (NEXUS AI SYSTEM)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Cyan

$apiKey = "{key}"
$baseUrl = "{base}"
$defaultModel = "{model}"

if (-not $apiKey) {{
    Write-Host "[!] Loi: Thieu API Key trong duong dan." -ForegroundColor Red
    return
}}

Write-Host "[..] Dang thiet lap bien moi truong he thong..." -ForegroundColor Gray
[System.Environment]::SetEnvironmentVariable('OPENAI_BASE_URL', $baseUrl, 'User')
[System.Environment]::SetEnvironmentVariable('OPENAI_API_KEY', $apiKey, 'User')
[System.Environment]::SetEnvironmentVariable('XAI_BASE_URL', $baseUrl, 'User')
[System.Environment]::SetEnvironmentVariable('XAI_API_KEY', $apiKey, 'User')
$env:OPENAI_BASE_URL = $baseUrl
$env:OPENAI_API_KEY = $apiKey

Write-Host "[OK] Da luu Base URL: $baseUrl" -ForegroundColor Green
Write-Host "[OK] Da luu API Key vao may cua ban thanh cong!" -ForegroundColor Green

$desktop = [Environment]::GetFolderPath("Desktop")
$batPath = "$desktop\\Chat_Grok.bat"

$chatScript = @"
@echo off
chcp 65001 >nul
title Grok 4.6 AI Terminal
python -c "import urllib.request, json, os; print('=== 🚀 GROK 4.6 AI DA KET NOI (Go quit de thoat) ===\\n'); while True: q = input('👤 Ban: '); if q.lower() in ['quit', 'exit']: break; if not q.strip(): continue; req = urllib.request.Request('$baseUrl/chat/completions', headers={{'Authorization': 'Bearer $apiKey', 'Content-Type': 'application/json'}}, data=json.dumps({{'model':'$defaultModel','messages':[{{'role':'user','content':q}}]}}).encode('utf-8')); print('🤖 Grok 4.6: ', json.loads(urllib.request.urlopen(req).read().decode('utf-8'))['choices'][0]['message']['content'], '\\n')"
pause
"@

Set-Content -Path $batPath -Value $chatScript -Encoding UTF8
Write-Host "[OK] Da tao icon 'Chat_Grok.bat' tren man hinh Desktop!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "🎉 CAI DAT HOAN TAT 100%! Ban co the mo icon tren Desktop hoac dung trong VS Code/Cursor ngay!" -ForegroundColor Yellow
"""
    return PlainTextResponse(ps_script, media_type="text/plain; charset=utf-8")


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Nexus Ops Web Console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"=== Nexus Ops Console v{__version__} ===")
    print(f"Server: {url}")
    print(f"Root:   {ROOT}")

    if not args.no_browser:

        def _open():
            import time

            time.sleep(1.0)
            webbrowser.open(url)

        import threading

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
