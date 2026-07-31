from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str = ""
    description: str = ""
    tool_id: str = ""
    parameters: dict = field(default_factory=dict)
    handler: Callable | None = field(default=None, repr=False)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.tool_id:
            self.tool_id = f"tool_{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tool_id": self.tool_id,
            "parameters": self.parameters,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Tool:
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            tool_id=data.get("tool_id", ""),
            parameters=data.get("parameters", {}),
            metadata=data.get("metadata", {}),
        )

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs) -> Any:
        if self.handler is None:
            logger.error("Tool %s has no handler", self.name)
            return {"error": f"No handler for tool {self.name}"}
        try:
            result = self.handler(**kwargs)
            logger.info("Tool %s executed successfully", self.name)
            return result
        except Exception as e:
            logger.exception("Tool %s execution failed", self.name)
            return {"error": str(e)}
