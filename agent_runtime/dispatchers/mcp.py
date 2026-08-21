"""MCP dispatcher — register/discover inbound Model Context Protocol servers.

Importers: dispatchers/__init__.py, daemon_server.py (_init_sub_dispatchers)
API: mcp.register_server, mcp.list_servers, mcp.unregister_server,
     mcp.list_resources, mcp.list_prompts
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class McpDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "mcp.register_server": self._handle_register_server,
            "mcp.list_servers": self._handle_list_servers,
            "mcp.unregister_server": self._handle_unregister_server,
            "mcp.list_resources": self._handle_list_resources,
            "mcp.list_prompts": self._handle_list_prompts,
        }

    def _get_mcp_registry(self):
        """Lazily create + cache an MCPRegistry on the daemon, bound to the runtime tool registry."""
        from tools import create_default_registry
        from tools.mcp_tool import MCPRegistry

        registry = self._daemon._get_runtime().tool_registry
        if registry is None:
            registry = create_default_registry()
            self._daemon._get_runtime().tool_registry = registry

        mcp_reg = getattr(self._daemon, "_mcp_registry", None)
        if mcp_reg is None or getattr(mcp_reg, "_registry", None) is not registry:
            mcp_reg = MCPRegistry(registry)
            self._daemon._mcp_registry = mcp_reg
            logger.info("MCP registry initialized on daemon")
        return mcp_reg

    async def _handle_register_server(self, params: dict) -> dict:
        mcp_reg = self._get_mcp_registry()
        try:
            registered = await mcp_reg.register_server(
                server_url=params.get("server_url"),
                headers=params.get("headers"),
                tool_filter=params.get("tool_filter"),
                stdio_cmd=params.get("stdio_cmd"),
                sse_url=params.get("sse_url"),
                post_url=params.get("post_url"),
                env=params.get("env"),
            )
            logger.info("mcp.register_server: registered %d tool(s)", len(registered))
            return {"registered": registered, "count": len(registered)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.register_server failed: %s", e)
            return self._err(str(e))

    async def _handle_list_servers(self, params: dict) -> dict:
        mcp_reg = self._get_mcp_registry()
        servers = mcp_reg.list_servers()
        return {"servers": servers, "count": len(servers)}

    async def _handle_unregister_server(self, params: dict) -> dict:
        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        if not server_url:
            return self._err("server_url is required")
        mcp_reg.unregister_server(server_url)
        logger.info("mcp.unregister_server: %s", server_url)
        return {"status": "ok", "unregistered": server_url}

    async def _handle_list_resources(self, params: dict) -> dict:
        from tools.mcp_tool import MCPRegistry

        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        servers = mcp_reg.list_servers()
        target_key = server_url or next(iter(servers), "")
        if not target_key:
            return {"resources": [], "count": 0}

        transport = MCPRegistry._build_transport(server_url=server_url)
        if transport is None:
            return self._err("could not build transport for server")
        try:
            resources = await transport.list_resources()
            return {"resources": resources, "count": len(resources)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.list_resources failed: %s", e)
            return self._err(str(e))
        finally:
            await transport.close()

    async def _handle_list_prompts(self, params: dict) -> dict:
        from tools.mcp_tool import MCPRegistry

        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        servers = mcp_reg.list_servers()
        target_key = server_url or next(iter(servers), "")
        if not target_key:
            return {"prompts": [], "count": 0}

        transport = MCPRegistry._build_transport(server_url=server_url)
        if transport is None:
            return self._err("could not build transport for server")
        try:
            prompts = await transport.list_prompts()
            return {"prompts": prompts, "count": len(prompts)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.list_prompts failed: %s", e)
            return self._err(str(e))
        finally:
            await transport.close()
