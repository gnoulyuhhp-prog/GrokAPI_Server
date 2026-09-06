"""
Central paths — keeps runtime data out of the project root.
ROOT = tool folder (next to main.py)
DATA = ROOT / "data"  (accounts ledger, stats, hotmails, counters)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SCRIPTS = ROOT / "scripts"
PROFILES = ROOT  # chrome_profile_* stay under ROOT for Chrome path simplicity


def ensure_dirs() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    SCRIPTS.mkdir(parents=True, exist_ok=True)


def data_file(name: str) -> Path:
    """Resolve a data file under data/ (creates data/ if needed)."""
    ensure_dirs()
    p = Path(name)
    if p.is_absolute():
        return p
    # already data/... in name
    if str(name).replace("\\", "/").startswith("data/"):
        return ROOT / name
    return DATA / name


# Common data files (internal ledger / stats — user checks Google Sheet, not these)
ACCOUNTS = lambda: data_file("accounts.txt")  # noqa: E731
HOTMAILS = lambda: data_file("hotmails.txt")  # noqa: E731
HOTMAILS_USED = lambda: data_file("hotmails_used.txt")  # noqa: E731
VPN_BY_EMAIL = lambda: data_file("vpn_by_email.json")  # noqa: E731
SUB2API_COUNTER = lambda: data_file("sub2api_name_counter.json")  # noqa: E731
RATE_LIMITS = lambda: data_file("rate_limits.json")  # noqa: E731
RECENT_NAMES = lambda: data_file("recent_names.json")  # noqa: E731
MAIL_FAIL_COOLDOWN = lambda: data_file("mail_fail_cooldown.json")  # noqa: E731
AZPOP_DOMAIN_STATS = lambda: data_file("azpop_domain_stats.json")  # noqa: E731
AZPOP_DOMAIN_RECENT = lambda: data_file("azpop_domain_recent.json")  # noqa: E731
TEMP_PROVIDER_STATS = lambda: data_file("temp_provider_stats.json")  # noqa: E731
GSHEET_LAST_PAYLOAD = lambda: data_file("gsheet_last_payload.json")  # noqa: E731
CONFIG = ROOT / "config.json"
