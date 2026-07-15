"""Node executor — dispatches execution to the appropriate handler for each node type."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .graph import NodeConfig

if TYPE_CHECKING:
    from .context import AgentContext
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry


class NodeExecutor:
    """Dispatches node execution to type-specific handlers."""

    def __init__(
        self,
        mlx_client: "FusionMLXClient",
        tool_registry: "ToolRegistry",
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        self._handlers = {
            "start": self._handle_start,
            "llm": self._handle_llm,
            "tool": self._handle_tool,
            "condition": self._handle_condition,
            "loop": self._handle_loop,
            "end": self._handle_end,
        }

    def get_handler(self, node_type: str):
        """Get the handler for a node type."""
        handler = self._handlers.get(node_type)
        if not handler:
            raise ValueError(f"Unknown node type: {node_type}")
        return handler

    async def _handle_start(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        return {"action": "next", "output": ""}

    async def _handle_llm(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        model = kwargs.get("model", node.model)
        tools_schema = kwargs.get("tools_schema", [])
        system_prompt = kwargs.get("system_prompt", node.system_prompt)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(ctx.messages)

        response = await self.mlx.chat(
            model=model,
            messages=messages,
            tools=tools_schema if tools_schema else None,
            temperature=node.temperature,
            max_tokens=node.max_tokens,
        )
        return {
            "action": "llm_response",
            "output": response.content,
            "tool_calls": response.tool_calls,
            "usage": response.usage,
        }

    async def _handle_tool(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        tool = self.tools.get(node.tool_name)
        result = await tool.execute(**node.tool_params)
        return {"action": "next", "output": str(result)}

    async def _handle_condition(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        from .runtime import AgentRuntime
        runtime = AgentRuntime(self.mlx, self.tools)
        result = runtime._evaluate_condition(node.condition_expr, ctx)
        return {"action": "branch", "output": result}

    async def _handle_loop(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        return {"action": "next", "output": f"iteration {ctx.iteration_count}"}

    async def _handle_end(self, node: NodeConfig, ctx: "AgentContext", **kwargs) -> dict[str, Any]:
        return {"action": "stop", "output": ""}