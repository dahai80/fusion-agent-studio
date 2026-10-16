import asyncio
import hashlib
import json

import pytest

from agent_runtime.agent_marketplace import AgentMarketplace, MarketEntry
from agent_runtime.mcp_server import MCPServer, create_mlx_registry
from agent_runtime.protocol_plane import ProtocolArtifact, ProtocolPlane
from tools.base import BaseTool
from tools.registry import ToolRegistry


class EchoTool(BaseTool):
    def __init__(self):
        self.name = "echo"
        self.description = "echo back"
        self.parameters = {"text": {"type": "string"}}
        self.readonly = True

    async def execute(self, **kwargs) -> str:
        return kwargs.get("text", "")


@pytest.fixture()
def registry():
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


def _req(server, method, params=None, req_id=1):
    return asyncio.run(server._handle_request({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))


def test_mcp_initialize(registry):
    server = MCPServer(registry)
    resp = _req(server, "initialize", {})
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "fusion-agent-studio"


def test_mcp_tools_list(registry):
    server = MCPServer(registry)
    resp = _req(server, "tools/list")
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "echo"
    assert tools[0]["x-fusion-origin"] == "fusion-agent-studio"
    assert tools[0]["inputSchema"]["type"] == "object"


def test_mcp_tools_call(registry):
    server = MCPServer(registry)
    resp = _req(server, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    assert resp["result"]["content"][0]["text"] == "hi"
    assert resp["result"]["isError"] is False


def test_mcp_unknown_tool_and_method(registry):
    server = MCPServer(registry)
    resp = _req(server, "tools/call", {"name": "nope"})
    assert resp["error"]["code"] == -32602
    resp2 = _req(server, "bogus/method")
    assert resp2["error"]["code"] == -32601
    assert _req(server, "notifications/initialized") is None


def test_mlx_registry_builds():
    reg = create_mlx_registry()
    names = [m["name"] for m in reg.list_tools()]
    assert "mlx_chat" in names and "mlx_status" in names
    assert reg.get("mlx_status").readonly is True


def test_protocol_plane_register_route():
    plane = ProtocolPlane()
    plane.register("mcp", "srv_tool", origin="http://x")
    plane.register("skill", "pdf-extract")
    plane.register("plugin", "my-plugin", extensions={"x-fusion-gui": True})
    assert len(plane.list()) == 3
    assert len(plane.list(kind="mcp")) == 1
    route = plane.governance_route("mcp", "srv_tool")
    assert route["registered"] is True
    assert route["chain"] == ["hooks", "permission", "budget"]
    unroute = plane.governance_route("mcp", "ghost")
    assert unroute["registered"] is False
    assert plane.unregister("mcp", "srv_tool") is True


def test_protocol_artifact_kind_validation():
    with pytest.raises(ValueError):
        ProtocolArtifact(kind="bogus", name="x")


def test_marketplace_signature_roundtrip(tmp_path):
    mp = AgentMarketplace(store_dir=tmp_path / "mkt")
    entry = MarketEntry(id="e1", name="Demo", graph_data={"nodes": [{"id": "a"}]})
    mp.publish(entry)
    assert entry.sha256
    expected = hashlib.sha256(json.dumps(entry.graph_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert entry.sha256 == expected
    out = mp.install("e1", target_dir=tmp_path / "inst")
    assert out is not None


def test_marketplace_tamper_refused(tmp_path):
    mp = AgentMarketplace(store_dir=tmp_path / "mkt2")
    entry = MarketEntry(id="e2", name="Tamper", graph_data={"nodes": []})
    mp.publish(entry)
    entry.graph_data["nodes"].append({"id": "evil"})
    out = mp.install("e2", target_dir=tmp_path / "inst2")
    assert out is None


def test_protocol_rpc(tmp_path):
    from agent_runtime.dispatchers.mcp import McpDispatcher

    class _Stub:
        pass

    d = McpDispatcher(_Stub())
    out = asyncio.run(d._handle_protocol_register({"kind": "skill", "name": "s1"}))
    assert out["kind"] == "skill"
    lst = asyncio.run(d._handle_protocol_list({}))
    assert lst["artifacts"][0]["name"] == "s1"
    route = asyncio.run(d._handle_protocol_route({"kind": "skill", "name": "s1"}))
    assert route["chain"] == ["hooks", "permission", "budget"]
    err = asyncio.run(d._handle_protocol_register({"kind": "skill", "name": ""}))
    assert "error" in err
    err2 = asyncio.run(d._handle_protocol_register({"kind": "nope", "name": "x"}))
    assert "error" in err2
