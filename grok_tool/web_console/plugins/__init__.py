"""Tool plugin registry — add new tools here."""

from __future__ import annotations

from typing import Dict

from .base import BaseToolPlugin
from .grok import GrokToolPlugin
from .heygen import HeygenToolPlugin
from .capcut import CapcutToolPlugin
from .zai import ZaiToolPlugin
from .canva import CanvaToolPlugin
from .claude import ClaudeToolPlugin
from .openai import OpenAIToolPlugin


_BRAND_COLORS = {
    "claude": "#D97757",
    "openai": "#10A37F",
}


def all_plugins() -> Dict[str, BaseToolPlugin]:
    plugins: list[BaseToolPlugin] = [
        GrokToolPlugin(),
        HeygenToolPlugin(),
        CapcutToolPlugin(),
        ZaiToolPlugin(),
        CanvaToolPlugin(),
        ClaudeToolPlugin(),
        OpenAIToolPlugin(),
    ]
    return {p.meta.id: p for p in plugins}


def get_plugin(tool_id: str) -> BaseToolPlugin:
    reg = all_plugins()
    if tool_id not in reg:
        raise KeyError(f"Unknown tool: {tool_id}")
    return reg[tool_id]
