"""MCP Tool Adapter — wraps Model Context Protocol servers as BaseTool instances.

Enables fusion-agent-studio to call MCP-compatible tool servers
(e.g. filesystem, github, postgres) through the standard ToolRegistry.

Supports three transports:
- http:  JSON-RPC 2.0 POST to tools/call (original)
- stdio: spawn MCP server subprocess, JSON-RPC over stdin/stdout (MCP standard)
- sse:   Server-Sent Events stream + POST (MCP streamable HTTP)

Also discovers MCP resources (resources/list) and prompts (prompts/list).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .base import BaseTool

logger = logging.getLogger(__name__)


class MCPTransport:
    """Abstract MCP transport — JSON-RPC 2.0 request/response."""

    async def call_tool(self, name: str, arguments: dict) -> dict:
        raise NotImplementedError

    async def list_tools(self) -> list[dict]:
        raise NotImplementedError

    async def list_resources(self) -> list[dict]:
        raise NotImplementedError

    async def list_prompts(self) -> list[dict]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class HTTPMCPTransport(MCPTransport):
    """HTTP transport — POST JSON-RPC to a remote MCP server."""

    def __init__(self, server_url: str, headers: dict[str, str] | None = None):
        self._server_url = server_url.rstrip("/")
        self._headers = headers or {}

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        import aiohttp

        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(
                self._server_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> list[dict]:
        resp = await self._rpc("tools/list")
        return resp.get("result", {}).get("tools", []) if "result" in resp else resp.get("tools", [])

    async def list_resources(self) -> list[dict]:
        resp = await self._rpc("resources/list")
        return resp.get("result", {}).get("resources", []) if "result" in resp else resp.get("resources", [])

    async def list_prompts(self) -> list[dict]:
        resp = await self._rpc("prompts/list")
        return resp.get("result", {}).get("prompts", []) if "result" in resp else resp.get("prompts", [])


class StdioMCPTransport(MCPTransport):
    """Stdio transport — spawn MCP server subprocess, JSON-RPC over stdin/stdout."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None):
        self._command = command
        self._env = env
        self._proc = None
        self._reader = None
        self._writer = None
        self._id = 0

    async def _ensure_started(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        import asyncio

        logger.info("stdio MCP transport starting: %s", self._command)
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**__import__("os").environ, **(self._env or {})},
        )
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin
        await self._initialize()

    async def _initialize(self) -> dict:
        return await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fusion-agent-studio", "version": "1.0"},
        })

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        import asyncio

        await self._ensure_started()
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        line = json.dumps(payload) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

        raw = await asyncio.wait_for(self._reader.readline(), timeout=30)
        if not raw:
            raise RuntimeError(f"MCP stdio server closed connection (method={method})")
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MCP stdio bad JSON response: {e}") from e

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> list[dict]:
        resp = await self._rpc("tools/list")
        return resp.get("result", {}).get("tools", [])

    async def list_resources(self) -> list[dict]:
        resp = await self._rpc("resources/list")
        return resp.get("result", {}).get("resources", [])

    async def list_prompts(self) -> list[dict]:
        resp = await self._rpc("prompts/list")
        return resp.get("result", {}).get("prompts", [])

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._writer.close()
                await self._proc.wait()
            except Exception as e:
                logger.warning("stdio MCP transport close error: %s", e)
            self._proc = None


