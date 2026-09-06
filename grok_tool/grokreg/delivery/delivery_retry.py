"""
Durable background delivery for Sub2API (and optional sheet) after reg success.

Competitor pattern: reg success is independent of upload success.
Failed SSO→Sub2API imports land in a local JSON queue and retry until done.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("grok_tool")

ROOT = Path(__file__).resolve().parents[2]
QUEUE_FILE = ROOT / "data" / "delivery_queue.json"
_lock = threading.Lock()
_worker: Optional["DeliveryRetryWorker"] = None
_worker_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_queue() -> list[dict[str, Any]]:
    try:
        if QUEUE_FILE.exists():
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
    except Exception as e:
        log.warning("[delivery] load queue failed: %s", e)
    return []


def _save_queue(items: list[dict[str, Any]]) -> None:
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(QUEUE_FILE)
    except Exception as e:
        log.warning("[delivery] save queue failed: %s", e)


def enqueue_sub2api(
    *,
    email: str,
    sso: str,
    name: str = "",
    password: str = "",
    group: str = "grok free",
    sub_cfg: dict[str, Any] | None = None,
    error: str = "",
) -> str:
    """Add or refresh a pending Sub2API delivery. Returns queue id."""
    if not (sso or "").strip():
        log.warning("[delivery] skip enqueue — empty SSO for %s", email)
        return ""
    rid = uuid.uuid4().hex[:12]
    rec = {
        "id": rid,
        "kind": "sub2api",
        "email": email,
        "password": password or "",
        "sso": sso.strip(),
        "name": name or "",
        "group": group or "grok free",
        "sub_cfg_snapshot": {
            k: v
            for k, v in (sub_cfg or {}).items()
            if k
            in (
                "sub2api_url",
                "sub2api_user",
                "sub2api_pass",
                "sub2api_email",
                "sub2api_password",
                "sub2api_api_token",
                "api_token",
                "group",
                "group_ids",
                "name_prefix",
                "name_include_email",
                "refresh_usage_after_import",
                "usage_refresh_sec",
                "timeout_sec",
                "timeout_oauth_sec",
                "concurrency",
                "priority",
                "proxy_id",
                "auto_pause_on_expired",
            )
        },
        "attempts": 0,
        "last_error": (error or "")[:300],
        "status": "pending",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    with _lock:
        items = _load_queue()
        # de-dupe by email+kind pending
        items = [
            x
            for x in items
            if not (
                x.get("kind") == "sub2api"
                and str(x.get("email") or "").lower() == email.lower()
                and x.get("status") in ("pending", "retrying")
            )
        ]
        items.append(rec)
        _save_queue(items)
    log.info("[delivery] queued sub2api id=%s email=%s name=%s", rid, email, name)
    return rid


def queue_stats() -> dict[str, int]:
    with _lock:
        items = _load_queue()
    pending = sum(1 for x in items if x.get("status") in ("pending", "retrying"))
    done = sum(1 for x in items if x.get("status") == "done")
    failed = sum(1 for x in items if x.get("status") == "failed")
    return {"total": len(items), "pending": pending, "done": done, "failed": failed}


def process_queue_once(
    config: dict[str, Any] | None = None,
    *,
    limit: int = 10,
    max_attempts: int = 30,
) -> int:
    """
    Try pending Sub2API deliveries once.
    Returns number of newly completed items.
    """
    from grokreg.delivery.sub2api_client import Sub2APIError, export_sso_to_sub2api

    completed = 0
    with _lock:
        items = _load_queue()
        pending = [
            x
            for x in items
            if x.get("kind") == "sub2api" and x.get("status") in ("pending", "retrying")
        ][:limit]

    if not pending:
        return 0

    # Fast probe Sub2API reachability before looping items to avoid hanging when server is offline
    probe_cfg = dict(pending[0].get("sub_cfg_snapshot") or {})
    if config:
        probe_cfg.update(dict(config.get("sub2api") or {}))
    try:
        from grokreg.delivery.sub2api_client import client_from_cfg
        _probe_client = client_from_cfg(probe_cfg)
        _probe_client.resolve_token()
    except Exception as _probe_exc:
        log.debug("[delivery] Sub2API server not reachable (%s), skipping batch retry", _probe_exc)
        return 0

    for rec in pending:
        rid = rec.get("id")
        email = str(rec.get("email") or "")
        sso = str(rec.get("sso") or "")
        name = str(rec.get("name") or "")
        attempts = int(rec.get("attempts") or 0) + 1

        # merge snapshot + live config (live config takes precedence)
        sub_cfg: dict[str, Any] = {}
        sub_cfg.update(dict(rec.get("sub_cfg_snapshot") or {}))
        if config:
            sub_cfg.update(dict(config.get("sub2api") or {}))
        if rec.get("group"):
            sub_cfg.setdefault("group", rec["group"])

        try:
            result = export_sso_to_sub2api(
                sub_cfg,
                sso,
                email=email,
                name=name or email,
            )
            with _lock:
                items = _load_queue()
                for x in items:
                    if x.get("id") == rid:
                        x["status"] = "done"
                        x["attempts"] = attempts
                        x["last_error"] = ""
                        x["result_name"] = result.get("name") or name
                        x["account_id"] = result.get("account_id")
                        x["updated_at"] = _now_iso()
                        break
                # prune done older than keep — keep last 50 done
                done_ids = [x["id"] for x in items if x.get("status") == "done"]
                if len(done_ids) > 50:
                    drop = set(done_ids[:-50])
                    items = [x for x in items if x.get("id") not in drop]
                _save_queue(items)
            completed += 1
            result_name = str(result.get("name") or name or "").strip()
            log.info(
                "[delivery] retry OK id=%s email=%s name=%s",
                rid,
                email,
                result_name,
            )
            try:
                from grokreg.core.helpers import save_account
                from grokreg.core.paths_cfg import ACCOUNTS
                from grokreg.reg.flow import push_results_to_gsheet

                save_account(
                    ACCOUNTS(),
                    email,
                    str(rec.get("password") or ""),
                    f"added_sub2api:{result_name}" if result_name else "added_sub2api",
                )
                push_results_to_gsheet(config or {}, email)
            except Exception as e:
                log.error("[delivery] Google Sheet push failed for %s: %s", email, e)
        except Exception as exc:
            err = str(exc)[:300]
            status = "failed" if attempts >= max_attempts else "retrying"
            with _lock:
                items = _load_queue()
                for x in items:
                    if x.get("id") == rid:
                        x["status"] = status
                        x["attempts"] = attempts
                        x["last_error"] = err
                        x["updated_at"] = _now_iso()
                        break
                _save_queue(items)
            log.warning(
                "[delivery] retry fail id=%s attempt=%s status=%s err=%s",
                rid,
                attempts,
                status,
                err[:120],
            )
    return completed


class DeliveryRetryWorker:
    def __init__(
        self,
        config_getter,
        *,
        interval_seconds: int = 60,
    ):
        self._config_getter = config_getter
        self.interval_seconds = max(15, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="delivery-retry",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "[delivery] durable retry worker started interval=%ss",
            self.interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = None
                try:
                    cfg = self._config_getter() if self._config_getter else None
                except Exception:
                    cfg = None
                n = process_queue_once(cfg)
                if n:
                    log.info("[delivery] worker completed %s item(s)", n)
            except Exception as exc:
                log.warning("[delivery] worker tick failed: %s", exc)
            self._stop.wait(self.interval_seconds)


def ensure_worker(config: dict[str, Any]) -> None:
    """Start global durable worker if enabled in config.sub2api."""
    global _worker
    sub = config.get("sub2api") or {}
    if not sub.get("enabled", True):
        return
    if sub.get("durable_retry") is False:
        return
    interval = int(sub.get("durable_interval_sec") or 60)

    def _getter():
        return config

    with _worker_lock:
        if _worker is None:
            _worker = DeliveryRetryWorker(_getter, interval_seconds=interval)
            _worker.start()
        else:
            # already running
            pass


def stop_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker:
            _worker.stop()
            _worker = None
