"""Microsoft plus-aliases: 1 Hotmail → many Grok signups.

Index 0 uses the mailbox as-is. Index N>0 is ``local+N@domain``.
OTP still arrives in the same Outlook inbox (Graph / IMAP on the mailbox).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def clamp_max_aliases(value: Any, default: int = 5) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(20, n))


def max_aliases_from_config(config: dict[str, Any] | None) -> int:
    cfg = config or {}
    raw = cfg.get("hotmail_max_aliases")
    if raw is None:
        block = cfg.get("hotmail")
        if isinstance(block, dict):
            raw = block.get("max_aliases_per_account", block.get("max_aliases"))
    return clamp_max_aliases(raw if raw is not None else 5)


def split_email(email: str) -> tuple[str, str]:
    s = (email or "").strip()
    if "@" not in s:
        return s, ""
    local, _, domain = s.partition("@")
    return local, domain


def make_plus_alias(mailbox: str, index: int) -> str:
    """Index 0 = exact mailbox. Index N = local+N@domain (register-web)."""
    mailbox = (mailbox or "").strip()
    if int(index) <= 0 or "@" not in mailbox:
        return mailbox
    local, domain = split_email(mailbox)
    return f"{local}+{int(index)}@{domain}"


def mailbox_from_alias(email: str) -> str:
    """Best-effort: user+3@d.com → user@d.com. Index 0 stays unchanged."""
    email = (email or "").strip()
    local, domain = split_email(email)
    if not domain or "+" not in local:
        return email
    base, _, tag = local.rpartition("+")
    if base and tag.isdigit():
        return f"{base}@{domain}"
    return email


def alias_index_of(email: str, mailbox: str) -> int:
    email_l = (email or "").strip().lower()
    mailbox_l = (mailbox or "").strip().lower()
    if not email_l or not mailbox_l:
        return 0
    if email_l == mailbox_l:
        return 0
    local, domain = split_email(email_l)
    m_local, m_domain = split_email(mailbox_l)
    if domain != m_domain:
        return 0
    prefix = m_local + "+"
    if local.startswith(prefix):
        tag = local[len(prefix) :]
        if tag.isdigit():
            return int(tag)
    return 0


def alias_matches_mailbox(email: str, mailbox: str, max_aliases: int = 20) -> bool:
    email_l = (email or "").strip().lower()
    mailbox_l = (mailbox or "").strip().lower()
    if not email_l or not mailbox_l:
        return False
    if email_l == mailbox_l:
        return True
    for i in range(max_aliases):
        if make_plus_alias(mailbox_l, i).lower() == email_l:
            return True
    return False


def default_ledger_path(list_path: Path) -> Path:
    return list_path.with_name("hotmail_aliases.json")


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def used_indices(ledger: dict[str, Any], mailbox: str) -> list[int]:
    rec = ledger.get((mailbox or "").strip().lower())
    if not isinstance(rec, dict):
        return []
    raw = rec.get("used") or []
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if n < 0 or n in seen:
            continue
        seen.add(n)
        out.append(n)
    out.sort()
    return out


def next_free_index(ledger: dict[str, Any], mailbox: str, max_aliases: int) -> int | None:
    used = set(used_indices(ledger, mailbox))
    for i in range(max(1, int(max_aliases))):
        if i not in used:
            return i
    return None


def remaining_slots(ledger: dict[str, Any], mailbox: str, max_aliases: int) -> int:
    used = used_indices(ledger, mailbox)
    return max(0, int(max_aliases) - len(used))


def mark_index_used(path: Path, mailbox: str, index: int, max_aliases: int) -> dict[str, Any]:
    """Persist alias index as consumed. Returns the mailbox record."""
    key = (mailbox or "").strip().lower()
    with _LOCK:
        data = load_ledger(path)
        rec = data.get(key)
        if not isinstance(rec, dict):
            rec = {}
        raw_used = rec.get("used") or []
        used: list[int] = []
        seen: set[int] = set()
        for item in list(raw_used) + [index]:
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if n < 0 or n in seen:
                continue
            seen.add(n)
            used.append(n)
        used.sort()
        rec = {
            "mailbox": mailbox,
            "used": used,
            "updated_at": int(time.time()),
        }
        data[key] = rec
        save_ledger(path, data)
        rec = dict(rec)
        rec["exhausted"] = len(used) >= max(1, int(max_aliases))
        rec["remaining"] = max(0, int(max_aliases) - len(used))
        return rec
