"""Compatibility shim — implementation lives in `grokreg.tools.export_morning_report`."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grokreg.tools.export_morning_report import *  # noqa: F403
from grokreg.tools.export_morning_report import main

if __name__ == "__main__":
    raise SystemExit(main())

