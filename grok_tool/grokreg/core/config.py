"""Config loader and defaults for grokreg."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from grokreg.core.runtime import CONFIG_PATH


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    cfg.setdefault("email_provider", "auto_temp")
    cfg.setdefault("hotmail_list", "data/hotmails.txt")
    cfg.setdefault("hotmail_max_aliases", 5)
    cfg.setdefault(
        "azpopmail",
        {
            "base_url": "https://azpopmail.com",
            "token": "",
            "verify_ssl": False,
            "domains": [],
            "poll_interval": 1.5,
        },
    )
    cfg.setdefault(
        "tmail_wibu",
        {
            "base_url": "https://tmail.wibucrypto.pro",
            "verify_ssl": True,
            "create_mode": "create",
            "poll_interval": 1.5,
            "domains": [
                "wibucrypto.pro",
                "aden.name.ng",
                "adix.name.ng",
                "aban.edu.vn",
                "codedcapcut.email",
            ],
        },
    )
    cfg.setdefault("fresh_profile_per_account", True)
    cfg.setdefault("reuse_chrome_profile", False)
    cfg.setdefault("keep_browser_open", False)
    cfg.setdefault("humanize", False)
    cfg.setdefault("human_delay_min", 0.2)
    cfg.setdefault("human_delay_max", 0.8)
    cfg.setdefault("inter_success_delay_min", 1)
    cfg.setdefault("inter_success_delay_max", 3)
    cfg.setdefault("mail_fail_cooldown_min", 30)
    cfg.setdefault("cf_max_retries", 2)
    cfg.setdefault("cf_wait_sec", 20)
    cfg.setdefault("turnstile_before_email_sec", 8)
    cfg.setdefault("timeout_otp", 60)
    cfg.setdefault("batch_count", 1)
    cfg.setdefault(
        "antiflag",
        {
            "enabled": True,
            "clear_cookies_on_start": True,
            "clear_storage_on_start": True,
            "stealth_inject": False,
            "force_ua": False,
            "human_typing": False,
            "pre_click_jiggle": False,
            "isolate_profile": True,
            "align_tz_to_ip": True,
            "hide_webdriver": True,
            "browser_preferences": True,
        },
    )
    if isinstance(cfg.get("antiflag"), dict):
        cfg["antiflag"].setdefault("clear_cookies_on_start", True)
        cfg["antiflag"].setdefault("clear_storage_on_start", True)
        cfg["antiflag"].setdefault("isolate_profile", False)
        cfg["antiflag"].setdefault("align_tz_to_ip", True)
    cfg.setdefault("castle_warmup_sec", 3)
    cfg.setdefault("castle_wait_token_sec", 10)
    cfg.setdefault("castle_retry_on_mint_fail", 1)
    cfg.setdefault("proxy", "")
    cfg.setdefault("headless", False)
    if str(os.environ.get("GROK_NO_FOCUS") or "").strip().lower() in ("1", "true", "yes"):
        cfg["chrome_steal_focus"] = False
        cfg["chrome_window_mode"] = "offscreen"
        cfg["chrome_window_position"] = str(cfg.get("chrome_window_position") or "-2400,40")
        cfg["chrome_background"] = True
    cfg.setdefault("chrome_steal_focus", False)
    cfg.setdefault("chrome_window_mode", "offscreen")
    cfg.setdefault("chrome_window_position", "-2400,40")
    cfg.setdefault("password_length", 14)
    cfg.setdefault("save_file", "data/accounts.txt")
    cfg.setdefault("chrome_user_data_dir", "chrome_profile")
    cfg.setdefault("chrome_debug_port", 9333)
    cfg["fresh_profile_per_account"] = bool(cfg.get("fresh_profile_per_account", True))
    cfg["reuse_chrome_profile"] = bool(cfg.get("reuse_chrome_profile", False))
    if cfg["fresh_profile_per_account"]:
        cfg["reuse_chrome_profile"] = False
    cfg.setdefault("rate_limit_cooldown_min", 55)
    cfg.setdefault("open_grok_after_success", False)
    cfg.setdefault("grok_url", "https://grok.com/")
    cfg.setdefault(
        "sub2api",
        {
            "enabled": True,
            "mode": "auto",
            "sub2api_url": "http://localhost:8080",
            "sub2api_user": "",
            "sub2api_pass": "",
            "sub2api_api_token": "",
            "name_prefix": "grok free",
            "start_number": 1,
            "group": "grok free",
            "group_ids": [],
            "model_test": "Grok 4.5",
            "timeout_oauth_sec": 180,
            "timeout_sec": 180,
            "timeout_test_sec": 120,
            "run_test": False,
            "fallback_browser_oauth": True,
            "durable_retry": True,
            "durable_interval_sec": 60,
        },
    )
    _s2 = cfg.setdefault("sub2api", {})
    _s2.setdefault("mode", "auto")
    _s2.setdefault("sub2api_api_token", "")
    _s2.setdefault("group_ids", [])
    _s2.setdefault("timeout_sec", 180)
    _s2.setdefault("fallback_browser_oauth", True)
    _s2.setdefault("durable_retry", True)
    _s2.setdefault("durable_interval_sec", 60)
    _s2.setdefault("run_test", False)
    _s2.setdefault("name_include_email", False)
    _s2.setdefault("refresh_usage_after_import", True)
    _s2.setdefault("usage_refresh_sec", 20)
    cfg.setdefault(
        "mail_api",
        {
            "enabled": True,
            "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
            "poll_interval": 2,
            "timeout": 60,
            "otp_regex": r"\b(\d{6})\b",
            "providers": [
                {"name": "mailgen", "enabled": True, "base_url": "https://mailgen.shop"},
                {"name": "mailtm_style", "enabled": False, "base_url": ""},
                {
                    "name": "generic_graph",
                    "enabled": True,
                    "base_url": "https://mailgen.shop",
                },
            ],
        },
    )
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
