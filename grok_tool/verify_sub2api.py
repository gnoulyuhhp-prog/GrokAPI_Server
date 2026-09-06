"""Compatibility shim — implementation lives in `grokreg.tools.verify_sub2api`."""
from grokreg.tools.verify_sub2api import *  # noqa: F403
from grokreg.tools.verify_sub2api import main

if __name__ == "__main__":
    raise SystemExit(main())
