"""MCP Tool Adapter — wraps Model Context Protocol servers as BaseTool instances.

Enables fusion-agent-studio to call MCP-compatible tool servers
(e.g. filesystem, github, postgres) through the standard ToolRegistry.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .base import BaseTool

logger = logging.getLogger(__name__)


class MCPTool(BaseTool):
    """A BaseTool backed by an MCP server tool.

    Lazily connects to the MCP server on first execute(), then caches
    the session for subsequent calls.
    """

    def __init__(
        self,
        server_url: str,
        tool_name: str,
        tool_description: str = "",
        tool_parameters: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.name = tool_name
        self.description = tool_description or f"MCP tool: {tool_name}"
        self.parameters = tool_parameters or {}
        self._server_url = server_url.rstrip("/")
        self._headers = headers or {}
        self._client = None

    @classmethod
    def from_mcp_discovery(
        cls,
        server_url: str,
        tool_name: str,
        headers: dict[str, str] | None = None,
    ) -> MCPTool:
        """Create an MCPTool by discovering the tool schema from the MCP server.

        Calls the server's tools/list endpoint to get schema details.
        """
        import urllib.request

        discovery_url = f"{server_url.rstrip('/')}/tools/list"
        req = urllib.request.Request(
            discovery_url,
            method="GET",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            tools_list = data.get("tools", [])
            for t in tools_list:
                if t.get("name") == tool_name:
                    return cls(
                        server_url=server_url,
                        tool_name=tool_name,
                        tool_description=t.get("description", ""),
                        tool_parameters=t.get("inputSchema", {}).get("properties", {}),
                        headers=headers,
                    )
            logger.warning("MCP tool '%s' not found in discovery from %s", tool_name, server_url)
        except Exception as e:
            logger.warning("MCP discovery failed for %s: %s", server_url, e)

        return cls(
            server_url=server_url,
            tool_name=tool_name,
            headers=headers,
        )

    async def execute(self, **kwargs) -> str:
        """Execute the MCP tool by calling the server's tools/call endpoint."""
        try:
            result = await self._call_mcp(kwargs)
            if isinstance(result, dict):
                content = result.get("content", [])
                texts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(item.get("text", ""))
                if texts:
                    return "\n".join(texts)
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except Exception as e:
            logger.error("MCP tool '%s' execution failed: %s", self.name, e)
            return f"Error: MCP tool '{self.name}' failed: {e}"

    async def _call_mcp(self, arguments: dict) -> dict:
        """Low-level MCP call via HTTP POST to tools/call."""
        import aiohttp

        call_url = f"{self._server_url}/tools/call"
        payload = {
            "name": self.name,
            "arguments": arguments,
        }

        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(
                call_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    def openai_schema(self) -> dict:
        """Generate OpenAI function-calling schema from MCP tool parameters."""
        properties = {}
        required = []
        for param_name, param_def in self.parameters.items():
            properties[param_name] = {
                "type": param_def.get("type", "string"),
                "description": param_def.get("description", ""),
            }
            if param_def.get("required", False):
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class MCPRegistry:
    """Discovers and registers MCP server tools into a ToolRegistry.

    Usage:
        registry = create_default_registry()
        mcp = MCPRegistry(registry)
        mcp.register_server("http://localhost:3000")
        mcp.register_server("http://localhost:3001", headers={"Authorization": "Bearer x"})
    """

    def __init__(self, tool_registry: Any):
        self._registry = tool_registry
        self._servers: dict[str, dict] = {}

    def register_server(
        self,
        server_url: str,
        headers: dict[str, str] | None = None,
        tool_filter: list[str] | None = None,
    ) -> list[str]:
        """Discover tools from an MCP server and register them.

        Returns list of registered tool names.
        """
        import urllib.request

        discovery_url = f"{server_url.rstrip('/')}/tools/list"
        req = urllib.request.Request(
            discovery_url,
            method="GET",
            headers={"Content-Type": "application/json", **(headers or {})},
        )

        registered = []
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            tools_list = data.get("tools", [])
            for t in tools_list:
                name = t.get("name", "")
                if tool_filter and name not in tool_filter:
                    continue
                mcp_tool = MCPTool(
                    server_url=server_url,
                    tool_name=name,
                    tool_description=t.get("description", ""),
                    tool_parameters=t.get("inputSchema", {}).get("properties", {}),
                    headers=headers,
                )
                self._registry.register(mcp_tool)
                registered.append(name)
                logger.info("Registered MCP tool: %s from %s", name, server_url)

            self._servers[server_url] = {
                "tools": [t.get("name") for t in tools_list],
                "headers": headers,
            }
        except Exception as e:
            logger.error("MCP server discovery failed for %s: %s", server_url, e)

        return registered

    def unregister_server(self, server_url: str) -> None:
        """Remove all tools from a specific MCP server."""
        server_info = self._servers.pop(server_url, None)
        if not server_info:
            return
        for tool_name in server_info.get("tools", []):
            try:
                self._registry.unregister(tool_name)
                logger.info("Unregistered MCP tool: %s", tool_name)
            except Exception as e:
                logger.warning("Failed to unregister MCP tool %s: %s", tool_name, e)

    def list_servers(self) -> dict[str, list[str]]:
        """Return mapping of server URLs to their registered tool names."""
        return dict(self._servers)
