"""Canva plugin — sibling folder ../canva/main.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseToolPlugin, FieldOption, ToolField, ToolMeta
from .grok import GrokToolPlugin


class CanvaToolPlugin(BaseToolPlugin):
    meta = ToolMeta(
        id="canva",
        name="Canva",
        description="Reg Canva — Hotmail ×5 alias + redeem mã",
        icon="C",
        status="ready",
        color="#00C4CC",
        fields=[
            ToolField(
                key="job",
                label="Việc",
                type="select",
                default="reg",
                options=[
                    FieldOption("reg", "Reg acc", "Continue with email → OTP"),
                    FieldOption("redeem", "Redeem mã", "Login → canva.com/redeem → Dùng thử ngay"),
                ],
            ),
            ToolField(
                key="mail",
                label="Loại email",
                type="select",
                default="1",
                options=[
                    FieldOption("1", "Hotmail", "1 mail = tối đa 5 acc (+1…+4), OTP về hộp gốc"),
                    FieldOption("3", "Temp tmail.wibu", "tmail.wibucrypto.pro"),
                    FieldOption("2", "Temp Azpop", "dễ bị INELIGIBLE_EMAIL"),
                    FieldOption("0", "Temp SMART", "azpop ↔ tmail"),
                    FieldOption("4", "Temp Guerrilla", "dễ bị chặn"),
                ],
            ),
            ToolField(
                key="count",
                label="Số lượng",
                type="number",
                default=1,
                min=0,
                max=1000000,
                hint="0 = chạy đến khi Stop",
            ),
            ToolField(
                key="backend",
                label="Cách reg",
                type="select",
                default="browser",
                options=[
                    FieldOption("browser", "Chrome ẩn", "nên dùng — OTP + alias như Grok"),
                    FieldOption("auto", "HTTP rồi Chrome", "POST 400 thì tự mở Chrome"),
                    FieldOption("protocol", "HTTP rồi Chrome", "giống auto — Canva không cho HTTP-only"),
                ],
            ),
            ToolField(
                key="codes",
                label="Mã redeem",
                type="textarea",
                default="",
                hint="Dán mã vào đây — mỗi dòng 1 mã (hoặc cách nhau bởi dấu phẩy). Ví dụ CANVASPIDERMAN",
            ),
            ToolField(
                key="threads",
                label="Threads redeem",
                type="number",
                default=3,
                min=1,
                max=8,
                hint="Chrome ẩn song song — chỉ dùng khi Việc = Redeem",
            ),
        ],
    )

    @staticmethod
    def canva_root(root: Path) -> Path:
        return root.parent / "canva"

    def _py(self, root: Path) -> Path:
        from grokreg.core import winhide

        return winhide.hidden_python(root)

    @staticmethod
    def _is_hotmail_mail(mail: str) -> bool:
        return GrokToolPlugin._is_hotmail_mail(mail)

    def preflight(self, params: dict[str, Any], root: Path) -> None:
        cv = self.canva_root(root)
        if not (cv / "main.py").exists():
            raise RuntimeError(f"Thiếu tool Canva: {cv}")
        if str(params.get("job") or "reg") == "redeem":
            written = self._write_codes(cv, str(params.get("codes") or ""))
            if written <= 0:
                raise RuntimeError("Thiếu mã redeem — dán mã vào ô Mã redeem (mỗi dòng 1 mã)")
            accs = cv / "data" / "accounts.txt"
            if not accs.exists():
                raise RuntimeError("Thiếu data/accounts.txt — reg acc trước")
            return
        if not self._is_hotmail_mail(str(params.get("mail") or "1")):
            return
        pool = self.hotmail_pool(root)
        slots = int(pool.get("slots") or pool.get("count") or 0)
        if slots <= 0:
            raise RuntimeError("Pool Hotmail trống / hết slot alias — import acc rồi Start")

    def build_command(self, params: dict[str, Any], root: Path) -> list[str]:
        py = self._py(root)
        if not py.exists():
            raise RuntimeError(f"Python venv not found: {py}")
        if str(params.get("job") or "reg") == "redeem":
            threads = int(params.get("threads") if params.get("threads") is not None else 3)
            threads = max(1, min(8, threads))
            self._write_codes(self.canva_root(root), str(params.get("codes") or ""))
            codes = "data/codes_web.txt"
            return [
                str(py),
                "-u",
                "canva_tool.py",
                "redeem",
                "--accounts",
                "data/accounts.txt",
                "--codes",
                codes,
                "--threads",
                str(threads),
                "--output",
                "data/proof.json",
                "--success-only",
            ]
        mail = str(params.get("mail") or "1")
        if self._is_hotmail_mail(mail):
            pool = self.hotmail_pool(root)
            count = int(pool.get("slots") or pool.get("count") or 0)
            if count <= 0:
                raise RuntimeError("Pool Hotmail trống — import acc trước khi Start")
            count = max(0, count)
        else:
            if mail not in ("0", "2", "3", "4"):
                mail = "1"
            count = int(params.get("count") if params.get("count") is not None else 1)
            count = max(0, count)
        backend = str(params.get("backend") or "auto").strip().lower()
        if backend not in ("protocol", "auto", "browser"):
            backend = "auto"
        return [
            str(py),
            "-u",
            "main.py",
            mail,
            "--count",
            str(count),
            "--backend",
            backend,
        ]

    def cwd(self, root: Path) -> Path:
        return self.canva_root(root)

    def stop_signal(self, root: Path) -> None:
        stop = self.canva_root(root) / "data" / "STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop:web\n", encoding="utf-8")
        try:
            import sys

            cv = str(self.canva_root(root))
            if cv not in sys.path:
                sys.path.insert(0, cv)
            from canreg.stop import request_stop

            request_stop("web", write_file=True)
        except Exception:
            pass

    @staticmethod
    def _write_codes(cv: Path, raw: str) -> int:
        """Ghi mã từ ô web → data/codes_web.txt. Chấp nhận xuống dòng hoặc dấu phẩy."""
        bits: list[str] = []
        seen: set[str] = set()
        blob = (raw or "").replace(",", "\n").replace(";", "\n")
        for ln in blob.splitlines():
            code = ln.strip()
            if not code or code.startswith("#"):
                continue
            code = code.split()[0]
            key = code.upper()
            if key in seen:
                continue
            seen.add(key)
            bits.append(code)
        dest = cv / "data" / "codes_web.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(("\n".join(bits) + ("\n" if bits else "")), encoding="utf-8")
        return len(bits)

    def hotmail_pool(self, root: Path) -> dict[str, Any]:
        pool = GrokToolPlugin().hotmail_pool(root)
        cv = self.canva_root(root)
        max_a = 1
        try:
            import json

            cfg = json.loads((cv / "config.json").read_text(encoding="utf-8"))
            max_a = int(cfg.get("hotmail_max_aliases") or 1)
        except Exception:
            max_a = 1
        pool["max_aliases"] = max(1, max_a)
        try:
            from grokreg.core.config import load_config
            from grokreg.mail.providers import HotmailProvider

            path = GrokToolPlugin()._hotmail_path(root)
            merged = dict(load_config())
            merged["hotmail_max_aliases"] = pool["max_aliases"]
            if path.exists():
                slots, lines = HotmailProvider.from_config(path, merged).available_count()
                pool["slots"] = slots
                pool["lines"] = lines
        except Exception:
            pool["slots"] = int(pool.get("count") or 0)
        return pool

    def import_hotmails(self, root: Path, text: str, mode: str = "append") -> dict[str, Any]:
        return GrokToolPlugin().import_hotmails(root, text, mode)

    @staticmethod
    def _classify(status: str) -> str:
        sl = (status or "").strip().lower()
        if sl.startswith("success"):
            return "reg_ok"
        if sl.startswith("stopped") or sl in ("pending", "manual_check"):
            return "pending"
        if sl.startswith("error") or sl:
            return "fail"
        return "other"

    def parse_results(self, root: Path, limit: int = 200) -> list[dict[str, Any]]:
        cv = self.canva_root(root)
        path = cv / "data" / "accounts.txt"
        rows: list[dict[str, Any]] = []
        redeem = cv / "data" / "redeem_success.txt"
        if redeem.exists():
            for line in reversed(redeem.read_text(encoding="utf-8", errors="replace").splitlines()):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split("|")
                status = parts[2].strip() if len(parts) > 2 else ""
                ok = status.upper() == "SUKSES"
                rows.append(
                    {
                        "email": parts[0].strip() if parts else "",
                        "password": parts[1].strip() if len(parts) > 1 else "",
                        "status": f"redeem:{status}",
                        "kind": "reg_ok" if ok else "fail",
                        "ok": ok,
                        "tool": "canva",
                    }
                )
                if len(rows) >= limit:
                    return rows
        if not path.exists():
            return rows
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            status = parts[2].strip() if len(parts) > 2 else ""
            kind = self._classify(status)
            rows.append(
                {
                    "email": parts[0].strip() if parts else "",
                    "password": parts[1].strip() if len(parts) > 1 else "",
                    "status": status,
                    "kind": kind,
                    "ok": kind == "reg_ok",
                    "tool": "canva",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def stats(self, root: Path) -> dict[str, Any]:
        rows = list(reversed(self.parse_results(root, limit=5000)))
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = (r.get("email") or "").strip().lower()
            if key:
                latest[key] = r
        latest_list = list(latest.values())
        ok = sum(1 for r in latest_list if r.get("ok"))
        fail = sum(1 for r in latest_list if r.get("kind") == "fail")
        pending = sum(1 for r in latest_list if r.get("kind") == "pending")
        return {
            "total": len(latest),
            "success": ok,
            "fail": fail,
            "pending": pending,
            "unique_emails": len(latest),
            "attempts": len(rows),
            "sub2api": 0,
            "reg_only": ok,
            "sub2_fail": 0,
            "blurb": f"{len(latest)} email · {ok} reg OK · {fail} fail · {len(rows)} lượt thử",
        }
