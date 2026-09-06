from __future__ import annotations

import pytest
from fastapi import HTTPException

from web_console import app as console_app
from web_console.sub2api_sales_client import CapacityUnavailable


class FakeSalesClient:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple] = []
        self.fulfill_calls: list[tuple] = []
        self.fail_capacity = False
        self.fail_fulfill = False

    def availability(self, group_id: int) -> dict:
        return {
            "group_id": group_id,
            "active_accounts": 132,
            "capacity_tokens": 6_600_000,
            "outstanding_tokens": 11_797,
            "reserved_tokens": 0,
            "available_tokens": 6_588_203,
            "suggested_tokens": 6_588_000,
            "minimum_tokens": 1_000,
            "tokens_per_active_account": 50_000,
        }

    def reserve(self, *args, **kwargs) -> dict:
        self.reserve_calls.append((args, kwargs))
        if self.fail_capacity:
            raise CapacityUnavailable(6_588_203, 6_588_000, 1_000)
        return {"id": 81, "state": "held"}

    def fulfill_batch(self, *args, **kwargs) -> dict:
        self.fulfill_calls.append((args, kwargs))
        if self.fail_fulfill:
            raise RuntimeError("temporary failure")
        items = args[2]
        return {
            "api_keys": [
                {"id": index + 1, "name": item["name"], "key": f"sk-key-{index:02d}", "quota": 2.0}
                for index, item in enumerate(items)
            ],
            "idempotent": False,
        }


@pytest.fixture
def sales(monkeypatch: pytest.MonkeyPatch) -> FakeSalesClient:
    client = FakeSalesClient()
    monkeypatch.setattr(console_app, "get_sub2api_sales_client", lambda: client)
    monkeypatch.setattr(console_app, "get_sub2api_sales_settings", lambda: (34, 7, "https://grok.example"))
    return client


def test_pool_stats_delegates_to_authoritative_capacity(sales: FakeSalesClient) -> None:
    result = console_app.get_sub2api_pool_stats.__wrapped__()

    assert result["remaining_tokens"] == 6_588_203
    assert result["suggested_tokens"] == 6_588_000
    assert result["active_accounts"] == 132
    assert result["connected"] is True


def test_generate_reserves_the_complete_batch_before_fulfillment(sales: FakeSalesClient) -> None:
    result = console_app.generate_sub2api_keys(console_app.GenerateKeysRequest(
        token_amount=1_000_000, count=3, name_prefix="Retail", group_name="Grok",
    ))

    assert result["ok"] is True
    assert result["count"] == 3
    assert len(result["keys"]) == 3
    reserve_args, _ = sales.reserve_calls[0]
    assert reserve_args[3] == 3_000_000
    _, _, items = sales.fulfill_calls[0][0]
    assert [item["requested_tokens"] for item in items] == [1_000_000] * 3


def test_insufficient_batch_returns_exact_capacity_and_creates_zero_keys(sales: FakeSalesClient) -> None:
    sales.fail_capacity = True

    with pytest.raises(HTTPException) as caught:
        console_app.generate_sub2api_keys(console_app.GenerateKeysRequest(
            token_amount=10_000_000, count=1, name_prefix="Retail", group_name="Grok",
        ))

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "INSUFFICIENT_GROK_CAPACITY"
    assert caught.value.detail["available_tokens"] == 6_588_203
    assert caught.value.detail["suggested_tokens"] == 6_588_000
    assert sales.fulfill_calls == []


def test_transient_batch_failure_does_not_release_or_return_partial_keys(sales: FakeSalesClient) -> None:
    sales.fail_fulfill = True

    with pytest.raises(HTTPException) as caught:
        console_app.generate_sub2api_keys(console_app.GenerateKeysRequest(
            token_amount=1_000_000, count=2, name_prefix="Retail", group_name="Grok",
        ))

    assert caught.value.status_code == 502
    assert "partial" not in str(caught.value.detail).lower()
