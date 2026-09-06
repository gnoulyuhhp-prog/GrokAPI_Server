from __future__ import annotations

import asyncio
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from grokreg.core import style_log
from grokreg.cli import app as cli_app
from web_console.job_manager import Job
from web_console.plugins.grok import GrokToolPlugin


class JobProgressTest(unittest.TestCase):
    def test_progress_marker_updates_snapshot_without_polluting_logs(self):
        job = Job(
            id="progress-test",
            tool_id="grok",
            params={"count": 100},
            status="running",
        )

        job.append_log("@@JOB_PROGRESS completed=2 total=100 ok=1 failed=1")

        snapshot = job.snapshot()
        self.assertEqual(
            snapshot["progress"],
            {
                "completed": 2,
                "total": 100,
                "ok": 1,
                "failed": 1,
                "percent": 2,
                "continuous": False,
            },
        )
        self.assertEqual(snapshot["logs"], [])

    def test_clearing_visible_logs_keeps_batch_progress(self):
        job = Job(id="progress-test", tool_id="grok", params={"count": 10})
        job.append_log("visible")
        job.append_log("@@JOB_PROGRESS completed=3 total=10 ok=2 failed=1")

        self.assertEqual(job.clear_logs(), 1)
        self.assertEqual(job.snapshot()["progress"]["completed"], 3)

    def test_progress_emitter_contract(self):
        with patch.dict("os.environ", {"GROK_WEB_CONSOLE": "1"}), patch.object(
            style_log, "_out"
        ) as output:
            style_log.progress(4, 100, 3)
        output.assert_called_once_with(
            "@@JOB_PROGRESS completed=4 total=100 ok=3 failed=1"
        )

    def test_grok_web_command_accepts_one_hundred_accounts(self):
        plugin = GrokToolPlugin()
        field = next(field for field in plugin.meta.fields if field.key == "count")
        self.assertEqual(field.max, 1000000)

        with patch.object(plugin, "_py", return_value=Path(sys.executable)):
            command = plugin.build_command(
                {"mail": "0", "count": 100, "backend": "github", "threads": 5}, Path.cwd()
            )
        self.assertEqual(command[command.index("--count") + 1], "100")
        self.assertEqual(command[command.index("--threads") + 1], "5")

    def test_parallel_github_really_overlaps_workers(self):
        guard = threading.Lock()
        active = 0
        max_active = 0

        def fake_register(_config):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.08)
            with guard:
                active -= 1
            return SimpleNamespace(
                ok=True,
                status="success",
                email="test@example.com",
                duration_sec=0.08,
            )

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("grokreg.protocol.worker.register_one_github", side_effect=fake_register),
                patch.object(cli_app, "ROOT", Path(tmp)),
                patch.object(cli_app, "is_stop_requested", return_value=False),
                patch.object(cli_app, "interruptible_sleep", new=AsyncMock(return_value=None)),
                patch.object(style_log, "_out"),
            ):
                ok, completed = asyncio.run(
                    cli_app.run_parallel_github({}, batch=6, threads=3)
                )

        self.assertEqual((ok, completed), (6, 6))
        self.assertGreaterEqual(max_active, 3)


if __name__ == "__main__":
    unittest.main()
