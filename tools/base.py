"""Base tool class and result type for all built-in tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    """Result of a tool execution."""

    success: bool
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def __str__(self) -> str:
        if self.success:
            return self.output
        return f"Error: {self.error}"


class BaseTool(ABC):
    """Abstract base class for all tools."""

    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__.lower()

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """Execute the tool with the given parameters.

        Returns:
            String result of the execution.
        """
        ...

    def openai_schema(self) -> dict:
        """Return the OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": list(self.parameters.keys()),
                },
            },
        }