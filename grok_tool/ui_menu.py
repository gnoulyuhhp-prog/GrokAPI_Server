"""Compatibility shim — implementation lives in `grokreg.tools.ui_menu`."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grokreg.tools.ui_menu import *  # noqa: F403
from grokreg.tools.ui_menu import _utf8, main_menu

if __name__ == "__main__":
    _utf8()
    try:
        main_menu()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}")
        try:
            input("Enter...")
        except Exception:
            pass
        raise SystemExit(1)

