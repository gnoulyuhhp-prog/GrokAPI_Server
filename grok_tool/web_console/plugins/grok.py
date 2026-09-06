"""Grok xAI registration plugin — wraps existing main.py."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .base import BaseToolPlugin, FieldOption, ToolField, ToolMeta


class GrokToolPlugin(BaseToolPlugin):
    meta = ToolMeta(
        id="grok",
        name="Grok / xAI",
        description="Đăng ký Grok — SSO → Sub2API · Google Sheet · ESC stop",
        icon="⚡",
        status="ready",
        color="#111111",
        fields=[
            ToolField(
                key="mail",
                label="Loại email",
                type="select",
                default="0",
                options=[
                    FieldOption("0", "Temp SMART", "azpop ↔ tmail failover"),
                    FieldOption("1", "Hotmail", "1 acc → tối đa 5 Grok (mail / mail+1 … +4)"),
                    FieldOption("2", "Temp azpop only", ""),
                    FieldOption("3", "Temp tmail.wibu only", ""),
                    FieldOption("4", "Temp Racing", "Đua TinyHost / Lol / VIP lấy nhanh nhất"),
                    FieldOption("5", "TinyHost", "tinyhost.shop"),
                    FieldOption("6", "TempMail.lol", ""),
                ],
            ),
            ToolField(
                key="count",
                label="Số lượng",
                type="number",
                default=1,
                min=0,
                max=1000000,
                hint="0 = chạy liên tục đến khi Stop",
            ),
            ToolField(
                key="threads",
                label="Số luồng song song",
                type="select",
                default="5",
                options=[
                    FieldOption("1", "1 luồng", "ổn định / nhẹ máy"),
                    FieldOption("3", "3 luồng", "cân bằng"),
                    FieldOption("5", "5 luồng", "khuyến nghị"),
                    FieldOption("10", "10 luồng", "nhanh / tốn RAM"),
                    FieldOption("12", "12 luồng", "tải cao"),
                ],
                hint="Luồng thật cho HTTP không Chrome · Số lượng phải ≥ số luồng",
            ),
            ToolField(
                key="sub2api",
                label="Auto Sub2API",
                type="checkbox",
                default=True,
                hint="Import SSO/OAuth sau khi reg OK",
            ),
            ToolField(
                key="backend",
                label="Cách reg",
                type="select",
                default="github",
                options=[
                    FieldOption("github", "HTTP không Chrome", "solver :5072"),
                    FieldOption("protocol", "HTTP + Castle", "Chrome lấy token"),
                    FieldOption("auto", "Tự động", "HTTP rồi Chrome nếu fail"),
                    FieldOption("browser", "Chrome ẩn", ""),
                ],
            ),
            ToolField(
                key="hide_chrome",
                label="Ẩn Chrome / không cướp màn hình",
                type="checkbox",
                default=True,
                hint="Cửa sổ reg chạy ngoài màn hình, không nhảy ra trước mặt",
            ),
        ],
    )

    def _py(self, root: Path) -> Path:
        from grokreg.core import winhide

        return winhide.hidden_python(root)

    @staticmethod
    def _is_hotmail_mail(mail: str) -> bool:
        n = (mail or "").strip().lower().replace(" ", "")
        return n in ("1", "hotmail", "outlook", "ms", "microsoft")

    def preflight(self, params: dict[str, Any], root: Path) -> None:
        backend = str(params.get("backend") or "github").strip().lower()
        if backend in ("protocol", "auto", "http", "pure_http", "github", "castle"):
            try:
                if str(root) not in sys.path:
                    sys.path.insert(0, str(root))
                from grokreg.core.config import load_config
                from services.solver_manager import ensure_started, get_status

                cfg = load_config()
                try:
                    threads = max(1, min(12, int(params.get("threads") or 1)))
                except (TypeError, ValueError):
                    threads = 1
                st = get_status(
                    str((cfg.get("turnstile") or {}).get("solver_url") or "") or None
                )
                if not st.get("online") or st.get("threads") != threads:
                    st = ensure_started(cfg, thread=threads)
                if not st.get("online") and st.get("provider") != "yescaptcha":
                    detail = str(st.get("last_error") or st.get("message") or "")
                    raise RuntimeError(
                        "Turnstile solver :5072 không khởi động được"
                        + (f": {detail}" if detail else "")
                    )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"Không kiểm tra được Turnstile solver: {exc}") from exc
        if not self._is_hotmail_mail(str(params.get("mail") or "0")):
            return
        pool = self.hotmail_pool(root)
        slots = int(pool.get("slots") or pool.get("count") or 0)
        if slots <= 0:
            raise RuntimeError("Pool Hotmail trống / hết slot alias — import acc rồi Start")

    def build_command(self, params: dict[str, Any], root: Path) -> list[str]:
        py = self._py(root)
        if not py.exists():
            raise RuntimeError(f"Python venv not found: {py}")
        mail = str(params.get("mail") or "0")
        if self._is_hotmail_mail(mail):
            pool = self.hotmail_pool(root)
            count = int(pool.get("slots") or pool.get("count") or 0)
            if count <= 0:
                raise RuntimeError("Pool Hotmail trống / hết slot alias — import acc trước khi Start")
            count = max(0, count)
        else:
            count = int(params.get("count") if params.get("count") is not None else 1)
            count = max(0, count)
        backend = str(params.get("backend") or "github").strip().lower()
        if backend not in ("github", "protocol", "auto", "browser"):
            backend = "github"
        # Protocol/GitHub + temp mail: azpop nhận được OTP xAI, tmail thì không.
        if backend in (
            "github",
            "protocol",
            "auto",
            "http",
            "pure_http",
            "castle",
        ) and not self._is_hotmail_mail(mail):
            mail = "2"
        try:
            threads = max(1, min(12, int(params.get("threads") or 1)))
        except (TypeError, ValueError):
            threads = 1
        return [
            str(py),
            "-u",
            "main.py",
            mail,
            "--count",
            str(count),
            "--backend",
            backend,
            "--threads",
            str(threads),
        ]

    def cwd(self, root: Path) -> Path:
        return root

    def env_overrides(self, params: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        try:
            env["GROK_THREADS"] = str(max(1, min(12, int(params.get("threads") or 1))))
        except (TypeError, ValueError):
            env["GROK_THREADS"] = "1"
        if params.get("sub2api") is False or str(params.get("sub2api")).lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            env["GROK_SUB2API"] = "0"
        hide = params.get("hide_chrome", True)
        if hide is False or str(hide).lower() in ("0", "false", "no", "off"):
            env["GROK_NO_FOCUS"] = "0"
        else:
            env["GROK_NO_FOCUS"] = "1"
        return env

    def stop_signal(self, root: Path) -> None:
        stop = root / "data" / "STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop:web\n", encoding="utf-8")
        try:
            from stop_control import request_stop

            request_stop("web", write_file=True)
        except Exception:
            pass

    @staticmethod
    def _classify(status: str) -> str:
        """Bucket status for stats/UI."""
        st = (status or "").strip()
        sl = st.lower()
        if sl.startswith("added_sub2api"):
            return "sub2api_ok"
        if sl.startswith("success_sub2api"):
            return "reg_ok_sub2_fail"
        if sl == "success" or sl.startswith("success_not_logged"):
            return "reg_ok"
        if sl in ("manual_check", "manual_finish", "stopped"):
            return "pending"
        if sl.startswith("error") or sl:
            return "fail"
        return "other"

    def parse_results(self, root: Path, limit: int = 200) -> list[dict[str, Any]]:
        path = root / "data" / "accounts.txt"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            email = parts[0].strip() if parts else ""
            password = parts[1].strip() if len(parts) > 1 else ""
            status = parts[2].strip() if len(parts) > 2 else ""
            kind = self._classify(status)
            ok = kind in ("sub2api_ok", "reg_ok", "reg_ok_sub2_fail")
            rows.append(
                {
                    "email": email,
                    "password": password,
                    "status": status,
                    "kind": kind,
                    "ok": ok,
                    "tool": "grok",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _iter_all_rows(self, root: Path) -> list[dict[str, Any]]:
        """All ledger lines oldest→newest (not reversed/limit)."""
        path = root / "data" / "accounts.txt"
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            email = parts[0].strip() if parts else ""
            password = parts[1].strip() if len(parts) > 1 else ""
            status = parts[2].strip() if len(parts) > 2 else ""
            kind = self._classify(status)
            out.append(
                {
                    "email": email,
                    "password": password,
                    "status": status,
                    "kind": kind,
                    "ok": kind in ("sub2api_ok", "reg_ok", "reg_ok_sub2_fail"),
                }
            )
        return out

    def stats(self, root: Path) -> dict[str, Any]:
        """
        Clear KPIs (not “weird” success=fail dump):

        - attempts: every line in accounts.txt (mỗi lần thử)
        - unique_emails: số email khác nhau
        - reg_ok: reg xong (success / sub2 ok / sub2 fail nhưng đã reg)
        - sub2api: đã add Sub2API (added_sub2api*)
        - reg_only: reg OK nhưng chưa/không Sub2API
        - sub2_fail: reg OK + Sub2API fail
        - fail: error*
        - pending: manual_check / stopped
        """
        all_rows = self._iter_all_rows(root)
        attempts = len(all_rows)

        # latest status per email (last line wins)
        latest: dict[str, dict[str, Any]] = {}
        for r in all_rows:
            key = (r.get("email") or "").strip().lower()
            if key:
                latest[key] = r

        def count_kind(rows: list[dict[str, Any]], kind: str) -> int:
            return sum(1 for r in rows if r.get("kind") == kind)

        latest_list = list(latest.values())
        # Prefer unique-email latest for “real” account counts
        sub2api = count_kind(latest_list, "sub2api_ok")
        reg_only = count_kind(latest_list, "reg_ok")
        sub2_fail = count_kind(latest_list, "reg_ok_sub2_fail")
        fail = count_kind(latest_list, "fail")
        pending = count_kind(latest_list, "pending")
        reg_ok = sub2api + reg_only + sub2_fail

        # attempt-level (raw lines) for debugging / rate
        att_sub2 = count_kind(all_rows, "sub2api_ok")
        att_reg_ok = sum(1 for r in all_rows if r.get("ok"))
        att_fail = count_kind(all_rows, "fail")

        hotmails = 0
        hp = root / "data" / "hotmails.txt"
        if hp.exists():
            try:
                from grokreg.core.config import load_config
                from grokreg.mail.providers import HotmailProvider

                slots, lines = HotmailProvider.from_config(hp, load_config()).available_count()
                hotmails = slots or lines
            except Exception:
                for ln in hp.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if ln.strip() and not ln.strip().startswith("#"):
                        hotmails += 1
        counter = 0
        cp = root / "data" / "sub2api_name_counter.json"
        if cp.exists():
            try:
                counter = int(json.loads(cp.read_text(encoding="utf-8")).get("next") or 0)
            except Exception:
                pass

        return {
            # primary KPIs (unique email, last status) — UI shows these
            "total": len(latest),  # unique emails
            "success": reg_ok,  # reg OK (any form)
            "fail": fail,
            "sub2api": sub2api,
            "reg_only": reg_only,
            "sub2_fail": sub2_fail,
            "pending": pending,
            "unique_emails": len(latest),
            # raw ledger
            "attempts": attempts,
            "attempts_success": att_reg_ok,
            "attempts_fail": att_fail,
            "attempts_sub2api": att_sub2,
            "hotmails": hotmails,
            "next_name": f"grok free {max(1, counter):03d}" if counter else "grok free 001",
            # human blurb
            "blurb": (
                f"{len(latest)} email · {reg_ok} reg OK "
                f"({sub2api} Sub2API · {reg_only} reg-only · {sub2_fail} sub2 fail) · "
                f"{fail} fail · {attempts} lượt thử"
            ),
        }

    def _hotmail_path(self, root: Path) -> Path:
        try:
            from grokreg.core.config import load_config

            rel = str(load_config().get("hotmail_list") or "data/hotmails.txt")
        except Exception:
            rel = "data/hotmails.txt"
        p = Path(rel)
        return p if p.is_absolute() else (root / p)

    def hotmail_pool(self, root: Path) -> dict[str, Any]:
        from grokreg.mail.hotmail_import import parse_hotmail_text

        path = self._hotmail_path(root)
        raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        parsed = parse_hotmail_text(raw)
        accounts: list[dict[str, Any]] = []
        for rec in parsed["rows"]:
            accounts.append(
                {
                    "email": rec["email"],
                    "has_password": bool(rec.get("password")),
                    "has_refresh": bool(rec.get("refresh")),
                    "has_client_id": bool(rec.get("client_id")),
                }
            )
        slots = len(accounts)
        lines = len(accounts)
        max_aliases = 5
        try:
            from grokreg.core.config import load_config
            from grokreg.mail.hotmail_alias import max_aliases_from_config
            from grokreg.mail.providers import HotmailProvider

            cfg = load_config()
            max_aliases = max_aliases_from_config(cfg)
            if path.exists():
                slots, lines = HotmailProvider.from_config(path, cfg).available_count()
        except Exception:
            pass
        return {
            "path": "data/hotmails.txt",
            "count": len(accounts),
            "lines": lines,
            "slots": slots,
            "max_aliases": max_aliases,
            "accounts": accounts[:200],
        }

    def import_hotmails(
        self, root: Path, text: str, mode: str = "append"
    ) -> dict[str, Any]:
        from grokreg.mail.hotmail_import import format_line, parse_hotmail_text

        parsed = parse_hotmail_text(text)
        if not parsed["rows"] and not parsed["errors"]:
            raise ValueError("Không thấy dòng Hotmail nào")
        if not parsed["rows"]:
            sample = parsed["errors"][:3]
            hint = "; ".join(
                f"dòng {e['line']}: {e.get('text') or e.get('reason')}" for e in sample
            )
            raise ValueError(
                f"Không parse được dòng hợp lệ ({parsed['invalid']} lỗi). "
                "Dùng email|password|refresh|client_id hoặc email:password. "
                f"Ví dụ lỗi: {hint}"
            )

        path = self._hotmail_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_raw = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        existing = parse_hotmail_text(existing_raw)
        have = {r["email"].lower(): r for r in existing["rows"]}

        mode_n = (mode or "append").strip().lower()
        added = 0
        updated = 0
        skipped = 0
        if mode_n == "replace":
            have = {}
        for rec in parsed["rows"]:
            key = rec["email"].lower()
            if key in have and mode_n != "replace":
                skipped += 1
                continue
            if key in have:
                updated += 1
            else:
                added += 1
            have[key] = rec

        lines = [
            format_line(r["email"], r["password"], r["refresh"], r["client_id"])
            for r in have.values()
        ]
        path.write_text(("\n".join(lines) + ("\n" if lines else "")), encoding="utf-8")
        pool = self.hotmail_pool(root)
        pool.update(
            {
                "ok": True,
                "added": added,
                "updated": updated,
                "skipped": skipped,
                "invalid": parsed["invalid"],
                "errors": parsed["errors"],
                "mode": "replace" if mode_n == "replace" else "append",
            }
        )
        return pool
