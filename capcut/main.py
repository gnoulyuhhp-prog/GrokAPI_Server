"""CapCut CLI Registration Tool."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Force UTF-8 on Windows
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
GROK_TOOL_DIR = ROOT.parent / "grok_tool"
if str(GROK_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(GROK_TOOL_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capreg.flow import run_batch
from capreg.stop import clear_stop


def parse_args():
    parser = argparse.ArgumentParser(description="CapCut Auto Register & Pro Offer Claim")
    parser.add_argument("mail", nargs="?", default="4", help="Loai email: 1=Hotmail, 2=Azpop, 4=Guerrilla")
    parser.add_argument("--count", type=int, default=1, help="So luong acc can reg (0=vo han)")
    parser.add_argument("--backend", default="protocol", help="Backend (mac dinh protocol)")
    return parser.parse_args()


def main():
    clear_stop()
    args = parse_args()
    try:
        asyncio.run(run_batch(args.mail, args.count, args.backend))
    except KeyboardInterrupt:
        print("\n[CapCut] Người dùng bấm Ctrl+C để dừng.")
    except Exception as e:
        print(f"\n[CapCut] Lỗi: {e}")


if __name__ == "__main__":
    main()
