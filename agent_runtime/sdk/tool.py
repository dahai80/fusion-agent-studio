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

    def to_daemon_dict(self) -> dict:
        # C12: 序列化 Tool 供 daemon 注册. Python handler 工具附源码 (inspect.getsource),
        # daemon 端 exec 成 BaseTool 子类; 无 handler 则 schema-only (terminal/shell).
        import inspect

        payload = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "metadata": self.metadata,
        }
        if self.handler is not None:
            try:
                source = inspect.getsource(self.handler)
                payload["source"] = source
                payload["handler_name"] = self.handler.__name__
                payload["type"] = "python"
            except (OSError, TypeError) as e:
                logger.warning(
                    "Tool %s handler source unavailable: %s — fallback schema-only",
                    self.name,
                    e,
                )
                payload["type"] = "terminal"
        else:
            payload["type"] = "terminal"
        return payload

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

    async def register_to_daemon(self, client) -> dict:
        # C12: 注册本 Tool 到 daemon (Python handler -> tool.register_python,
        # schema-only -> tool.dynamic_register). 注册后 agent 可调用此工具.
        payload = self.to_daemon_dict()
        result = await client.register_tool(payload)
        if "error" in result:
            logger.error("Tool %s daemon register failed: %s", self.name, result["error"])
        else:
            logger.info("Tool %s registered to daemon", self.name)
        return result
