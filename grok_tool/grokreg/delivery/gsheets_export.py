#!/usr/bin/env python3
"""
Đẩy báo cáo overnight lên Google Sheet (layout multi-tab dễ đọc).

Auth (chọn 1):
  A) Apps Script Web App — config.google_sheets.webapp_url
  B) Service account JSON — gsheets_service_account.json + share Editor

One-shot (không cần webapp): chạy gsheets_push_once.gs trong Apps Script → writeGrokReport
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config.json"
VPN_META = ROOT / "data" / "vpn_by_email.json"
# Defaults empty — real IDs/secrets live only in local config.json (gitignored)
DEFAULT_SHEET_ID = ""
DEFAULT_GID = 0


def load_gs_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    gs = dict(cfg.get("google_sheets") or {})
    gs.setdefault("enabled", True)
    gs.setdefault("spreadsheet_id", DEFAULT_SHEET_ID)
    gs.setdefault("gid", DEFAULT_GID)
    gs.setdefault("webapp_url", "")
    gs.setdefault("webapp_secret", "")
    gs.setdefault("credentials_file", "gsheets_service_account.json")
    gs.setdefault("mode", "replace_night")
    gs.setdefault("write_txt_backup", True)
    gs.setdefault("detect_vpn_country", True)
    return gs


def _common_password(cfg: dict[str, Any] | None = None) -> str:
    """Password label for sheet summary — from local config only."""
    try:
        raw = cfg
        if raw is None and CONFIG.exists():
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return str(raw.get("fixed_password") or "")
    except Exception:
        pass
    return ""


def sub2api_name(status: str) -> str:
    if "added_sub2api" in (status or "") and ":" in status:
        return status.split(":", 1)[-1].strip()
    return ""


def detect_exit_ip_country(timeout: float = 8.0) -> dict[str, str]:
    """
    Detect public IP + country (goes through system VPN if enabled).
    Returns {ip, country, country_code, label} — label e.g. "Japan (JP) · 1.2.3.4"
    """
    out = {"ip": "", "country": "", "country_code": "", "label": "— (no VPN / unknown)"}
    endpoints = [
        # ip-api.com free, no key
        (
            "http://ip-api.com/json/?fields=status,country,countryCode,query",
            lambda j: (
                j.get("query") or "",
                j.get("country") or "",
                j.get("countryCode") or "",
            )
            if j.get("status") == "success"
            else ("", "", ""),
        ),
        (
            "https://ipapi.co/json/",
            lambda j: (
                j.get("ip") or "",
                j.get("country_name") or "",
                j.get("country_code") or "",
            ),
        ),
        (
            "https://ifconfig.co/json",
            lambda j: (
                j.get("ip") or "",
                j.get("country") or "",
                j.get("country_iso") or j.get("country_code") or "",
            ),
        ),
    ]
    for url, parse in endpoints:
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code >= 400:
                continue
            j = r.json()
            ip, country, code = parse(j)
            if not ip and not country:
                continue
            out["ip"] = str(ip).strip()
            out["country"] = str(country).strip()
            out["country_code"] = str(code).strip().upper()
            if out["country"] and out["country_code"]:
                out["label"] = f"{out['country']} ({out['country_code']}) · {out['ip']}"
            elif out["country"]:
                out["label"] = f"{out['country']} · {out['ip']}"
            elif out["ip"]:
                out["label"] = f"IP {out['ip']}"
            else:
                out["label"] = "—"
            return out
        except Exception:
            continue
    return out


def _load_vpn_meta() -> dict[str, str]:
    if not VPN_META.exists():
        return {}
    try:
        raw = json.loads(VPN_META.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k).lower(): str(v) for k, v in raw.items() if v}
    except Exception:
        pass
    return {}


def _save_vpn_meta(meta: dict[str, str]) -> None:
    try:
        VPN_META.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        pass


def _all_full_from_accounts() -> list[dict]:
    """
    Mọi acc thành công trong accounts.txt (sổ tay chung, không chỉ ca đêm).
    - full_ok  = added_sub2api:...
    - reg_ok   = success / success_sub2api...
    Dedup by email (last line wins) so re-runs don't flood the sheet.
    """
    try:
        import grokreg.tools.export_morning_report as emr

        rows = emr.parse_accounts()
        # last status per email
        by_email: dict[str, dict] = {}
        for r in rows:
            em = str(r.get("email") or "").strip().lower()
            if not em:
                continue
            by_email[em] = r
        out: list[dict] = []
        for r in by_email.values():
            tag = emr.classify(r.get("status", ""))
            # Lấy toàn bộ acc thành công (đã vào Sub2API hoặc reg thành công)
            if tag in ("full_ok", "reg_ok"):
                out.append(r)
        # stable-ish order: keep accounts.txt appearance order of last write
        order = []
        seen = set()
        for r in reversed(rows):
            em = str(r.get("email") or "").strip().lower()
            if (
                em in by_email
                and em not in seen
                and emr.classify(by_email[em].get("status", "")) in ("full_ok", "reg_ok")
            ):
                order.append(by_email[em])
                seen.add(em)
        order.reverse()
        return order if order else out
    except Exception:
        return []


def build_payload(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Sổ tay acc FULL thành công (tab grok only).

    accounts: [# Tag Email Pass Sub2API Status Date Exported VPN]
    summary: includes vpn_country / vpn_label (IP exit lúc export)
    """
    data = data or {}
    ref: datetime = data.get("ref") or datetime.now()
    start = data.get("start")
    end = data.get("end")
    gs = load_gs_config()

    # Detect VPN/country via public IP (system VPN applies to this request)
    vpn = {"ip": "", "country": "", "country_code": "", "label": "—"}
    if gs.get("detect_vpn_country", True):
        vpn = detect_exit_ip_country()

    # Master list = all FULL in accounts.txt
    full_all = _all_full_from_accounts()
    if not full_all and data.get("full_ok"):
        full_all = list(data["full_ok"])

    # Session emails (ca này / batch) → gán VPN hiện tại; acc cũ giữ meta cũ
    session_emails = {
        str(r.get("email", "")).lower()
        for r in (data.get("full_ok") or [])
        if r.get("email")
    }
    vpn_meta = _load_vpn_meta()
    if vpn.get("label") and vpn["label"] not in ("—", "— (no VPN / unknown)"):
        for em in session_emails:
            if em:
                vpn_meta[em] = vpn["label"]
        # Nếu export full ledger không có session → gán VPN hiện tại cho acc chưa có meta
        if not session_emails:
            for r in full_all:
                em = str(r.get("email", "")).lower()
                if em and em not in vpn_meta:
                    vpn_meta[em] = vpn["label"]
        _save_vpn_meta(vpn_meta)

    accounts_rows: list[list[Any]] = []
    for n, r in enumerate(full_all, 1):
        em = str(r.get("email", "")).lower()
        # Ưu tiên meta đã lưu; acc session mới → VPN hiện tại
        vpn_cell = vpn_meta.get(em) or (
            vpn.get("label") if em in session_emails else ""
        ) or (vpn.get("label") if not session_emails else "")
        when = str(r.get("ts") or "").strip()
        st = r.get("status", "")
        s_name = sub2api_name(st)
        if not s_name:
            if st == "success":
                s_name = "reg only"
            elif st.startswith("success_sub2api"):
                s_name = "sub2api pending"
            else:
                s_name = "—"
        accounts_rows.append(
            [
                n,
                "FULL",
                r.get("email", ""),
                r.get("password", ""),
                s_name,
                st,
                when[:10] if when else "",
                when or "—",
                vpn_cell or "—",
            ]
        )

    batch_label = ""
    if start and end:
        try:
            batch_label = (
                f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}"
            )
        except Exception:
            batch_label = str(start)

    summary = {
        "exported_at": ref.strftime("%Y-%m-%d %H:%M:%S"),
        "password_common": _common_password(),
        "alltime_full": len(full_all),
        "alltime_total": data.get("alltime_total", 0),
        "alltime_ok": data.get("alltime_ok", 0),
        "alltime_fail": data.get("alltime_fail", 0),
        "batch_label": batch_label,
        "acc_full": data.get("full_n") if batch_label else "",
        "acc_fail": data.get("fail_n") if batch_label else "",
        "acc_ok": data.get("ok_n") if batch_label else "",
        "total_unique": data.get("total", 0),
        "ok_rate": round(float(data.get("rate", 0.0) or 0.0), 1),
        "starts": data.get("starts", 0),
        "ends": data.get("ends_n", 0),
        "night_start": batch_label.split(" → ")[0] if batch_label else "",
        "night_end": batch_label.split(" → ")[-1] if batch_label else "",
        # VPN / exit IP
        "vpn_ip": vpn.get("ip") or "",
        "vpn_country": vpn.get("country") or "",
        "vpn_country_code": vpn.get("country_code") or "",
        "vpn_label": vpn.get("label") or "—",
    }

    return {
        "secret": "",
        "spreadsheet_id": "",
        "gid": 0,
        "mode": "success_ledger",
        "summary": summary,
        "headers": [
            "#",
            "Tag",
            "Email",
            "Password",
            "Sub2API Name",
            "Status",
            "Date",
            "Exported At",
            "VPN",
        ],
        "accounts": accounts_rows,
        "fail_headers": [],
        "fails": [],
    }


