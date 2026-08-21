"""Tests for inbound MCP — transport (http/stdio/sse), MCPRegistry, McpDispatcher RPC.

Spins up ephemeral in-process MCP servers (aiohttp.web for http/sse, a python
subprocess for stdio) to exercise real JSON-RPC flows. Process data cleaned up
after tests — only final outputs kept.
"""
from __future__ import annotations

import sys

import pytest
from aiohttp import web

from tools import MCPRegistry, MCPTool, ToolRegistry
from tools.mcp_tool import (
    HTTPMCPTransport,
    SSEMCPTransport,
    StdioMCPTransport,
)

# ---------------------------------------------------------------------------
# In-process HTTP MCP server fixture
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the input text",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
        },
    },
]

MCP_RESOURCES = [{"uri": "file:///foo.txt", "name": "foo", "mimeType": "text/plain"}]
MCP_PROMPTS = [{"name": "greet", "description": "Greet someone"}]


async def _mcp_handler(request: web.Request):
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id", 1)
    if method == "tools/list":
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}})
    if method == "tools/call":
        params = body.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        if name == "echo":
            text = args.get("text", "")
            return web.json_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"echo:{text}"}]},
            })
        if name == "add":
            return web.json_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]},
            })
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"message": f"unknown tool {name}"}})
    if method == "resources/list":
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"resources": MCP_RESOURCES}})
    if method == "prompts/list":
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {"prompts": MCP_PROMPTS}})
    if method == "initialize":
        return web.json_response({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
        })
    return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"message": f"unknown method {method}"}})


@pytest.fixture
async def mcp_http_server():
    app = web.Application()
    app.router.add_post("/rpc", _mcp_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}/rpc"
    yield base_url
    await runner.cleanup()


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

