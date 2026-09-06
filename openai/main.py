"""OpenAI / ChatGPT CLI Registration Tool."""
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

from openaireg.flow import run_batch
from openaireg.stop import clear_stop


def parse_args():
    parser = argparse.ArgumentParser(description="OpenAI / ChatGPT Auto Register Tool")
    parser.add_argument("mail", nargs="?", default="0", help="Loai email: 0=SMART, 1=Hotmail, 2=Azpop, 3=Tmail")
    parser.add_argument("--count", type=int, default=1, help="So luong acc can reg (0=vo han)")
    parser.add_argument("--backend", default="protocol", choices=["protocol", "browser", "auto"], help="Backend")
    return parser.parse_args()


def main():
    clear_stop()
    args = parse_args()
    try:
        asyncio.run(run_batch(args.mail, args.count, args.backend))
    except KeyboardInterrupt:
        print("\n[OpenAI] Người dùng bấm Ctrl+C để dừng.")
    except Exception as e:
        print(f"\n[OpenAI] Lỗi: {e}")


if __name__ == "__main__":
    main()
