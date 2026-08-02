"""Plugin dispatcher — expose agent-studio tools via fusion-plugins-ecosystem registry.

Importers: dispatchers/__init__.py, daemon_server.py (_init_sub_dispatchers)
API: plugin.list_tools, plugin.gateway_info, plugin.invoke
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class PluginDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "plugin.list_tools": self._handle_plugin_list_tools,
            "plugin.gateway_info": self._handle_plugin_gateway_info,
            "plugin.invoke": self._handle_plugin_invoke,
        }

    async def _handle_plugin_list_tools(self, params: dict) -> dict:
        from tools import create_default_registry

        from ..plugin_bridge import list_mcp_tools

        registry = self._daemon._get_runtime()._tool_registry
        if registry is None:
            registry = create_default_registry()

        try:
            tools = list_mcp_tools(registry._tools)
            return {"tools": tools, "count": len(tools)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("plugin.list_tools failed: %s", e)
            return self._err(str(e))

    async def _handle_plugin_gateway_info(self, params: dict) -> dict:
        from tools import create_default_registry

        from ..plugin_bridge import gateway_info

        registry = self._daemon._get_runtime()._tool_registry
        if registry is None:
            registry = create_default_registry()

        try:
            info = gateway_info(registry._tools)
            return info
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("plugin.gateway_info failed: %s", e)
            return self._err(str(e))

    async def _handle_plugin_invoke(self, params: dict) -> dict:
        from tools import create_default_registry

        from ..plugin_bridge import invoke_tool

        tool_name = params.get("tool_name", "")
        arguments = params.get("arguments", {})

        if not tool_name:
            return self._err("tool_name is required")

        registry = self._daemon._get_runtime()._tool_registry
        if registry is None:
            registry = create_default_registry()

        tool = registry._tools.get(tool_name)
        if tool is None:
            return self._err(f"tool '{tool_name}' not found")

        result = await invoke_tool(tool, arguments)
        return result
