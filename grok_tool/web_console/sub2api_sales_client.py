from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

@dataclass(slots=True)
class CapacityUnavailable(RuntimeError):
    available_tokens: int
    suggested_tokens: int
    minimum_tokens: int

    def __str__(self) -> str:
        return "INSUFFICIENT_GROK_CAPACITY"


class Sub2APISalesClient:
    def __init__(
        self,
        base_url: str,
        admin_api_key: str,
        sales_secret: str,
        *,
        session: Any | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_api_key = admin_api_key.strip()
        self.sales_secret = sales_secret.strip()
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.timeout = min(max(float(timeout), 1.0), 30.0)
        if not self.base_url or not self.admin_api_key or not self.sales_secret:
            raise ValueError("Sub2API sales client is not configured")

    def _post(self, endpoint: str, payload: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.sales_secret.encode("utf-8"),
            timestamp.encode("ascii") + b"\n" + raw,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.admin_api_key,
            "X-Sales-Timestamp": timestamp,
            "X-Sales-Signature": signature,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self.session.post(
                f"{self.base_url}/api/v1/admin/api-keys/sales/{endpoint}",
                data=raw,
                headers=headers,
                timeout=self.timeout,
            )
            body = response.json()
        except Exception as exc:
            raise RuntimeError("Sub2API sales request failed") from exc

        reason = str(body.get("reason") or body.get("code") or "")
        if response.status_code == 409 and reason == "INSUFFICIENT_GROK_CAPACITY":
            details = body.get("metadata") or body.get("details") or {}
            raise CapacityUnavailable(
                available_tokens=int(details.get("available_tokens") or 0),
                suggested_tokens=int(details.get("suggested_tokens") or 0),
                minimum_tokens=int(details.get("minimum_tokens") or 0),
            )
        if response.status_code >= 400:
            raise RuntimeError(reason or f"Sub2API sales request failed ({response.status_code})")
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise RuntimeError("Sub2API sales response is invalid")
        return data

    def availability(self, group_id: int) -> dict[str, Any]:
        return self._post("availability", {"group_id": int(group_id)})

    def reserve(
        self,
        reference: str,
        operation: str,
        group_id: int,
        tokens: int,
        expires_at: datetime,
        *,
        target_key: str = "",
    ) -> dict[str, Any]:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        payload: dict[str, Any] = {
            "external_reference": reference,
            "operation": operation,
            "group_id": int(group_id),
            "requested_tokens": int(tokens),
            "expires_at": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if target_key:
            payload["target_key"] = target_key
        return self._post("reserve", payload)

    def release(self, reservation_id: int) -> dict[str, Any]:
        return self._post("release", {"reservation_id": int(reservation_id)})

    def fulfill_batch(
        self,
        reservation_id: int,
        group_id: int,
        items: list[dict[str, Any]],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._post(
            "fulfill-batch",
            {"reservation_id": int(reservation_id), "group_id": int(group_id), "items": items},
            idempotency_key=idempotency_key,
        )
