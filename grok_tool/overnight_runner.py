"""Compatibility shim — implementation lives in `grokreg.tools.overnight_runner`."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grokreg.tools.overnight_runner import *  # noqa: F403
from grokreg.tools.overnight_runner import main

if __name__ == "__main__":
    raise SystemExit(main())

