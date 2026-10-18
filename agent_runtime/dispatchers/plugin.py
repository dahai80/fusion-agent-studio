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
            "plugin.package": self._handle_plugin_package,
        }

    def _registry(self):
        from tools import create_default_registry

        registry = self._daemon._get_runtime().tool_registry
        if registry is None:
            registry = create_default_registry()
            self._daemon._get_runtime().tool_registry = registry
        return registry

    def _pm(self):
        from tools.plugin_manager import PluginManager

        cached = getattr(self._daemon, "_plugin_manager", None)
        if cached is None:
            cached = PluginManager(self._registry())
            self._daemon._plugin_manager = cached
        return cached

    def _plane(self):
        getter = getattr(self._daemon, "_get_protocol_plane", None)
        if getter is not None:
            return getter()
        from agent_runtime.protocol_plane import ProtocolPlane

        plane = getattr(self._daemon, "_protocol_plane", None)
        if plane is None:
            plane = ProtocolPlane()
            self._daemon._protocol_plane = plane
        return plane

    async def _handle_plugin_list_tools(self, params: dict) -> dict:
        from tools import create_default_registry

        from ..plugin_bridge import list_mcp_tools

        registry = self._daemon._get_runtime().tool_registry
        if registry is None:
            registry = create_default_registry()

        try:
            plane = self._plane()
            for tool_name in sorted(getattr(registry, "plugin_tools", set())):
                plane.ensure("plugin", tool_name, origin="plugins")
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

        # N17 #390: plugin-sourced tools must exist on the governance plane
        # before execution. Built-in registry tools are not plugin artifacts.
        if tool_name in getattr(registry, "plugin_tools", set()):
            try:
                self._plane().require("plugin", tool_name)
            except ValueError as e:
                logger.warning("plugin.invoke governance denied tool=%s: %s", tool_name, e)
                return self._err(str(e))
        result = await invoke_tool(tool, arguments)
        return result

    async def _handle_plugin_install(self, params: dict) -> dict:
        # N17 #390: local source install is implemented; remote URLs still
        # return an explicit not_implemented instead of a fake "queued".
        source = str(params.get("source", "") or "")
        if not source:
            return self._err("source is required")
        if "://" in source:
            logger.info("plugin.install: remote source=%s (not_implemented)", source)
            return {
                "status": "not_implemented",
                "implemented": False,
                "message": "Remote plugin install not implemented. Pass a local "
                ".py path or drop it into ~/.fusion-agent-studio/plugins/.",
                "source": source,
            }
        try:
            tool = self._pm().install_local(source, name=str(params.get("name", "") or ""))
        except (ValueError, OSError) as e:
            logger.error("plugin.install failed source=%s: %s", source, e)
            return self._err(str(e))
        art = self._plane().ensure("plugin", tool.name, origin=str(source), readonly=False)
        logger.info("plugin.install: source=%s tool=%s", source, tool.name)
        return {"status": "ok", "implemented": True, "tool": tool.name, "artifact": art.to_dict()}

    async def _handle_plugin_uninstall(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        if not name:
            return self._err("name is required")
        pm = self._pm()
        tool_names = pm.tool_names_for(name)
        try:
            removed = pm.uninstall(name)
        except (ValueError, OSError) as e:
            logger.error("plugin.uninstall failed name=%s: %s", name, e)
            return self._err(str(e))
        for tool_name in tool_names:
            self._plane().unregister("plugin", tool_name)
        logger.info("plugin.uninstall: name=%s removed=%s tools=%s", name, removed, tool_names)
        return {"status": "ok" if removed else "not_found", "implemented": True,
                "name": name, "removed": removed, "tools": tool_names}

    async def _handle_plugin_package(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        dest_dir = str(params.get("dest_dir", "") or "")
        if not name or not dest_dir:
            return self._err("name and dest_dir are required")
        try:
            path = self._pm().package(name, dest_dir)
        except (ValueError, OSError) as e:
            logger.error("plugin.package failed name=%s: %s", name, e)
            return self._err(str(e))
        logger.info("plugin.package: name=%s path=%s", name, path)
        return {"status": "ok", "name": name, "path": str(path)}
