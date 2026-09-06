"""Stop signal control for CapCut registration."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_STOP_EVENT = threading.Event()
_STOP_REASON = ""
STOP_FILE = Path(__file__).resolve().parent.parent / "data" / "STOP"


class StopRequested(Exception):
    pass


def request_stop(reason: str = "web", write_file: bool = False) -> None:
    global _STOP_REASON
    _STOP_REASON = reason
    _STOP_EVENT.set()
    if write_file:
        try:
            STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
            STOP_FILE.write_text(f"stop:{reason}\n", encoding="utf-8")
        except Exception:
            pass


def is_stop_requested() -> bool:
    if _STOP_EVENT.is_set():
        return True
    if STOP_FILE.exists():
        _STOP_EVENT.set()
        return True
    return False


def clear_stop() -> None:
    global _STOP_REASON
    _STOP_REASON = ""
    _STOP_EVENT.clear()
    if STOP_FILE.exists():
        try:
            STOP_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def raise_if_stop() -> None:
    if is_stop_requested():
        raise StopRequested(_STOP_REASON or "Stop signal received")
