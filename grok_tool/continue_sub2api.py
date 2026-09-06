"""Compatibility shim — implementation lives in `grokreg.tools.continue_sub2api`."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grokreg.tools.continue_sub2api import *  # noqa: F403
from grokreg.tools.continue_sub2api import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

