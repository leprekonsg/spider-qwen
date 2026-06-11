"""Tool registry.

Maps tool names the controller/planner may invoke to callables. v1 registers
only `search` and `fetch` (the lightest tools). Agent/Browser/code-interpreter
are intentionally absent.
"""

from __future__ import annotations

from typing import Any, Callable


class ToolRegistry:
    ALLOWED_V1 = frozenset({"search", "fetch", "qwen_web_extractor"})

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self.require_allowed(name)
        self._tools[name] = fn

    @classmethod
    def require_allowed(cls, name: str) -> None:
        if name not in cls.ALLOWED_V1:
            raise ValueError(
                f"Tool '{name}' is not allowed in v1. Allowed: {sorted(cls.ALLOWED_V1)}"
            )

    def get(self, name: str) -> Callable[..., Any]:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
