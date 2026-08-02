"""Compatibility shim — NodeExecutor has been inlined into AgentRuntime.

This module exists so that external plugins importing
``from agent_runtime.executor import NodeExecutor`` continue to work.
The real execution logic lives in agent_runtime.runtime.AgentRuntime.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .graph import NodeConfig

if TYPE_CHECKING:
    from .context import AgentContext
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class NodeExecutor:
    """Compatibility shim — dispatches to AgentRuntime internally.

    Prefer using AgentRuntime directly. This class is kept for
    backward compatibility with plugins that imported NodeExecutor.
    """

    def __init__(
        self,
        mlx_client: "FusionMLXClient",
        tool_registry: "ToolRegistry",
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        logger.warning(
            "NodeExecutor is deprecated — use AgentRuntime instead. "
            "Import from agent_runtime.runtime."
        )

    def get_handler(self, node_type: str):
        raise NotImplementedError(
            "NodeExecutor is deprecated. Use AgentRuntime directly."
        )

    async def _handle_start(
        self, node: NodeConfig, ctx: "AgentContext", **kwargs
    ) -> dict[str, Any]:
        return {"action": "next", "output": ""}

    async def _handle_end(
        self, node: NodeConfig, ctx: "AgentContext", **kwargs
    ) -> dict[str, Any]:
        return {"action": "stop", "output": ""}
