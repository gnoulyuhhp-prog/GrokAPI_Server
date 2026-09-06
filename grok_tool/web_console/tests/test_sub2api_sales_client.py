from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from web_console.sub2api_sales_client import CapacityUnavailable, Sub2APISalesClient


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[str, bytes, dict, float]] = []

    def post(self, url: str, *, data: bytes, headers: dict, timeout: float) -> FakeResponse:
        self.calls.append((url, data, headers, timeout))
        return self.response


def test_availability_signs_the_exact_raw_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1_800_000_000)
    session = FakeSession(FakeResponse(200, {"data": {"available_tokens": 6_588_203}}))
    client = Sub2APISalesClient("https://grok.example", "admin-token", "sales-secret", session=session)

    result = client.availability(34)

    assert result["available_tokens"] == 6_588_203
    url, raw, headers, timeout = session.calls[0]
    assert url == "https://grok.example/api/v1/admin/api-keys/sales/availability"
    assert json.loads(raw) == {"group_id": 34}
    expected = hmac.new(b"sales-secret", b"1800000000\n" + raw, hashlib.sha256).hexdigest()
    assert headers["X-Sales-Signature"] == expected
    assert headers["X-Sales-Timestamp"] == "1800000000"
    assert headers["x-api-key"] == "admin-token"
    assert timeout <= 30


def test_capacity_conflict_preserves_exact_and_suggested_tokens() -> None:
    session = FakeSession(FakeResponse(409, {
        "reason": "INSUFFICIENT_GROK_CAPACITY",
        "metadata": {
            "available_tokens": "6588203",
            "suggested_tokens": "6588000",
            "minimum_tokens": "1000",
        },
    }))
    client = Sub2APISalesClient("https://grok.example", "admin-token", "sales-secret", session=session)

    with pytest.raises(CapacityUnavailable) as caught:
        client.reserve("console-1", "new_key", 34, 10_000_000, datetime.now(timezone.utc))

    assert caught.value.available_tokens == 6_588_203
    assert caught.value.suggested_tokens == 6_588_000
    assert caught.value.minimum_tokens == 1_000


def test_errors_never_include_plaintext_target_key() -> None:
    target = "sk-plain-secret-renewal-key-123456"
    session = FakeSession(FakeResponse(500, {"message": f"failed for {target}"}))
    client = Sub2APISalesClient("https://grok.example", "admin-token", "sales-secret", session=session)

    with pytest.raises(RuntimeError) as caught:
        client.reserve("console-2", "renew_key", 34, 1_000_000, datetime.now(timezone.utc), target_key=target)

    assert target not in str(caught.value)
