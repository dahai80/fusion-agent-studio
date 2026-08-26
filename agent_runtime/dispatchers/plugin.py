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
            "plugin.install": self._handle_plugin_install,
            "plugin.uninstall": self._handle_plugin_uninstall,
        }

    async def _handle_plugin_list_tools(self, params: dict) -> dict:
        from tools import create_default_registry

        from ..plugin_bridge import list_mcp_tools

        registry = self._daemon._get_runtime().tool_registry
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

        registry = self._daemon._get_runtime().tool_registry
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

        registry = self._daemon._get_runtime().tool_registry
        if registry is None:
            registry = create_default_registry()

        tool = registry._tools.get(tool_name)
        if tool is None:
            return self._err(f"tool '{tool_name}' not found")

        result = await invoke_tool(tool, arguments)
        return result

    async def _handle_plugin_install(self, params: dict) -> dict:
        # 审计 P2/dim1: 旧返回 "queued" 但从不执行 — UI 误判安装成功. 显式 not_implemented.
        # 插件经文件落盘 ~/.fusion-agent-studio/plugins/ + PluginManager 自动加载, 无远程安装.
        source = params.get("source", "")
        if not source:
            return self._err("source is required")
        logger.info("plugin.install: source=%s (not_implemented)", source)
        return {
            "status": "not_implemented",
            "implemented": False,
            "message": "Remote plugin install not implemented. Drop .py into "
            "~/.fusion-agent-studio/plugins/ for auto-load.",
            "source": source,
        }

    async def _handle_plugin_uninstall(self, params: dict) -> dict:
        # 审计 P2/dim1: 同 install — 旧 "queued" 谎报. 显式 not_implemented.
        name = params.get("name", "")
        if not name:
            return self._err("name is required")
        logger.info("plugin.uninstall: name=%s (not_implemented)", name)
        return {
            "status": "not_implemented",
            "implemented": False,
            "message": "Programmatic uninstall not implemented. Remove the .py "
            "file from ~/.fusion-agent-studio/plugins/ to disable.",
            "name": name,
        }
