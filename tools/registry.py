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

    def list_tools(self) -> list[dict[str, Any]]:
        """List all registered tools as metadata dicts."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]

    def to_openai_schemas(self) -> list[dict]:
        """Convert all registered tools to OpenAI function-calling schemas."""
        return [t.openai_schema() for t in self._tools.values()]

    @property
    def count(self) -> int:
        return len(self._tools)