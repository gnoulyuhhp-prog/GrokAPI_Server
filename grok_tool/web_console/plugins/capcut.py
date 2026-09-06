"""CapCut plugin — sibling folder ../capcut/main.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseToolPlugin, FieldOption, ToolField, ToolMeta
from .grok import GrokToolPlugin


class CapcutToolPlugin(BaseToolPlugin):
    meta = ToolMeta(
        id="capcut",
        name="CapCut",
        description="Đăng ký CapCut — HTTP + check ưu đãi Pro/trial",
        icon="✂",
        status="ready",
        color="#00C8D2",
        fields=[
            ToolField(
                key="mail",
                label="Loại email",
                type="select",
                default="4",
                options=[
                    FieldOption("4", "Temp Guerrilla", "CapCut gửi OTP được"),
                    FieldOption("2", "Temp Azpop", "thường không nhận mail CapCut"),
                    FieldOption("1", "Hotmail", "pool chung với Grok"),
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
                default="protocol",
                options=[
                    FieldOption("protocol", "HTTP không Chrome", "Passport + nhận ưu đãi"),
                ],
            ),
            ToolField(
                key="invite",
                label="Mã invite / redeem",
                type="text",
                default="",
                hint="Để trống nếu không có",
            ),
            ToolField(
                key="claim_offer",
                label="Nhận + check ưu đãi sau reg",
                type="checkbox",
                default=True,
                hint="Login web, thử redeem, đọc Pro/trial/gói/hạn",
            ),
        ],
    )

    @staticmethod
    def capcut_root(root: Path) -> Path:
        return root.parent / "capcut"

    def _py(self, root: Path) -> Path:
        from grokreg.core import winhide

        return winhide.hidden_python(root)

    @staticmethod
    def _is_hotmail_mail(mail: str) -> bool:
        return GrokToolPlugin._is_hotmail_mail(mail)

    def preflight(self, params: dict[str, Any], root: Path) -> None:
        cc = self.capcut_root(root)
        if not (cc / "main.py").exists():
            raise RuntimeError(f"Thiếu tool CapCut: {cc}")
        if not self._is_hotmail_mail(str(params.get("mail") or "4")):
            return
        pool = self.hotmail_pool(root)
        slots = int(pool.get("slots") or pool.get("count") or 0)
        if slots <= 0:
            raise RuntimeError("Pool Hotmail trống / hết slot alias — import acc rồi Start")

    def build_command(self, params: dict[str, Any], root: Path) -> list[str]:
        py = self._py(root)
        if not py.exists():
            raise RuntimeError(f"Python venv not found: {py}")
        mail = str(params.get("mail") or "4")
        if self._is_hotmail_mail(mail):
            pool = self.hotmail_pool(root)
            count = int(pool.get("slots") or pool.get("count") or 0)
            if count <= 0:
                raise RuntimeError("Pool Hotmail trống — import acc trước khi Start")
            count = max(0, count)
        else:
            if mail not in ("2", "4"):
                mail = "4"
            count = int(params.get("count") if params.get("count") is not None else 1)
            count = max(0, count)
        return [
            str(py),
            "-u",
            "main.py",
            mail,
            "--count",
            str(count),
            "--backend",
            "protocol",
        ]

    def cwd(self, root: Path) -> Path:
        return self.capcut_root(root)

    def env_overrides(self, params: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        inv = str(params.get("invite") or "").strip()
        if inv:
            env["CAPCUT_INVITE"] = inv
        claim = params.get("claim_offer", True)
        if claim is False or str(claim).lower() in ("0", "false", "no", "off"):
            env["CAPCUT_CLAIM"] = "0"
        else:
            env["CAPCUT_CLAIM"] = "1"
        return env

    def stop_signal(self, root: Path) -> None:
        stop = self.capcut_root(root) / "data" / "STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop:web\n", encoding="utf-8")
        try:
            import sys

            cc = str(self.capcut_root(root))
            if cc not in sys.path:
                sys.path.insert(0, cc)
            from capreg.stop import request_stop

            request_stop("web", write_file=True)
        except Exception:
            pass

    def hotmail_pool(self, root: Path) -> dict[str, Any]:
        return GrokToolPlugin().hotmail_pool(root)

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
        path = self.capcut_root(root) / "data" / "accounts.txt"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
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
                    "tool": "capcut",
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
