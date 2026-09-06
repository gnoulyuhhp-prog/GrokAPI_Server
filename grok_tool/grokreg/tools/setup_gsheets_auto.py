#!/usr/bin/env python3
"""
Auto-setup Google Sheet push for Grok overnight.

Prereq (1 click by user):
  https://script.google.com/home/usersettings
  → bật ON "Google Apps Script API"

Uses clasp OAuth tokens from %USERPROFILE%\\.clasprc.json
Then: create project → upload Code.gs → version → deploy webapp → config.json → test push
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CLASPRC = Path.home() / ".clasprc.json"
CONFIG = ROOT / "config.json"
CODE_GS = ROOT / "gsheets_clasp" / "Code.gs"
if not CODE_GS.exists():
    CODE_GS = ROOT / "scripts" / "gsheets" / "gsheets_apps_script.gs"
APPSSCRIPT_JSON = ROOT / "gsheets_clasp" / "appsscript.json"
FIX_LOG = ROOT / "data" / "setup_gsheets_log.txt"
STATE = ROOT / "scripts" / "gsheets" / "clasp" / "setup_state.json"


def _sheet_from_config() -> tuple[str, int, str]:
    """Read spreadsheet id / gid / webapp_secret from local config only."""
    if not CONFIG.exists():
        raise SystemExit("Missing config.json — copy config.example.json and fill google_sheets.*")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    gs = cfg.get("google_sheets") or {}
    sid = str(gs.get("spreadsheet_id") or "").strip()
    if not sid:
        raise SystemExit("config.json google_sheets.spreadsheet_id is empty")
    gid = int(gs.get("gid") or 0)
    secret = str(gs.get("webapp_secret") or "").strip()
    return sid, gid, secret


SHEET_ID, GID, WEBAPP_SECRET = "", 0, ""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] SETUP {msg}"
    print(line, flush=True)
    try:
        with open(FIX_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_token() -> str:
    if not CLASPRC.exists():
        raise SystemExit(
            "Chưa clasp login. Chạy: npx @google/clasp login\n"
            f"Missing {CLASPRC}"
        )
    data = json.loads(CLASPRC.read_text(encoding="utf-8"))
    tok = data["tokens"]["default"]
    exp = tok.get("expiry_date", 0) / 1000
    if exp < time.time() + 60:
        log("refreshing OAuth token...")
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": tok["client_id"],
                "client_secret": tok["client_secret"],
                "refresh_token": tok["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        tok["access_token"] = j["access_token"]
        if "expires_in" in j:
            tok["expiry_date"] = int((time.time() + j["expires_in"]) * 1000)
        data["tokens"]["default"] = tok
        CLASPRC.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return tok["access_token"]


def api(method: str, url: str, access: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {access}"
    if "json" in kwargs:
        headers.setdefault("Content-Type", "application/json")
    r = requests.request(method, url, headers=headers, timeout=90, **kwargs)
    return r


def wait_script_api(access: str, max_wait: int = 600) -> None:
    """Poll until Apps Script API works (user toggled settings)."""
    url = "https://script.googleapis.com/v1/projects"
    t0 = time.time()
    n = 0
    while time.time() - t0 < max_wait:
        n += 1
        r = api(
            "POST",
            url,
            access,
            json={"title": f"Grok API Probe {int(time.time())}"},
        )
        if r.status_code in (200, 201):
            # delete probe? leave it; or ignore
            sid = r.json().get("scriptId")
            log(f"Apps Script API ON (probe scriptId={sid})")
            return
        if r.status_code == 403 and "not enabled" in r.text.lower():
            if n == 1 or n % 4 == 0:
                log(
                    "WAITING: bật Google Apps Script API tại "
                    "https://script.google.com/home/usersettings  "
                    f"(đã chờ {int(time.time()-t0)}s)"
                )
            time.sleep(15)
            access = load_token()
            continue
        # other error — still retry a bit
        log(f"probe status={r.status_code} {r.text[:200]}")
        time.sleep(10)
        access = load_token()
    raise SystemExit(
        "TIMEOUT: Apps Script API vẫn tắt. "
        "Mở https://script.google.com/home/usersettings bật ON rồi chạy lại:\n"
        "  python setup_gsheets_auto.py"
    )


def create_project(access: str) -> str:
    # Prefer container-bound to the target sheet
    r = api(
        "POST",
        "https://script.googleapis.com/v1/projects",
        access,
        json={"title": "Grok Overnight Export", "parentId": SHEET_ID},
    )
    if r.status_code >= 400:
        log(f"bound create failed {r.status_code}: {r.text[:200]} — try standalone")
        r = api(
            "POST",
            "https://script.googleapis.com/v1/projects",
            access,
            json={"title": "Grok Overnight Export"},
        )
    r.raise_for_status()
    sid = r.json()["scriptId"]
    log(f"created scriptId={sid}")
    return sid


def upload_content(access: str, script_id: str) -> None:
    code = CODE_GS.read_text(encoding="utf-8")
    if APPSSCRIPT_JSON.exists():
        manifest = APPSSCRIPT_JSON.read_text(encoding="utf-8")
    else:
        manifest = json.dumps(
            {
                "timeZone": "Asia/Ho_Chi_Minh",
                "dependencies": {},
                "exceptionLogging": "STACKDRIVER",
                "runtimeVersion": "V8",
                "webapp": {"executeAs": "USER_DEPLOYING", "access": "ANYONE"},
            },
            indent=2,
        )
    body = {
        "files": [
            {"name": "Code", "type": "SERVER_JS", "source": code},
            {"name": "appsscript", "type": "JSON", "source": manifest},
        ]
    }
    r = api(
        "PUT",
        f"https://script.googleapis.com/v1/projects/{script_id}/content",
        access,
        json=body,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"upload content failed {r.status_code}: {r.text[:400]}")
    log("uploaded Code.gs + appsscript.json")


def create_version(access: str, script_id: str) -> int:
    r = api(
        "POST",
        f"https://script.googleapis.com/v1/projects/{script_id}/versions",
        access,
        json={"description": "grok overnight webapp"},
    )
    r.raise_for_status()
    ver = int(r.json().get("versionNumber") or 1)
    log(f"version={ver}")
    return ver


def deploy_webapp(access: str, script_id: str, version: int) -> str:
    # Create deployment entry point WEB_APP
    body = {
        "versionNumber": version,
        "description": "grok-overnight-webapp",
        "manifestFileName": "appsscript",
    }
    r = api(
        "POST",
        f"https://script.googleapis.com/v1/projects/{script_id}/deployments",
        access,
        json=body,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"deploy failed {r.status_code}: {r.text[:500]}")
    dep = r.json()
    dep_id = dep.get("deploymentId") or ""
    # entryPoints may contain webApp url
    url = None
    for ep in dep.get("entryPoints") or []:
        if ep.get("entryPointType") == "WEB_APP" or "webApp" in ep:
            wa = ep.get("webApp") or ep
            url = wa.get("url") or wa.get("entryPointConfig", {}).get("url")
            if url:
                break
    if not url and dep_id:
        # Classic web app URL form uses deployment id
        url = f"https://script.google.com/macros/s/{dep_id}/exec"
    if not url:
        raise RuntimeError(f"no webapp url in deploy response: {json.dumps(dep)[:500]}")
    log(f"deployed deploymentId={dep_id}")
    log(f"webapp_url={url}")
    return url


def save_config(webapp_url: str) -> None:
    global SHEET_ID, GID, WEBAPP_SECRET
    if not SHEET_ID:
        SHEET_ID, GID, WEBAPP_SECRET = _sheet_from_config()
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    gs = cfg.setdefault("google_sheets", {})
    gs.update(
        {
            "enabled": True,
            "spreadsheet_id": SHEET_ID,
            "gid": GID,
            "webapp_url": webapp_url,
            "webapp_secret": WEBAPP_SECRET or gs.get("webapp_secret") or "",
            "credentials_file": "gsheets_service_account.json",
            "mode": "replace_night",
            "write_txt_backup": True,
            "push_max_attempts": 3,
            "require_sheet_success": True,
        }
    )
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log("config.json updated (local only)")


def test_push() -> int:
    import grokreg.tools.export_morning_report as emr

    log("testing export_morning_report (sheet REQUIRED)...")
    rc = int(emr.main())
    log(f"export exit={rc}")
    return rc


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    global SHEET_ID, GID, WEBAPP_SECRET
    log("=== Grok Google Sheets auto setup ===")
    if not CODE_GS.exists():
        raise SystemExit(f"missing {CODE_GS}")

    SHEET_ID, GID, WEBAPP_SECRET = _sheet_from_config()
    log(f"using spreadsheet from config (gid={GID})")

    access = load_token()
    log("OAuth token OK (clasp)")

    # Ensure source files present
    (ROOT / "gsheets_clasp").mkdir(exist_ok=True)
    if not (ROOT / "gsheets_clasp" / "Code.gs").exists():
        (ROOT / "gsheets_clasp" / "Code.gs").write_text(
            CODE_GS.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if not APPSSCRIPT_JSON.exists():
        APPSSCRIPT_JSON.write_text(
            json.dumps(
                {
                    "timeZone": "Asia/Ho_Chi_Minh",
                    "exceptionLogging": "STACKDRIVER",
                    "runtimeVersion": "V8",
                    "webapp": {"executeAs": "USER_DEPLOYING", "access": "ANYONE"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    log("Open settings if needed: https://script.google.com/home/usersettings")
    wait_script_api(access)
    access = load_token()

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    script_id = state.get("scriptId")
    if not script_id:
        script_id = create_project(access)
        state["scriptId"] = script_id
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        log(f"reuse scriptId={script_id}")

    upload_content(access, script_id)
    ver = create_version(access, script_id)
    webapp_url = deploy_webapp(access, script_id, ver)
    state["webapp_url"] = webapp_url
    state["version"] = ver
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # also write .clasp.json for future clasp use
    (ROOT / "gsheets_clasp" / ".clasp.json").write_text(
        json.dumps({"scriptId": script_id, "rootDir": "."}, indent=2),
        encoding="utf-8",
    )

    save_config(webapp_url)

    # Warm-up: first request may need user to open URL once
    log("warmup GET webapp (open browser if Authorization required)...")
    try:
        import webbrowser

        webbrowser.open(webapp_url)
    except Exception:
        pass
    try:
        wr = requests.get(webapp_url, timeout=30, allow_redirects=True)
        log(f"warmup GET {wr.status_code} {wr.text[:100]}")
    except Exception as e:
        log(f"warmup note: {e}")

    time.sleep(2)
    rc = test_push()
    if rc == 0:
        log("SUCCESS — Google Sheet push working")
        print(f"\nSheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={GID}")
        print(f"Webapp: {webapp_url}")
        return 0

    log("Push still failing — open webapp URL in browser, click Allow, then re-run test:")
    log(f"  {webapp_url}")
    log("  python export_morning_report.py")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