class TestHTTPMCPTransport:

    @pytest.mark.asyncio
    async def test_list_tools(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        tools = await transport.list_tools()
        names = [t["name"] for t in tools]
        assert "echo" in names and "add" in names

    @pytest.mark.asyncio
    async def test_call_tool(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        result = await transport.call_tool("echo", {"text": "hello"})
        assert result["result"]["content"][0]["text"] == "echo:hello"

    @pytest.mark.asyncio
    async def test_list_resources(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        resources = await transport.list_resources()
        assert len(resources) == 1
        assert resources[0]["uri"] == "file:///foo.txt"

    @pytest.mark.asyncio
    async def test_list_prompts(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        prompts = await transport.list_prompts()
        assert prompts[0]["name"] == "greet"


# ---------------------------------------------------------------------------
# MCPTool
# ---------------------------------------------------------------------------

class TestMCPTool:

    @pytest.mark.asyncio
    async def test_execute_via_transport(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        tool = MCPTool(
            tool_name="echo",
            tool_description="Echo",
            tool_parameters={"text": {"type": "string"}},
            transport=transport,
        )
        out = await tool.execute(text="world")
        assert out == "echo:world"

    @pytest.mark.asyncio
    async def test_execute_error_is_prefixed(self, mcp_http_server):
        # bad server_url -> connection refused -> Error: prefix
        tool = MCPTool(
            tool_name="echo",
            tool_parameters={"text": {"type": "string"}},
            server_url="http://127.0.0.1:1/rpc",
        )
        out = await tool.execute(text="x")
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, mcp_http_server):
        transport = HTTPMCPTransport(mcp_http_server)
        tool = MCPTool(tool_name="nope", transport=transport)
        out = await tool.execute()
        # server returns error response -> content empty -> JSON dump
        assert out  # non-empty

    def test_openai_schema(self):
        tool = MCPTool(
            tool_name="echo",
            tool_description="Echo tool",
            tool_parameters={"text": {"type": "string", "required": True}},
        )
        schema = tool.openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert "text" in schema["function"]["parameters"]["properties"]
        assert "text" in schema["function"]["parameters"]["required"]


# ---------------------------------------------------------------------------
# MCPRegistry
# ---------------------------------------------------------------------------

class TestMCPRegistry:

    @pytest.mark.asyncio
    async def test_register_server_http(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        registered = await mcp.register_server(server_url=mcp_http_server)
        assert sorted(registered) == ["add", "echo"]
        assert registry.has("echo")
        assert registry.has("add")

    @pytest.mark.asyncio
    async def test_register_server_with_filter(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        registered = await mcp.register_server(server_url=mcp_http_server, tool_filter=["echo"])
        assert registered == ["echo"]
        assert registry.has("echo")
        assert not registry.has("add")

    @pytest.mark.asyncio
    async def test_list_servers(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        await mcp.register_server(server_url=mcp_http_server)
        servers = mcp.list_servers()
        assert mcp_http_server in servers
        assert sorted(servers[mcp_http_server]["tools"]) == ["add", "echo"]

    @pytest.mark.asyncio
    async def test_unregister_server(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        await mcp.register_server(server_url=mcp_http_server)
        assert registry.has("echo")
        mcp.unregister_server(mcp_http_server)
        assert not registry.has("echo")
        assert not registry.has("add")

    @pytest.mark.asyncio
    async def test_register_server_no_transport_returns_empty(self):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        registered = await mcp.register_server()
        assert registered == []

    @pytest.mark.asyncio
    async def test_registered_tool_executable(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        await mcp.register_server(server_url=mcp_http_server)
        tool = registry.get("echo")
        out = await tool.execute(text="roundtrip")
        assert out == "echo:roundtrip"


# ---------------------------------------------------------------------------
# Stdio transport — python subprocess acting as an MCP server
# ---------------------------------------------------------------------------

STDIO_SERVER_SCRIPT = r'''
import json, sys

def respond(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method", "")
    rid = msg.get("id", 1)
    if method == "initialize":
        respond(rid, {"protocolVersion": "2024-11-05", "capabilities": {}})
    elif method == "tools/list":
        respond(rid, {"tools": [{"name": "stdio_echo", "description": "echo via stdio",
            "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}}]})
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        respond(rid, {"content": [{"type": "text", "text": "stdio:" + args.get("msg", "")}]})
    elif method == "resources/list":
        respond(rid, {"resources": [{"uri": "stdio://res", "name": "res"}]})
    elif method == "prompts/list":
        respond(rid, {"prompts": [{"name": "stdio_prompt"}]})
    else:
        respond(rid, {})
'''


class TestStdioMCPTransport:

    @pytest.mark.asyncio
    async def test_stdio_list_tools_and_call(self, tmp_path):
        script = tmp_path / "stdio_mcp_server.py"
        script.write_text(STDIO_SERVER_SCRIPT)
        transport = StdioMCPTransport(command=[sys.executable, str(script)])
        try:
            tools = await transport.list_tools()
            assert tools[0]["name"] == "stdio_echo"
            result = await transport.call_tool("stdio_echo", {"msg": "hi"})
            assert result["result"]["content"][0]["text"] == "stdio:hi"
            resources = await transport.list_resources()
            assert resources[0]["uri"] == "stdio://res"
            prompts = await transport.list_prompts()
            assert prompts[0]["name"] == "stdio_prompt"
        finally:
            await transport.close()

    @pytest.mark.asyncio
    async def test_stdio_registry_register(self, tmp_path):
        script = tmp_path / "stdio_mcp_server.py"
        script.write_text(STDIO_SERVER_SCRIPT)
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        registered = await mcp.register_server(stdio_cmd=[sys.executable, str(script)])
        assert "stdio_echo" in registered
        assert registry.has("stdio_echo")
        tool = registry.get("stdio_echo")
        out = await tool.execute(msg="ping")
        assert out == "stdio:ping"


# ---------------------------------------------------------------------------
# SSE transport — reuses the same http server (POST endpoint works for both)
# ---------------------------------------------------------------------------

class TestSSEMCPTransport:

    @pytest.mark.asyncio
    async def test_sse_list_and_call(self, mcp_http_server):
        # SSE transport uses post_url for JSON-RPC; same /rpc endpoint works
        transport = SSEMCPTransport(sse_url=mcp_http_server, post_url=mcp_http_server)
        try:
            tools = await transport.list_tools()
            assert "echo" in [t["name"] for t in tools]
            result = await transport.call_tool("add", {"a": 3, "b": 4})
            assert result["result"]["content"][0]["text"] == "7"
        finally:
            await transport.close()

    @pytest.mark.asyncio
    async def test_sse_registry_register(self, mcp_http_server):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        registered = await mcp.register_server(sse_url=mcp_http_server, post_url=mcp_http_server)
        assert "echo" in registered
        assert registry.has("echo")


# ---------------------------------------------------------------------------
# McpDispatcher RPC integration
# ---------------------------------------------------------------------------

class _FakeRuntime:
    def __init__(self):
        self.tool_registry = ToolRegistry()


class _FakeDaemon:
    def __init__(self):
        self._runtime = _FakeRuntime()
        self._mcp_registry = None

    def _get_runtime(self):
        return self._runtime


class TestMcpDispatcher:

    @pytest.mark.asyncio
    async def test_register_then_list_servers(self, mcp_http_server):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        handlers = disp.get_handlers()
        assert "mcp.register_server" in handlers
        assert "mcp.list_servers" in handlers
        assert "mcp.unregister_server" in handlers
        assert "mcp.list_resources" in handlers
        assert "mcp.list_prompts" in handlers

        result = await disp._handle_register_server({"server_url": mcp_http_server})
        assert result["count"] == 2
        assert daemon._runtime.tool_registry.has("echo")

        listed = await disp._handle_list_servers({})
        assert mcp_http_server in listed["servers"]
        assert listed["count"] == 1

    @pytest.mark.asyncio
    async def test_register_missing_server_returns_empty(self):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        result = await disp._handle_register_server({})
        assert result["count"] == 0
        assert result["registered"] == []

    @pytest.mark.asyncio
    async def test_unregister_server(self, mcp_http_server):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        await disp._handle_register_server({"server_url": mcp_http_server})
        assert daemon._runtime.tool_registry.has("echo")
        out = await disp._handle_unregister_server({"server_url": mcp_http_server})
        assert out["status"] == "ok"
        assert not daemon._runtime.tool_registry.has("echo")

    @pytest.mark.asyncio
    async def test_unregister_missing_url_errors(self):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        out = await disp._handle_unregister_server({})
        assert "error" in out

    @pytest.mark.asyncio
    async def test_list_resources_and_prompts(self, mcp_http_server):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        await disp._handle_register_server({"server_url": mcp_http_server})
        res = await disp._handle_list_resources({"server_url": mcp_http_server})
        assert res["count"] == 1
        assert res["resources"][0]["uri"] == "file:///foo.txt"
        prompts = await disp._handle_list_prompts({"server_url": mcp_http_server})
        assert prompts["count"] == 1
        assert prompts["prompts"][0]["name"] == "greet"

    @pytest.mark.asyncio
    async def test_list_resources_empty_when_no_servers(self):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        daemon = _FakeDaemon()
        disp = McpDispatcher(daemon)
        res = await disp._handle_list_resources({})
        assert res == {"resources": [], "count": 0}