def push_via_webapp(gs: dict[str, Any], payload: dict[str, Any]) -> str:
    url = (gs.get("webapp_url") or "").strip()
    if not url:
        raise RuntimeError("webapp_url trống")
    payload = dict(payload)
    payload["secret"] = gs.get("webapp_secret") or ""
    payload["spreadsheet_id"] = gs.get("spreadsheet_id") or DEFAULT_SHEET_ID
    payload["gid"] = int(gs.get("gid") or DEFAULT_GID)
    payload["mode"] = gs.get("mode") or "replace_night"
    payload["tab"] = str(payload.get("tab") or gs.get("tab") or "grok").strip() or "grok"

    # follow redirects (Apps Script often 302)
    r = requests.post(url, json=payload, timeout=90, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"webapp HTTP {r.status_code}: {r.text[:300]}")
    text = (r.text or "").strip()
    # Must be JSON {ok: true, ...} — HTML/login pages are failures
    if text.startswith("<!") or "<html" in text[:80].lower():
        raise RuntimeError(
            f"webapp returned HTML (not JSON) — deploy/auth issue: {text[:160]}"
        )
    try:
        j = r.json()
    except ValueError as e:
        raise RuntimeError(f"webapp non-JSON response: {text[:200]}") from e
    if not isinstance(j, dict):
        raise RuntimeError(f"webapp unexpected JSON: {j!r}")
    if j.get("ok") is False:
        raise RuntimeError(j.get("error") or str(j))
    if j.get("ok") is not True:
        raise RuntimeError(f"webapp missing ok:true → {j}")
    return f"webapp ok: {j}"


