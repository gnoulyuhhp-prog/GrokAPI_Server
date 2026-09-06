"""
Grok Register Tool — thin entrypoint (modular package: grokreg/).

Layout:
  grokreg/core/      config, helpers, cleanup, runtime
  grokreg/mail/      OTP, mail_api, temp providers
  grokreg/browser/   Chrome/pydoll, CF, page flow
  grokreg/reg/       register_one pipeline
  grokreg/cli/       argparse + async main
  grokreg/delivery/  Sub2API / sheets (see shims at repo root)

Legacy monolith backup: main.py.monolith.bak
Run:  python main.py 0 --count 1
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root on path when launched from elsewhere
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# --- public re-exports (batch_runner / continue_sub2api / sub2api_oauth import these) ---
from grokreg.core.runtime import (  # noqa: F401
    ROOT,
    DATA_DIR,
    CONFIG_PATH,
    log,
    FIRST_NAMES,
    LAST_NAMES,
    MS_CLIENT_IDS,
    RATE_LIMIT_PATH,
)
from grokreg.core.config import load_config  # noqa: F401
from grokreg.core.cleanup import kill_old_runs  # noqa: F401
from grokreg.core.helpers import (  # noqa: F401
    extract_otp,
    normalize_otp_for_input,
    random_password,
    random_string,
    random_name,
    resolve_password,
    save_account,
)
from grokreg.mail.mail_api import EmailSession, MailApiClient  # noqa: F401
from grokreg.mail.providers import (  # noqa: F401
    AzpopMailProvider,
    HotmailProvider,
    MailTmProvider,
    wait_otp_smart,
)
from grokreg.browser.chrome import (  # noqa: F401
    BrowserHandle,
    build_chrome_options,
    open_or_attach_browser,
    close_browser_handle,
    navigate_signup_with_cf,
    click_turnstile_checkbox_robust,
    force_click_cloudflare_checkbox,
)
from grokreg.browser.jsutil import _exec_js, _unwrap_js_result  # noqa: F401
from grokreg.browser.page_flow import (  # noqa: F401
    click_button_by_text,
    type_into,
    fill_otp_on_page,
    dismiss_cookie_banner,
    page_is_logged_in,
    detect_page_error,
    ensure_logged_in_landing,
    login_with_credentials,
)
from grokreg.reg.flow import (  # noqa: F401
    register_one,
    acquire_email_session,
    push_results_to_gsheet,
)
from grokreg.cli.app import (  # noqa: F401
    build_arg_parser,
    main,
    parse_provider_choice,
    pick_email_provider,
)


if __name__ == "__main__":
    asyncio.run(main())
