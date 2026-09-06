"""
Deliver registered Grok SSO cookies into Sub2API via admin SSO import.

Ported/adapted from grok-register-web core/sub2api_client.py:
  POST /api/v1/admin/grok/sso-to-oauth  with sso_tokens + name + group_ids

Auth:
  - x-api-key (sub2api_api_token) preferred when set
  - else Bearer JWT from POST /api/v1/auth/login (email + password)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

log = logging.getLogger("grok_tool")

DEFAULT_TIMEOUT_SEC = 180
_TOKEN_REFRESH_SKEW = 5 * 60

_token_cache: dict[str, tuple[str, float]] = {}
_token_cache_lock = threading.Lock()
_auth_method_cache: dict[str, str] = {}
_auth_method_cache_lock = threading.Lock()
_AUTH_METHOD_INVALIDATED = object()


def _clean_credential(value: str) -> str:
    return "".join(ch for ch in (value or "") if 0x21 <= ord(ch) <= 0x7E)


class Sub2APIError(RuntimeError):
    """Sub2API delivery or admin API failure."""


def normalize_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = parsed.path.rstrip("/")
    lower = path.lower()
    for marker in ("/api/v1", "/api"):
        index = lower.find(marker)
        if index >= 0:
            path = path[:index]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def parse_group_ids(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value] if value > 0 else []
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            try:
                num = int(item)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out.append(num)
        return out
    text = str(value).strip().strip("[]")
    if not text:
        return []
    out = []
    for part in text.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            num = int(part)
        except ValueError:
            continue
        if num > 0:
            out.append(num)
    return out


def _unwrap_envelope(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and (
        "code" in payload or "message" in payload
    ):
        return payload.get("data")
    return payload


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_status(payload: dict[str, Any] | None) -> tuple[int, str]:
    """Real Grok usage code from a quota payload or stored snapshot."""
    data = payload if isinstance(payload, dict) else {}
    err = str(
        data.get("probe_error")
        or data.get("error")
        or data.get("message")
        or ""
    ).strip()
    snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    bill = data.get("billing") if isinstance(data.get("billing"), dict) else {}
    code = (
        _as_int(snap.get("status_code"), 0)
        or _as_int(bill.get("status_code"), 0)
        or _as_int(data.get("status_code"), 0)
    )
    blob = err + " " + str(data.get("probe_error") or "")
    if "403" in blob or "GROK_QUOTA_PROBE_UPSTREAM_ERROR" in blob:
        if code in (0, 200):
            code = 403
    return code, err


def _usage_ready(code: int, err: str) -> bool:
    """True when admin will show usage (not the Cấm/403 import artifact)."""
    if code == 429:
        return True
    if code != 200:
        return False
    low = (err or "").lower()
    if "403" in low or "grok_quota_probe_upstream" in low:
        return False
    return True


class Sub2APIClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_token: str = "",
        email: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        session: requests.Session | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        if not self.base_url:
            raise Sub2APIError("sub2api_url is empty")
        self.api_token = (api_token or "").strip()
        self.email = (email or "").strip()
        self.password = password or ""
        self.timeout = max(5.0, float(timeout or DEFAULT_TIMEOUT_SEC))
        self.connect_timeout = 2.5
        self.login_read_timeout = 4.0
        self.session = session or requests.Session()

    def _login(self) -> tuple[str, float]:
        if not self.email or not self.password:
            raise Sub2APIError("sub2api login requires email and password")
        url = f"{self.base_url}/api/v1/auth/login"
        try:
            response = self.session.post(
                url,
                json={"email": self.email, "password": self.password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=(self.connect_timeout, self.login_read_timeout),
            )
        except requests.RequestException as exc:
            raise Sub2APIError(f"sub2api login request failed: {exc}") from exc
        if not response.ok:
            body = (response.text or "")[:300]
            raise Sub2APIError(f"sub2api login failed: HTTP {response.status_code}: {body}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise Sub2APIError("sub2api login returned non-JSON") from exc
        data = _unwrap_envelope(payload)
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        token = str(
            data.get("access_token")
            or data.get("accessToken")
            or data.get("token")
            or ""
        ).strip()
        if not token:
            raise Sub2APIError("sub2api login did not return access_token")
        expires_in = _as_int(data.get("expires_in"), 3600)
        expires_at = time.time() + max(60, expires_in) - _TOKEN_REFRESH_SKEW
        return token, expires_at

    def resolve_token(self, *, force_refresh: bool = False) -> str:
        cache_key = self.base_url
        if self.api_token:
            with _auth_method_cache_lock:
                cached_method = _auth_method_cache.get(cache_key)
            if cached_method == "bearer":
                if not (self.email and self.password):
                    return self.api_token
            elif cached_method != _AUTH_METHOD_INVALIDATED:
                return self.api_token

        if not force_refresh:
            with _token_cache_lock:
                cached = _token_cache.get(cache_key)
                if cached and cached[1] > time.time():
                    return cached[0]
        token, expires_at = self._login()
        with _token_cache_lock:
            _token_cache[cache_key] = (token, expires_at)
        with _auth_method_cache_lock:
            _auth_method_cache[cache_key] = "bearer"
        return token

    def _auth_headers(self, token: str) -> dict[str, str]:
        cleaned = _clean_credential(token)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token and cleaned == _clean_credential(self.api_token):
            headers["x-api-key"] = cleaned
        else:
            headers["Authorization"] = f"Bearer {cleaned}"
        return headers

    def invalidate_auth_method(self) -> None:
        with _auth_method_cache_lock:
            _auth_method_cache.pop(self.base_url, None)

    def _retry_token_after_401(self, prev_token: str) -> str:
        if self.api_token and prev_token == self.api_token:
            self.invalidate_auth_method()
            with _auth_method_cache_lock:
                _auth_method_cache[self.base_url] = _AUTH_METHOD_INVALIDATED
            if not (self.email and self.password):
                raise Sub2APIError(
                    "sub2api HTTP 401: admin api-key rejected and no email/password fallback"
                )
            return self.resolve_token(force_refresh=True)
        return self.resolve_token(force_refresh=True)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        timeout: float | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        token = self.resolve_token()
        url = f"{self.base_url}{path}"
        read_timeout = float(timeout if timeout is not None else self.timeout)
        req_timeout = (self.connect_timeout, read_timeout)
        try:
            response = self.session.request(
                method,
                url,
                json=body,
                headers=self._auth_headers(token),
                timeout=req_timeout,
            )
        except requests.RequestException as exc:
            raise Sub2APIError(f"sub2api {method} {path} failed: {exc}") from exc

        if response.status_code == 401 and retry_on_401:
            token = self._retry_token_after_401(token)
            try:
                response = self.session.request(
                    method,
                    url,
                    json=body,
                    headers=self._auth_headers(token),
                    timeout=req_timeout,
                )
            except requests.RequestException as exc:
                raise Sub2APIError(f"sub2api {method} {path} failed: {exc}") from exc

        if not response.ok:
            body_text = (response.text or "")[:400]
            raise Sub2APIError(
                f"sub2api {method} {path} HTTP {response.status_code}: {body_text}"
            )
        if not (response.text or "").strip():
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise Sub2APIError(f"sub2api {method} {path} returned non-JSON") from exc
        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            if code not in (0, "0", None, 200, "200"):
                message = payload.get("message") or payload.get("error") or str(code)
                raise Sub2APIError(f"sub2api {method} {path} rejected: {message}")
        return _unwrap_envelope(payload)

    def list_groups(self) -> list[dict[str, Any]]:
        data = self._request_json(
            "GET",
            "/api/v1/admin/groups/all",
            timeout=min(30.0, self.timeout),
        )
        groups: list[dict[str, Any]] = []
        if isinstance(data, list):
            groups = [g for g in data if isinstance(g, dict)]
        elif isinstance(data, dict):
            for key in ("items", "data", "list", "groups"):
                value = data.get(key)
                if isinstance(value, list):
                    groups = [g for g in value if isinstance(g, dict)]
                    break
        return groups

    def resolve_group_ids_by_name(self, group_name: str) -> list[int]:
        """Match group name (case-insensitive contains) → ids. Prefer exact match."""
        want = (group_name or "").strip().lower()
        if not want:
            return []
        groups = self.list_groups()
        exact: list[int] = []
        partial: list[int] = []
        for g in groups:
            name = str(g.get("name") or g.get("title") or "").strip()
            gid = _as_int(g.get("id") or g.get("group_id"), 0)
            if gid <= 0 or not name:
                continue
            # Prefer Grok platform groups
            plat = str(g.get("platform") or "").strip().lower()
            if plat and plat not in ("", "grok"):
                continue
            nl = name.lower()
            if nl == want:
                exact.append(gid)
            elif want in nl or nl in want:
                partial.append(gid)
        return exact or partial

    def test_connection(self) -> dict[str, Any]:
        token = self.resolve_token(force_refresh=bool(self.email and self.password))
        try:
            groups = self.list_groups()
        except Sub2APIError as exc:
            return {"ok": False, "error": str(exc)}
        grok_groups = [
            g
            for g in groups
            if str(g.get("platform") or "").strip().lower() in {"", "grok"}
        ]
        with _auth_method_cache_lock:
            detected = _auth_method_cache.get(self.base_url)
        if detected == "bearer":
            auth_label = "login"
        elif self.api_token:
            auth_label = "api_key"
        else:
            auth_label = "login"
        return {
            "ok": True,
            "base_url": self.base_url,
            "auth": auth_label,
            "group_count": len(groups),
            "grok_group_count": len(grok_groups),
            "token_preview": f"{token[:8]}…" if token else "",
        }

    def search_accounts(
        self,
        query: str,
        *,
        platform: str = "grok",
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Admin account search. Sub2API matches NAME only (not credentials.email)."""
        q = (query or "").strip()
        if not q:
            return []
        path = f"/api/v1/admin/accounts?search={quote(q)}&page=1&page_size={max(1, int(page_size))}"
        if platform:
            path += f"&platform={quote(platform)}"
        data = self._request_json("GET", path, timeout=min(30.0, self.timeout))
        if isinstance(data, list):
            return [a for a in data if isinstance(a, dict)]
        if isinstance(data, dict):
            items = data.get("items") or data.get("accounts") or data.get("list") or []
            if isinstance(items, list):
                return [a for a in items if isinstance(a, dict)]
        return []

    def find_account_by_name(self, name: str) -> dict[str, Any] | None:
        want = (name or "").strip().lower()
        if not want:
            return None
        for acc in self.search_accounts(name, page_size=50):
            if str(acc.get("name") or "").strip().lower() == want:
                return acc
        return None

    def get_account(self, account_id: int) -> dict[str, Any]:
        aid = _as_int(account_id, 0)
        if aid <= 0:
            raise Sub2APIError("account_id is invalid")
        data = self._request_json(
            "GET",
            f"/api/v1/admin/accounts/{aid}",
            timeout=min(30.0, self.timeout),
        )
        if not isinstance(data, dict):
            raise Sub2APIError(f"sub2api account {aid} returned unexpected payload")
        return data

    def probe_quota(self, account_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        """GET /admin/grok/accounts/:id/quota — same as admin 'Đầu dò'."""
        aid = _as_int(account_id, 0)
        if aid <= 0:
            raise Sub2APIError("account_id is invalid")
        wait = 12.0 if timeout is None else max(3.0, float(timeout))
        data = self._request_json(
            "GET",
            f"/api/v1/admin/grok/accounts/{aid}/quota",
            timeout=wait,
        )
        if not isinstance(data, dict):
            raise Sub2APIError(f"sub2api quota probe {aid} returned unexpected payload")
        return data

    def ensure_usage_visible(
        self,
        account_id: int,
        *,
        budget_sec: float = 20.0,
        wait_import_probe_sec: float | None = None,
        retries: int = 1,
    ) -> dict[str, Any]:
        """
        After SSO import, keep probing until admin can show usage (200/429).

        Sub2API's first auto-probe is often 403 (Cấm) — that is not a ban.
        We retry until a real usage snapshot lands, within budget_sec.
        """
        aid = _as_int(account_id, 0)
        if aid <= 0:
            raise Sub2APIError("account_id is invalid")

        budget = min(30.0, max(8.0, float(budget_sec or 20.0)))
        started = time.time()
        deadline = started + budget
        last_code = 0
        last_err = ""
        last_headers = False
        last_source = None
        attempt = 0

        while True:
            attempt += 1
            remaining = deadline - time.time()
            if remaining < 2.5 and attempt > 1:
                break
            try:
                acc = self.get_account(aid)
                extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
                snap = extra.get("grok_usage_snapshot")
                if isinstance(snap, dict) and snap:
                    code, err = _usage_status(snap)
                    if _usage_ready(code, err):
                        elapsed = round(time.time() - started, 2)
                        log.info(
                            "[sub2api-api] usage already visible id=%s code=%s elapsed=%.1fs",
                            aid,
                            code,
                            elapsed,
                        )
                        return {
                            "ok": True,
                            "account_id": aid,
                            "status_code": code,
                            "waited": True,
                            "elapsed_s": elapsed,
                            "attempts": attempt,
                        }
            except Sub2APIError as exc:
                log.debug("[sub2api-api] wait usage id=%s: %s", aid, exc)

            probe_timeout = min(12.0, max(3.0, remaining if remaining > 0 else 8.0))
            try:
                last = self.probe_quota(aid, timeout=probe_timeout)
            except Sub2APIError as exc:
                last_err = str(exc)[:200]
                last_code = last_code or 0
                log.warning(
                    "[sub2api-api] usage probe fail id=%s try=%s: %s",
                    aid,
                    attempt,
                    exc,
                )
                last = {}
            else:
                last_code, last_err = _usage_status(last)
                last_headers = bool(last.get("headers_observed"))
                last_source = last.get("source")
                log.info(
                    "[sub2api-api] usage probe id=%s try=%s code=%s headers=%s err=%s",
                    aid,
                    attempt,
                    last_code,
                    last_headers,
                    (last_err[:80] if last_err else ""),
                )
                if _usage_ready(last_code, last_err):
                    elapsed = round(time.time() - started, 2)
                    return {
                        "ok": True,
                        "account_id": aid,
                        "status_code": last_code,
                        "headers_observed": last_headers,
                        "source": last_source,
                        "elapsed_s": elapsed,
                        "attempts": attempt,
                        "note": "rate_limited_shows_usage" if last_code == 429 else None,
                    }

            if time.time() >= deadline:
                break
            time.sleep(min(2.5, max(0.8, deadline - time.time())))

        elapsed = round(time.time() - started, 2)
        return {
            "ok": False,
            "account_id": aid,
            "status_code": last_code,
            "headers_observed": last_headers,
            "source": last_source,
            "probe_error": last_err or None,
            "elapsed_s": elapsed,
            "attempts": attempt,
        }

    def import_sso(
        self,
        sso_cookie: str,
        *,
        email: str = "",
        name: str = "",
        group_ids: list[int] | None = None,
        proxy_id: int | None = None,
        concurrency: int = 1,
        priority: int = 1,
        auto_pause_on_expired: bool = True,
        notes: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sso = (sso_cookie or "").strip()
        if not sso:
            raise Sub2APIError("SSO cookie is empty")

        body: dict[str, Any] = {
            "sso_tokens": [sso],
            "concurrency": max(0, int(concurrency)),
            "priority": int(priority),
            "auto_pause_on_expired": bool(auto_pause_on_expired),
        }
        account_name = (name or email or "").strip()
        if account_name:
            body["name"] = account_name
        note = (notes if notes is not None else email or "").strip()
        if note:
            body["notes"] = note
        extra_body: dict[str, Any] = dict(extra or {})
        if email and "email" not in extra_body:
            extra_body["email"] = email
        if email and "email_address" not in extra_body:
            extra_body["email_address"] = email
        if extra_body:
            body["extra"] = extra_body
        if group_ids:
            body["group_ids"] = [int(g) for g in group_ids if int(g) > 0]
        if proxy_id and int(proxy_id) > 0:
            body["proxy_id"] = int(proxy_id)

        log.info(
            "[sub2api-api] SSO import start name=%s groups=%s",
            account_name or "(unnamed)",
            body.get("group_ids") or [],
        )
        data = self._request_json(
            "POST",
            "/api/v1/admin/grok/sso-to-oauth",
            body=body,
            timeout=self.timeout,
        )
        if not isinstance(data, dict):
            raise Sub2APIError("sub2api SSO import returned unexpected payload")

        created = data.get("created") if isinstance(data.get("created"), list) else []
        failed = data.get("failed") if isinstance(data.get("failed"), list) else []

        if failed:
            first = failed[0] if isinstance(failed[0], dict) else {}
            err = str(first.get("error") or "SSO import failed")
            raise Sub2APIError(f"sub2api SSO import failed: {err}")
        if not created:
            raise Sub2APIError("sub2api SSO import returned no created accounts")

        item = created[0] if isinstance(created[0], dict) else {}
        account = item.get("account") if isinstance(item.get("account"), dict) else {}
        created_id = account.get("id")
        created_name = item.get("name") or account.get("name") or account_name
        cred_email = ""
        if isinstance(account.get("credentials"), dict):
            cred_email = str(account["credentials"].get("email") or "")
        log.info(
            "[sub2api-api] SSO import OK name=%s email=%s id=%s — search admin by NAME %r (email search is name-only)",
            created_name or "(unnamed)",
            item.get("email") or cred_email or email or "",
            created_id or "",
            created_name or account_name,
        )
        return {
            "ok": True,
            "created_count": len(created),
            "failed_count": len(failed),
            "name": created_name,
            "email": item.get("email") or cred_email or email or "",
            "account_id": created_id,
            "account": account,
            "raw": data,
        }


def client_from_cfg(sub_cfg: dict[str, Any]) -> Sub2APIClient:
    """Build client from grok_tool config.sub2api dict."""
    timeout = _as_int(
        sub_cfg.get("timeout_sec") or sub_cfg.get("timeout_oauth_sec"),
        DEFAULT_TIMEOUT_SEC,
    )
    return Sub2APIClient(
        sub_cfg.get("sub2api_url") or "",
        api_token=str(sub_cfg.get("sub2api_api_token") or sub_cfg.get("api_token") or "").strip(),
        email=str(
            sub_cfg.get("sub2api_user")
            or sub_cfg.get("sub2api_email")
            or sub_cfg.get("email")
            or ""
        ).strip(),
        password=str(
            sub_cfg.get("sub2api_pass")
            or sub_cfg.get("sub2api_password")
            or sub_cfg.get("password")
            or ""
        ),
        timeout=timeout,
    )


def export_sso_to_sub2api(
    sub_cfg: dict[str, Any],
    sso_cookie: str,
    *,
    email: str = "",
    name: str = "",
) -> dict[str, Any]:
    """
    Import one SSO into Sub2API via API.
    Resolves group by group_ids or by name (group / name_prefix).
    """
    base = normalize_base_url(str(sub_cfg.get("sub2api_url") or ""))
    token = str(sub_cfg.get("sub2api_api_token") or sub_cfg.get("api_token") or "").strip()
    user = str(
        sub_cfg.get("sub2api_user") or sub_cfg.get("sub2api_email") or ""
    ).strip()
    password = str(sub_cfg.get("sub2api_pass") or sub_cfg.get("sub2api_password") or "")
    if not base:
        raise Sub2APIError("sub2api_url is empty")
    if not token and not (user and password):
        raise Sub2APIError("sub2api needs api_token or user/pass")

    client = client_from_cfg(sub_cfg)
    group_ids = parse_group_ids(sub_cfg.get("group_ids") or sub_cfg.get("sub2api_group_ids"))
    group_name = str(sub_cfg.get("group") or "grok free").strip()
    if not group_ids and group_name:
        try:
            group_ids = client.resolve_group_ids_by_name(group_name)
            if group_ids:
                log.info(
                    "[sub2api-api] resolved group %r → ids=%s",
                    group_name,
                    group_ids,
                )
            else:
                log.warning(
                    "[sub2api-api] group name %r not found — import without group_ids",
                    group_name,
                )
        except Sub2APIError as e:
            log.warning("[sub2api-api] resolve group failed: %s", e)
            raise
        except Exception as e:
            log.warning("[sub2api-api] resolve group failed: %s", e)

    account_name = (name or email or "").strip()
    email_clean = (email or "").strip()
    if sub_cfg.get("name_include_email") and email_clean:
        if email_clean.lower() not in account_name.lower():
            account_name = f"{account_name} {email_clean}".strip()
    proxy_raw = str(sub_cfg.get("proxy_id") or sub_cfg.get("sub2api_proxy_id") or "").strip()
    proxy_id = _as_int(proxy_raw, 0) or None
    concurrency = _as_int(sub_cfg.get("concurrency") or 1, 1)
    priority = _as_int(sub_cfg.get("priority") or 1, 1)
    auto_pause = str(sub_cfg.get("auto_pause_on_expired", "true")).lower() in (
        "1",
        "true",
        "yes",
    )

    result = client.import_sso(
        sso_cookie,
        email=email_clean,
        name=account_name,
        group_ids=group_ids,
        proxy_id=proxy_id,
        concurrency=concurrency,
        priority=priority,
        auto_pause_on_expired=auto_pause,
        notes=email_clean or None,
        extra={"email": email_clean, "email_address": email_clean} if email_clean else None,
    )
    if sub_cfg.get("refresh_usage_after_import", True):
        aid = _as_int(result.get("account_id"), 0)
        if aid > 0:
            try:
                usage = client.ensure_usage_visible(
                    aid,
                    budget_sec=_as_int(sub_cfg.get("usage_refresh_sec"), 20) or 20,
                )
                result["usage"] = usage
                if usage.get("ok"):
                    log.info(
                        "[sub2api-api] usage ready id=%s code=%s — admin will show usage not Cấm",
                        aid,
                        usage.get("status_code"),
                    )
                else:
                    log.warning(
                        "[sub2api-api] usage still not 200 id=%s code=%s err=%s",
                        aid,
                        usage.get("status_code"),
                        usage.get("probe_error"),
                    )
            except Exception as e:
                log.warning("[sub2api-api] usage refresh skipped id=%s: %s", aid, e)
        else:
            log.warning("[sub2api-api] no account_id after import — cannot refresh usage")
    return result


def test_sub2api_connection(sub_cfg: dict[str, Any]) -> dict[str, Any]:
    base = normalize_base_url(str(sub_cfg.get("sub2api_url") or ""))
    token = str(sub_cfg.get("sub2api_api_token") or "").strip()
    user = str(sub_cfg.get("sub2api_user") or sub_cfg.get("sub2api_email") or "").strip()
    password = str(sub_cfg.get("sub2api_pass") or sub_cfg.get("sub2api_password") or "")
    if not base:
        return {"ok": False, "error": "sub2api_url is empty"}
    if not token and not (user and password):
        return {"ok": False, "error": "provide api_token or user/pass"}
    try:
        return client_from_cfg(sub_cfg).test_connection()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
