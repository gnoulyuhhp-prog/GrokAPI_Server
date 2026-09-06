"""Parse Hotmail / Outlook lines pasted from the web UI or a text file."""
from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_PREFIX_RE = re.compile(r"^(?:[-*•]|\d+[.)\]])\s*")


def _clean_cell(value: str) -> str:
    s = (value or "").strip().strip("\"'“”‘’`").strip()
    if s.lower().startswith("mailto:"):
        s = s[7:].strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1].strip()
    return s


def is_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(_clean_cell(value)))


def is_guid(value: str) -> bool:
    return bool(_GUID_RE.match((value or "").strip()))


def split_line(line: str) -> list[str]:
    raw = (line or "").strip()
    if not raw:
        return []
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff").strip()
    raw = _PREFIX_RE.sub("", raw).strip()
    if "----" in raw:
        parts = [p.strip() for p in raw.split("----")]
    elif "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
    elif "\t" in raw:
        parts = [p.strip() for p in raw.split("\t")]
    elif ";" in raw and "@" in raw.split(";", 1)[0]:
        parts = [p.strip() for p in raw.split(";", 3)]
    elif raw.count(":") >= 1 and is_email(raw.split(":", 1)[0]):
        parts = [p.strip() for p in raw.split(":", 3)]
    elif "," in raw and is_email(raw.split(",", 1)[0].strip().strip("\"'")):
        parts = [p.strip() for p in raw.split(",")]
    elif " / " in raw and is_email(raw.split(" / ", 1)[0]):
        parts = [p.strip() for p in raw.split(" / ")]
    elif re.search(r"\s{2,}", raw) and is_email(re.split(r"\s{2,}", raw, maxsplit=1)[0]):
        parts = [p.strip() for p in re.split(r"\s{2,}", raw)]
    elif " " in raw and is_email(raw.split(" ", 1)[0]):
        head, tail = raw.split(" ", 1)
        parts = [head, tail.strip()]
    else:
        found = _EMAIL_FIND_RE.search(raw)
        if found and not is_email(raw):
            email = found.group(0)
            rest = (raw[: found.start()] + " " + raw[found.end() :]).strip()
            parts = [email] + ([rest] if rest else [])
        else:
            parts = [raw]
    return [_clean_cell(p) for p in parts if _clean_cell(p) or p == parts[0]]


def _looks_token(value: str) -> bool:
    s = (value or "").strip()
    return len(s) >= 40 and not is_guid(s)


def normalize_parts(parts: list[str]) -> dict[str, str] | None:
    """
    Accept:
      email|password|refresh|client_id     (grok_tool)
      email|password|refresh
      email|password
      email:password[:refresh[:client_id]]
      email;password;...
      email----password----client_id----refresh   (register-web)
    """
    parts = [_clean_cell(p) for p in parts]
    while parts and parts[-1] == "":
        parts.pop()
    if len(parts) < 1 or not is_email(parts[0]):
        return None
    email = parts[0]
    password = parts[1] if len(parts) > 1 else ""
    third = parts[2] if len(parts) > 2 else ""
    fourth = parts[3] if len(parts) > 3 else ""
    refresh = ""
    client_id = ""
    if fourth or third:
        if is_guid(third) and (not fourth or _looks_token(fourth) or not is_guid(fourth)):
            client_id, refresh = third, fourth
        elif is_guid(fourth):
            refresh, client_id = third, fourth
        elif _looks_token(third):
            refresh, client_id = third, fourth
        else:
            refresh, client_id = third, fourth
    return {
        "email": email,
        "password": password,
        "refresh": refresh,
        "client_id": client_id,
    }


def format_line(email: str, password: str = "", refresh: str = "", client_id: str = "") -> str:
    return f"{email.strip()}|{password}|{refresh}|{client_id}".rstrip("|")


def parse_hotmail_text(text: str) -> dict[str, Any]:
    """Parse a paste / file dump. Returns {ok, invalid, rows, errors}."""
    rows: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, raw in enumerate((text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        parts = split_line(line)
        rec = normalize_parts(parts)
        if not rec:
            errors.append({"line": i, "text": line[:80], "reason": "Thiếu email hợp lệ"})
            continue
        key = rec["email"].lower()
        if key in seen:
            errors.append({"line": i, "text": rec["email"], "reason": "Trùng trong bản dán"})
            continue
        seen.add(key)
        rec["raw"] = format_line(rec["email"], rec["password"], rec["refresh"], rec["client_id"])
        rows.append(rec)
    return {
        "ok": len(rows),
        "invalid": len(errors),
        "rows": rows,
        "errors": errors[:30],
    }
