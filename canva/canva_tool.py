"""Canva Multipurpose Tool (Redeem CLI)."""
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

from canreg.redeem import redeem_batch
from canreg.stop import clear_stop


def parse_args():
    parser = argparse.ArgumentParser(description="Canva Tool CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    
    # Redeem subcommand
    redeem_p = subparsers.add_parser("redeem", help="Redeem promo codes for accounts")
    redeem_p.add_argument("--accounts", default="data/accounts.txt", help="Path to accounts.txt")
    redeem_p.add_argument("--codes", default="data/codes_web.txt", help="Path to codes.txt")
    redeem_p.add_argument("--threads", type=int, default=3, help="Number of concurrent threads")
    redeem_p.add_argument("--output", default="data/proof.json", help="Output JSON proof file")
    redeem_p.add_argument("--success-only", action="store_true", help="Log only successes")
    
    return parser.parse_args()


def main():
    clear_stop()
    args = parse_args()
    if args.subcommand == "redeem":
        try:
            asyncio.run(redeem_batch(args.accounts, args.codes, args.threads, args.output, args.success_only))
        except KeyboardInterrupt:
            print("\n[Canva] Người dùng bấm Ctrl+C để dừng.")
        except Exception as e:
            print(f"\n[Canva] Lỗi: {e}")


if __name__ == "__main__":
    main()
