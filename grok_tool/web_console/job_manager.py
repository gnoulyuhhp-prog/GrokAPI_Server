"""Run tool jobs as subprocesses with ring-buffer logs."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from grokreg.core import winhide

from .plugins import get_plugin
from .plugins.base import BaseToolPlugin


_PROGRESS_RE = re.compile(
    r"@@JOB_PROGRESS\s+completed=(\d+)\s+total=(\d+)\s+ok=(\d+)\s+failed=(\d+)"
)


@dataclass
class Job:
    id: str
    tool_id: str
    params: dict[str, Any]
    status: str = "pending"  # pending|running|stopping|done|error|stopped
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    exit_code: Optional[int] = None
    error: str = ""
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=4000))
    proc: Any = field(default=None, repr=False)
    _log_seq: int = 0
    progress_completed: int = 0
    progress_total: int = 0
    progress_ok: int = 0
    progress_failed: int = 0

    def append_log(self, line: str) -> None:
        progress = _PROGRESS_RE.search(line)
        if progress:
            self.progress_completed = int(progress.group(1))
            self.progress_total = int(progress.group(2))
            self.progress_ok = int(progress.group(3))
            self.progress_failed = int(progress.group(4))
            self._log_seq += 1
            return
        self._log_seq += 1
        ts = time.strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {line.rstrip()}")

    def clear_logs(self) -> int:
        removed = len(self.logs)
        self.logs.clear()
        # Treat clearing as a log-stream change so SSE consumers refresh even
        # when no new process output is produced afterwards.
        self._log_seq += 1
        return removed

    def snapshot(self, log_from: int = 0) -> dict[str, Any]:
        lines = list(self.logs)
        # log_from is index into absolute sequence approx via length
        total = self._log_seq
        # return last N if log_from is high
        if log_from > 0 and log_from < total:
            # best-effort: return lines from offset in buffer
            start = max(0, len(lines) - (total - log_from))
            chunk = lines[start:]
        else:
            chunk = lines
        target = self.progress_total
        if target <= 0:
            try:
                target = max(0, int(self.params.get("count") or 0))
            except (TypeError, ValueError):
                target = 0
        percent = (
            min(100, round(self.progress_completed * 100 / target)) if target else None
        )
        return {
            "id": self.id,
            "tool_id": self.tool_id,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "log_seq": total,
            "logs": chunk if log_from else lines[-300:],
            "running": self.status in ("running", "stopping", "pending"),
            "progress": {
                "completed": self.progress_completed,
                "total": target,
                "ok": self.progress_ok,
                "failed": self.progress_failed,
                "percent": percent,
                "continuous": target == 0,
            },
        }


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        # RLock: start/stop may call helpers that also take the lock
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current_id: Optional[str] = None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [
            {
                "id": j.id,
                "tool_id": j.tool_id,
                "status": j.status,
                "params": j.params,
                "created_at": j.created_at,
                "ended_at": j.ended_at,
                "exit_code": j.exit_code,
            }
            for j in items[:limit]
        ]

    def _current_unlocked(self) -> Optional[Job]:
        if self._current_id and self._current_id in self._jobs:
            j = self._jobs[self._current_id]
            if j.status in ("running", "stopping", "pending"):
                return j
        for j in self._jobs.values():
            if j.status in ("running", "stopping", "pending"):
                return j
        return None

    def current(self) -> Optional[Job]:
        with self._lock:
            return self._current_unlocked()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_latest_for_tool(self, tool_id: str) -> Optional[Job]:
        with self._lock:
            tool_jobs = [j for j in self._jobs.values() if j.tool_id == tool_id]
            if not tool_jobs:
                return None
            # Return active job if running, else newest by created_at
            running = [j for j in tool_jobs if j.status in ("running", "stopping", "pending")]
            if running:
                return running[0]
            return max(tool_jobs, key=lambda j: j.created_at)

    def clear_logs(self, job_id: str) -> tuple[Job, int]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job, job.clear_logs()

    def clear_tool_logs(self, tool_id: str) -> int:
        with self._lock:
            total_removed = 0
            for job in self._jobs.values():
                if job.tool_id == tool_id:
                    total_removed += job.clear_logs()
            return total_removed

    def start(self, tool_id: str, params: dict[str, Any]) -> Job:
        with self._start_lock:
            plugin = get_plugin(tool_id)
            if plugin.meta.status == "coming_soon":
                raise RuntimeError(f"Tool '{plugin.meta.name}' chưa sẵn sàng")
            # Never restart/check shared services while another registration is live.
            with self._lock:
                cur = self._current_unlocked()
                if cur is not None:
                    raise RuntimeError(
                        f"Đang có job chạy ({cur.tool_id}:{cur.id[:8]}). Hãy Stop trước."
                    )
            if hasattr(plugin, "preflight"):
                plugin.preflight(params or {}, self.root)

            with self._lock:
                job = Job(id=uuid.uuid4().hex[:12], tool_id=tool_id, params=dict(params or {}))
                self._jobs[job.id] = job
                self._current_id = job.id

        t = threading.Thread(target=self._run, args=(job, plugin), daemon=True)
        t.start()
        return job

    def stop(self, job_id: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            if job_id:
                job = self._jobs.get(job_id)
            else:
                job = self._current_unlocked()
            if job is None:
                try:
                    get_plugin("grok").stop_signal(self.root)
                except Exception:
                    pass
                return {"ok": True, "message": "Không có job đang chạy — đã gửi STOP"}
            if job.status not in ("running", "pending", "stopping"):
                return {"ok": True, "message": f"Job đã {job.status}"}
            job.status = "stopping"
            plugin = get_plugin(job.tool_id)

        try:
            plugin.stop_signal(self.root)
            job.append_log(">>> STOP signal sent (data/STOP + soft stop)")
        except Exception as e:
            job.append_log(f"STOP signal error: {e}")

        proc = job.proc
        if proc and proc.poll() is None:
            # give soft stop a few seconds, then terminate
            def _kill_later():
                time.sleep(8)
                if proc.poll() is None:
                    job.append_log(">>> Force terminate process...")
                    try:
                        if os.name == "nt":
                            proc.terminate()
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            threading.Thread(target=_kill_later, daemon=True).start()

        return {"ok": True, "job_id": job.id, "message": "Đang dừng..."}

    def _run(self, job: Job, plugin: BaseToolPlugin) -> None:
        job.status = "running"
        job.started_at = time.time()
        job.append_log(f"=== START tool={job.tool_id} params={job.params} ===")
        try:
            cmd = plugin.build_command(job.params, self.root)
            try:
                count_pos = cmd.index("--count") + 1
                job.progress_total = max(0, int(cmd[count_pos]))
            except (ValueError, IndexError, TypeError):
                pass
            cwd = Path(plugin.cwd(self.root))
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["GROK_WEB_CONSOLE"] = "1"
            env["GROK_SKIP_KILL_OLD"] = "1"  # don't kill web console chrome accidentally
            if hasattr(plugin, "env_overrides"):
                env.update(plugin.env_overrides(job.params))  # type: ignore[attr-defined]

            # clear leftover STOP
            stop = self.root / "data" / "STOP"
            try:
                if stop.exists():
                    stop.unlink()
            except Exception:
                pass

            cmd = winhide.rewrite_python_cmd(cmd)
            job.append_log("CMD: " + " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **winhide.kwargs(new_group=True),
            )
            job.proc = proc

            assert proc.stdout is not None
            for line in proc.stdout:
                job.append_log(line)
            code = proc.wait()
            job.exit_code = code
            if job.status == "stopping":
                job.status = "stopped"
                job.append_log(f"=== STOPPED exit={code} ===")
            elif code == 0:
                job.status = "done"
                job.append_log("=== DONE OK ===")
            else:
                job.status = "error"
                job.append_log(f"=== DONE with exit={code} ===")
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.append_log(f"FATAL: {e}")
        finally:
            job.ended_at = time.time()
            job.proc = None
