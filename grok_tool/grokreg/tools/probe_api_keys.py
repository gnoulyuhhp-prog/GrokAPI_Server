#!/usr/bin/env python3
"""List Sub2API API keys and test which ones work."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from pathlib import Path

def _load_sub2() -> tuple[str, str, str]:
    """Credentials from local config.json only (never hardcode secrets)."""
    cfg_path = Path(__file__).resolve().parent / "config.json"
    base, email, password = "http://localhost:8080", "", ""
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            sub = cfg.get("sub2api") or {}
            base = str(sub.get("sub2api_url") or base).rstrip("/")
            email = str(sub.get("sub2api_user") or sub.get("sub2api_email") or "")
            password = str(sub.get("sub2api_pass") or sub.get("sub2api_password") or "")
        except Exception:
            pass
    return base, email, password


BASE, EMAIL, PASSWORD = _load_sub2()


def http(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: float = 45.0,
    api_key: str | None = None,
) -> tuple[int, Any, str]:
    url = path if path.startswith("http") else BASE + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw), raw
            except Exception:
                return resp.status, None, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return e.code, parsed, raw
    except Exception as e:
        return 0, None, str(e)


def main() -> int:
    code, data, raw = http(
        "POST",
        "/api/v1/auth/login",
        body={"email": EMAIL, "password": PASSWORD},
    )
    if code != 200 or not data or not data.get("data", {}).get("access_token"):
        print("LOGIN FAIL", code, raw[:300])
        return 1
    token = data["data"]["access_token"]
    print(f"LOGIN OK token_len={len(token)}")

    # discover chat routes from frontend
    _, _, html = http("GET", "/")
    m = re.search(r'src="(/assets/index-[^"]+)"', html or "")
    paths_found: list[str] = []
    if m:
        _, _, js = http("GET", m.group(1))
        paths_found = sorted(set(re.findall(r"/v1/[a-zA-Z0-9_./-]+", js or "")))
        print("JS routes sample:")
        for p in paths_found[:60]:
            print(" ", p)

    code, keys_data, raw = http("GET", "/api/v1/keys?page=1&page_size=100", token=token)
    items = (keys_data or {}).get("data", {}).get("items") or []
    print(f"\n=== API KEYS ({len(items)}) ===")
    for k in items:
        g = (k.get("group") or {}).get("name") or "-"
        print(
            f"id={k.get('id')} name={k.get('name')} status={k.get('status')} "
            f"group={g} last={k.get('last_used_at')} key={str(k.get('key'))[:22]}..."
        )

    if not items:
        print("No keys")
        return 1

    # models via first key
    sk0 = items[0]["key"]
    code, models, raw = http("GET", "/v1/models", api_key=sk0)
    model_ids = []
    if models and isinstance(models.get("data"), list):
        model_ids = [m["id"] for m in models["data"] if m.get("id")]
    print(f"\nMODELS available ({len(model_ids)}): {', '.join(model_ids[:15])}...")

    # candidate chat endpoints
    candidates = [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/chat/completions",
        "/api/v1/chat/completions",
        "/openai/v1/chat/completions",
        "/v1/grok/chat/completions",
        "/api/openai/v1/chat/completions",
        "/gateway/v1/chat/completions",
    ]
    for p in paths_found:
        if any(x in p for x in ("chat", "completion", "response", "message")):
            if p not in candidates:
                candidates.append(p)

    models_to_try = []
    for mid in ("grok-4.5", "grok-4.5-latest", "grok", "grok-4.3", "grok-latest"):
        if mid in model_ids or not model_ids:
            models_to_try.append(mid)
    if not models_to_try and model_ids:
        models_to_try = model_ids[:3]

    # find working endpoint once
    work_path = None
    work_model = None
    work_body_style = None  # openai | responses
    probe_key = sk0
    print("\n=== PROBE ENDPOINT ===")
    for path in candidates:
        for model in models_to_try:
            for style, body in (
                (
                    "openai",
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                        "max_tokens": 32,
                        "stream": False,
                    },
                ),
                (
                    "responses",
                    {
                        "model": model,
                        "input": "Reply with exactly: PONG",
                        "max_output_tokens": 32,
                    },
                ),
            ):
                t0 = time.time()
                code, data, raw = http(
                    "POST", path, api_key=probe_key, body=body, timeout=60
                )
                dt = time.time() - t0
                snippet = (raw or "")[:180].replace("\n", " ")
                # skip SPA html
                if snippet.strip().startswith("<!doctype") or "Sub2API - AI" in snippet:
                    print(f"{path} model={model} style={style} -> HTML SPA ({code}) {dt:.1f}s")
                    continue
                print(
                    f"{path} model={model} style={style} -> {code} {dt:.1f}s {snippet}"
                )
                if code == 200 and data is not None:
                    work_path, work_model, work_body_style = path, model, style
                    break
            if work_path:
                break
        if work_path:
            break

    if not work_path:
        print("\n!! Could not find working chat endpoint. Keys listed but not testable via HTTP chat.")
        # still try accounts test API if any
        return 2

    print(f"\nUsing endpoint={work_path} model={work_model} style={work_body_style}")
    print("\n=== TEST EACH KEY ===")
    results = []
    for k in items:
        name = k.get("name")
        status = k.get("status")
        sk = k.get("key")
        group = (k.get("group") or {}).get("name") or "-"
        if work_body_style == "responses":
            body = {
                "model": work_model,
                "input": "Reply with exactly: PONG",
                "max_output_tokens": 32,
            }
        else:
            body = {
                "model": work_model,
                "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
                "max_tokens": 32,
                "stream": False,
            }
        t0 = time.time()
        code, data, raw = http("POST", work_path, api_key=sk, body=body, timeout=90)
        dt = time.time() - t0
        ok = False
        reply = ""
        err = ""
        if code == 200 and data is not None:
            # extract content
            try:
                if "choices" in data:
                    reply = (
                        data["choices"][0]
                        .get("message", {})
                        .get("content")
                        or data["choices"][0].get("text")
                        or ""
                    )
                    ok = True
                elif "output" in data or "output_text" in data:
                    reply = str(data.get("output_text") or data.get("output") or "")[:200]
                    ok = True
                else:
                    reply = json.dumps(data)[:200]
                    ok = True
            except Exception as e:
                err = f"parse:{e}"
                reply = (raw or "")[:200]
        else:
            err = (raw or "")[:250].replace("\n", " ")
        results.append(
            {
                "id": k.get("id"),
                "name": name,
                "status": status,
                "group": group,
                "http": code,
                "ok": ok,
                "sec": round(dt, 1),
                "reply": (reply or "")[:120],
                "err": err[:200],
                "key_prefix": str(sk)[:18],
            }
        )
        flag = "OK" if ok else "FAIL"
        print(
            f"[{flag}] id={k.get('id')} name={name} group={group} http={code} {dt:.1f}s "
            f"reply={repr((reply or '')[:80])} err={err[:100]}"
        )

    print("\n=== SUMMARY ===")
    working = [r for r in results if r["ok"]]
    failing = [r for r in results if not r["ok"]]
    print(f"Working: {len(working)}/{len(results)}")
    for r in working:
        print(f"  ✓ {r['name']} (id={r['id']}) group={r['group']} {r['sec']}s key={r['key_prefix']}...")
    print(f"Failing: {len(failing)}/{len(results)}")
    for r in failing:
        print(f"  ✗ {r['name']} (id={r['id']}) group={r['group']} http={r['http']} {r['err'][:120]}")

    out = {
        "endpoint": work_path,
        "model": work_model,
        "results": results,
    }
    Path = __import__("pathlib").Path
    Path(__file__).with_name("api_key_test_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nSaved api_key_test_results.json")
    return 0 if working else 3


if __name__ == "__main__":
    raise SystemExit(main())