def _open_sheet_gspread(gs: dict[str, Any]):
    import gspread
    from google.oauth2.service_account import Credentials

    cred_path = Path(gs.get("credentials_file") or "gsheets_service_account.json")
    if not cred_path.is_absolute():
        cred_path = ROOT / cred_path
    if not cred_path.exists():
        raise RuntimeError(f"Thiếu credentials: {cred_path}")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(str(cred_path), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(gs.get("spreadsheet_id") or DEFAULT_SHEET_ID)
    return sh


def push_via_service_account(gs: dict[str, Any], payload: dict[str, Any]) -> str:
    """Mirror Apps Script multi-tab layout via gspread."""
    sh = _open_sheet_gspread(gs)
    summary = payload["summary"]
    accounts = payload["accounts"]
    fails = payload["fails"]

    full = []
    for a in accounts:
        # # Tag Email Pass Sub2 Status Night Exported
        full.append([a[0], a[2], a[3], a[4] or "", a[1], a[6] if len(a) > 6 else a[5]])

    fail_rows = []
    for f in fails:
        fail_rows.append([f[0], f[2] if len(f) > 2 else f[1], f[1], f[3] if len(f) > 3 else ""])

    night_date = str(summary.get("night_start", ""))[:10]
    night_label = f"{summary.get('night_start', '')} → {summary.get('night_end', '')}"

    def get_or_create(name: str):
        try:
            return sh.worksheet(name)
        except Exception:
            return sh.add_worksheet(title=name, rows=2000, cols=12)

    # Prefer existing gid sheet as Tong quan
    dash = None
    gid = int(gs.get("gid") or DEFAULT_GID)
    for w in sh.worksheets():
        if w.id == gid:
            dash = w
            break
    if dash is None:
        dash = get_or_create("Tong quan")
    try:
        dash.update_title("Tong quan")
    except Exception:
        pass

    dash.clear()
    grid = [
        [f"GROK REG — BÁO CÁO CA ĐÊM  ·  {night_date}", "", "", "", "", ""],
        ["Ca dem", night_label, "", "", "Xuat luc", summary.get("exported_at", "")],
        [],
        ["FULL Sub2API", "FAIL", "TONG unique", "TY LE OK %", "START run", "END run"],
        [
            summary.get("acc_full", 0),
            summary.get("acc_fail", 0),
            summary.get("total_unique", 0),
            summary.get("ok_rate", 0),
            summary.get("starts", 0),
            summary.get("ends", 0),
        ],
        [],
        ["ALL-TIME (accounts.txt)"],
        ["Tong dong", "FULL", "OK", "FAIL"],
        [
            summary.get("alltime_total", 0),
            summary.get("alltime_full", 0),
            summary.get("alltime_ok", 0),
            summary.get("alltime_fail", 0),
        ],
        [],
        [
            f"Pass chung: {_common_password() or '(config)'} | Tab Acc FULL = email/pass/name | Acc FAIL | Lich su"
        ],
    ]
    dash.update(range_name="A1", values=grid, value_input_option="USER_ENTERED")

    # Acc FULL
    full_ws = get_or_create("Acc FULL")
    full_ws.clear()
    full_header = [
        [f"ACC FULL · {night_date} · {len(full)} acc", "", "", "", "", ""],
        ["#", "Email", "Password", "Sub2API Name", "Tag", "Ngay ca dem"],
    ]
    full_ws.update(
        range_name="A1",
        values=full_header + (full or [["(trống)", "", "", "", "", ""]]),
        value_input_option="USER_ENTERED",
    )

    # Acc FAIL
    fail_ws = get_or_create("Acc FAIL")
    fail_ws.clear()
    fail_header = [
        [f"ACC FAIL · {night_date} · {len(fail_rows)} acc", "", "", ""],
        ["Email", "Ly do ngan", "Status raw", "Ngay ca dem"],
    ]
    fail_ws.update(
        range_name="A1",
        values=fail_header + (fail_rows or [["(khong co fail)", "", "", ""]]),
        value_input_option="USER_ENTERED",
    )

    # Lich su append
    try:
        log_ws = get_or_create("Lich su")
        if log_ws.row_count == 0 or not log_ws.get_all_values():
            log_ws.append_row(
                ["Ngay", "Xuat luc", "Ca dem", "FULL", "FAIL", "Tong", "Ty le %", "START", "END"]
            )
        elif log_ws.cell(1, 1).value != "Ngay":
            # if empty first cell, write header
            vals = log_ws.get_all_values()
            if not vals:
                log_ws.append_row(
                    ["Ngay", "Xuat luc", "Ca dem", "FULL", "FAIL", "Tong", "Ty le %", "START", "END"]
                )
        log_ws.append_row(
            [
                night_date,
                summary.get("exported_at", ""),
                night_label,
                summary.get("acc_full", 0),
                summary.get("acc_fail", 0),
                summary.get("total_unique", 0),
                summary.get("ok_rate", 0),
                summary.get("starts", 0),
                summary.get("ends", 0),
            ]
        )
    except Exception as e:
        print(f"Lich su skip: {e}")

    url = f"https://docs.google.com/spreadsheets/d/{gs.get('spreadsheet_id')}/edit"
    return f"gspread ok → {url} (FULL={len(full)} FAIL={len(fail_rows)})"


def lookup_account_row(email: str) -> dict[str, str]:
    em = (email or "").strip().lower()
    out = {"email": email, "password": "", "status": "", "ts": "", "name": ""}
    if not em:
        return out
    acc = ROOT / "data" / "accounts.txt"
    if acc.exists():
        for ln in acc.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) >= 3 and parts[0].lower() == em:
                out["email"] = parts[0]
                out["password"] = parts[1]
                out["status"] = "|".join(parts[2:])
    out["name"] = sub2api_name(out["status"]) or out["status"]
    times = ROOT / "data" / "account_times.json"
    if times.exists():
        try:
            raw = json.loads(times.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out["ts"] = str(raw.get(em) or "")
        except Exception:
            pass
    if not out["ts"]:
        out["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def append_one_to_sheet(email: str, tab: str = "grok") -> str:
    gs = load_gs_config()
    if not gs.get("enabled", True):
        return "google_sheets disabled in config"
    row = lookup_account_row(email)
    payload = {
        "action": "append",
        "tab": tab or "grok",
        "account": {
            "email": row["email"] or email,
            "password": row["password"],
            "name": row["name"],
            "status": row["status"],
            "time": row["ts"],
            "vpn": "—",
        },
    }
    return push_via_webapp(gs, payload)


def export_to_google_sheets(data: dict[str, Any]) -> str:
    gs = load_gs_config()
    if not gs.get("enabled", True):
        return "google_sheets disabled in config"

    payload = build_payload(data)
    payload["tab"] = str(gs.get("tab") or "grok").strip() or "grok"
    # always dump last payload for debug / one-shot regen
    try:
        (ROOT / "data" / "gsheet_last_payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass

    errors: list[str] = []

    if (gs.get("webapp_url") or "").strip():
        try:
            return push_via_webapp(gs, payload)
        except Exception as e:
            errors.append(f"webapp: {e}")

    cred = Path(gs.get("credentials_file") or "gsheets_service_account.json")
    if not cred.is_absolute():
        cred = ROOT / cred
    if cred.exists():
        try:
            return push_via_service_account(gs, payload)
        except Exception as e:
            errors.append(f"service_account: {e}")

    hint = (
        "Chưa cấu hình Google Sheets auth.\n"
        "ONE-SHOT (push ngay ca dem): \n"
        "  1. Mo Apps Script tren sheet\n"
        "  2. Dan file gsheets_push_once.gs → Run writeGrokReport\n"
        "Tu dong moi sang:\n"
        "  Deploy gsheets_apps_script.gs as Web app → dien webapp_url vao config.json\n"
        f"Sheet: https://docs.google.com/spreadsheets/d/{gs.get('spreadsheet_id')}/edit#gid={gs.get('gid')}"
    )
    if errors:
        raise RuntimeError(hint + "\n\nLỗi: " + " | ".join(errors))
    raise RuntimeError(hint)


if __name__ == "__main__":
    import grokreg.tools.export_morning_report as emr

    d = emr.collect_report_data()
    print(export_to_google_sheets(d))
