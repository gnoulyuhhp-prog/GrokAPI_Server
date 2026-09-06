"""Plugin contract for registration tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FieldOption:
    value: str
    label: str
    hint: str = ""


@dataclass
class ToolField:
    key: str
    label: str
    type: str = "text"  # text | number | select | checkbox | password | textarea
    default: Any = ""
    options: list[FieldOption] = field(default_factory=list)
    hint: str = ""
    min: Optional[int] = None
    max: Optional[int] = None


@dataclass
class ToolMeta:
    id: str
    name: str
    description: str
    icon: str = "◆"
    status: str = "ready"  # ready | beta | coming_soon
    fields: list[ToolField] = field(default_factory=list)
    color: str = "#229ed9"
    # Official publisher mark. Empty = auto /static/img/brands/{id}.svg|.png|.webp
    brand_icon: str = ""


class BaseToolPlugin:
    """Override for each registration tool."""

    meta: ToolMeta

    def build_command(self, params: dict[str, Any], root: Any) -> list[str]:
        raise NotImplementedError

    def cwd(self, root: Any) -> Any:
        return root

    def stop_signal(self, root: Any) -> None:
        """Write STOP / soft-stop for this tool."""
        pass

    def parse_results(self, root: Any, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def stats(self, root: Any) -> dict[str, Any]:
        return {}