class SSEMCPTransport(MCPTransport):
    """SSE transport — Server-Sent Events for server→client, POST for client→server."""

    def __init__(self, sse_url: str, post_url: str, headers: dict[str, str] | None = None):
        self._sse_url = sse_url
        self._post_url = post_url
        self._headers = headers or {}
        self._session = None
        self._id = 0

    async def _post(self, method: str, params: dict | None = None) -> dict:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers)
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        async with self._session.post(
            self._post_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._post("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> list[dict]:
        resp = await self._post("tools/list")
        return resp.get("result", {}).get("tools", [])

    async def list_resources(self) -> list[dict]:
        resp = await self._post("resources/list")
        return resp.get("result", {}).get("resources", [])

    async def list_prompts(self) -> list[dict]:
        resp = await self._post("prompts/list")
        return resp.get("result", {}).get("prompts", [])

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class MCPTool(BaseTool):
    """A BaseTool backed by an MCP server tool.

    Lazily uses its transport on first execute(), then caches results.
    """

    def __init__(
        self,
        tool_name: str,
        tool_description: str = "",
        tool_parameters: dict[str, Any] | None = None,
        transport: MCPTransport | None = None,
        server_url: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.name = tool_name
        self.description = tool_description or f"MCP tool: {tool_name}"
        self.parameters = tool_parameters or {}
        self._transport = transport
        # Legacy HTTP fallback fields (kept for backward compat with from_mcp_discovery)
        self._server_url = server_url.rstrip("/") if server_url else ""
        self._headers = headers or {}

    @classmethod
    def from_mcp_discovery(
        cls,
        server_url: str,
        tool_name: str,
        headers: dict[str, str] | None = None,
    ) -> MCPTool:
        """Create an MCPTool by discovering the tool schema from the MCP server (HTTP)."""
        import urllib.request

        transport = HTTPMCPTransport(server_url, headers=headers)
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
                        tool_name=tool_name,
                        tool_description=t.get("description", ""),
                        tool_parameters=t.get("inputSchema", {}).get("properties", {}),
                        transport=transport,
                        server_url=server_url,
                        headers=headers,
                    )
            logger.warning("MCP tool '%s' not found in discovery from %s", tool_name, server_url)
        except Exception as e:
            logger.warning("MCP discovery failed for %s: %s", server_url, e)

        return cls(
            tool_name=tool_name,
            transport=transport,
            server_url=server_url,
            headers=headers,
        )

    async def execute(self, **kwargs) -> str:
        """Execute the MCP tool via its transport."""
        try:
            if self._transport is not None:
                result = await self._transport.call_tool(self.name, kwargs)
            else:
                result = await self._call_mcp_legacy(kwargs)
            return self._format_result(result)
        except Exception as e:
            logger.error("MCP tool '%s' execution failed: %s", self.name, e)
            return f"Error: MCP tool '{self.name}' failed: {e}"

    @staticmethod
    def _format_result(result: dict | Any) -> str:
        if isinstance(result, dict):
            content = result.get("content", result.get("result", {}).get("content", []))
            if isinstance(content, list):
                texts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(item.get("text", ""))
                if texts:
                    return "\n".join(texts)
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    async def _call_mcp_legacy(self, arguments: dict) -> dict:
        """Low-level MCP call via HTTP POST to tools/call (legacy, no transport)."""
        import aiohttp

        if not self._server_url:
            raise RuntimeError("MCP tool has no transport and no server_url")
        call_url = f"{self._server_url}/tools/call"
        payload = {"name": self.name, "arguments": arguments}
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

    Supports http, stdio, and sse transports.

    Usage:
        registry = create_default_registry()
        mcp = MCPRegistry(registry)
        mcp.register_server("http://localhost:3000")               # http
        mcp.register_server(stdio_cmd=["npx", "mcp-server-fs"])    # stdio
        mcp.register_server(sse_url="http://x/sse",
                            post_url="http://x/rpc")               # sse
    """

    def __init__(self, tool_registry: Any):
        self._registry = tool_registry
        self._servers: dict[str, dict] = {}

    async def register_server(
        self,
        server_url: str | None = None,
        headers: dict[str, str] | None = None,
        tool_filter: list[str] | None = None,
        stdio_cmd: list[str] | None = None,
        sse_url: str | None = None,
        post_url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Discover tools from an MCP server and register them. Returns tool names."""
        transport = self._build_transport(
            server_url=server_url,
            headers=headers,
            stdio_cmd=stdio_cmd,
            sse_url=sse_url,
            post_url=post_url,
            env=env,
        )
        if transport is None:
            logger.error("MCP register_server: no transport configured")
            return []

        registered = []
        try:
            tools_list = await transport.list_tools()
            for t in tools_list:
                name = t.get("name", "")
                if tool_filter and name not in tool_filter:
                    continue
                mcp_tool = MCPTool(
                    tool_name=name,
                    tool_description=t.get("description", ""),
                    tool_parameters=t.get("inputSchema", {}).get("properties", {}),
                    transport=transport,
                    server_url=server_url or "",
                    headers=headers,
                )
                self._registry.register(mcp_tool)
                registered.append(name)
                logger.info("Registered MCP tool: %s", name)

            key = server_url or (sse_url or "") or " ".join(stdio_cmd or [])
            self._servers[key] = {
                "tools": [t.get("name") for t in tools_list],
                "headers": headers,
                "transport": "stdio" if stdio_cmd else ("sse" if sse_url else "http"),
            }
        except Exception as e:
            logger.error("MCP server discovery failed: %s", e)
            await transport.close()

        return registered

    @staticmethod
    def _build_transport(
        server_url: str | None = None,
        headers: dict[str, str] | None = None,
        stdio_cmd: list[str] | None = None,
        sse_url: str | None = None,
        post_url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> MCPTransport | None:
        if stdio_cmd:
            return StdioMCPTransport(command=stdio_cmd, env=env)
        if sse_url and post_url:
            return SSEMCPTransport(sse_url=sse_url, post_url=post_url, headers=headers)
        if server_url:
            return HTTPMCPTransport(server_url=server_url, headers=headers)
        return None

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
        """Return mapping of server keys to their registered tool names."""
        return dict(self._servers)
