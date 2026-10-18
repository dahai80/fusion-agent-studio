"""Tool registry — manages tool registration and discovery."""

from __future__ import annotations

from typing import Any

from .base import BaseTool


class ToolRegistry:
    """Registry for all available tools.

    Supports built-in tools and user-defined plugins.
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self.failed_plugins: dict[str, str] = {}  # plugin_name -> error, 显式化加载失败 (issue #164)
        # N17 #390: tool names sourced from user plugins, so the protocol
        # plane can enforce governance for the plugin layer only.
        self.plugin_tools: set[str] = set()

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        if not tool.name:
            raise ValueError("Tool must have a name")
        self._tools[tool.name] = tool

    def register_from_class(self, tool_class: type[BaseTool]) -> BaseTool:
        """Instantiate and register a tool from its class."""
        tool = tool_class()
        self.register(tool)
        return tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool:
        """Get a tool by name. Raises KeyError if not found."""
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found. Available: {list(self._tools.keys())}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def list_tools(self, role: str = "all") -> list[dict[str, Any]]:
        # M3-1 issue#322: role filter. role="all" (default) = current behavior (return all).
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
            if role == "all" or "all" in t.roles or role in t.roles
        ]

    def search_tools(self, query: str, limit: int = 8, role: str = "all") -> list[dict]:
        # N6/#343: keyword search over names+descriptions for lazy schema
        # loading — deferred tools stay out of the prompt until found here.
        q = (query or "").strip().lower()
        out = []
        for t in self._tools.values():
            if role != "all" and "all" not in t.roles and role not in t.roles:
                continue
            if not q or q in t.name.lower() or q in (t.description or "").lower():
                out.append({
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                })
                if len(out) >= max(1, limit):
                    break
        return out

    def to_openai_schemas(self, role: str = "all") -> list[dict]:
        # M3-1 issue#322: role filter. role="all" (default) = current behavior (return all).
        return [
            t.openai_schema()
            for t in self._tools.values()
            if role == "all" or "all" in t.roles or role in t.roles
        ]

    def tools_for_role(self, role: str) -> list[str]:
        # M3-1 issue#322: return tool names visible to a role.
        return [
            t.name
            for t in self._tools.values()
            if role == "all" or "all" in t.roles or role in t.roles
        ]

    def readonly_tool_names(self) -> set[str]:
        # N2 auto-grading: names of registered tools annotated readonly=True.
        # The permission engine auto-allows these without explicit allow rules.
        return {t.name for t in self._tools.values() if getattr(t, "readonly", False)}

    @property
    def count(self) -> int:
        return len(self._tools)