"""Delivery: Sub2API SSO import + durable retry + OAuth UI."""
from __future__ import annotations

# Prefer package copies; fall back handled by root modules still present.
try:
    from grokreg.delivery.sub2api_client import (  # noqa: F401
        Sub2APIClient,
        Sub2APIError,
        export_sso_to_sub2api,
        client_from_cfg,
    )
except Exception:
    pass
