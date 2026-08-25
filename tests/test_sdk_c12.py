"""Tests for C12 SDK query() + hook/memory/graph config + Tool daemon registration
(P1-6, issue #193).

Covers:
1. Agent dataclass new fields (hooks/memory/context_window/tools/max_iterations/temperature)
   + to_dict/from_dict round-trip + configure().
2. query() — unified entry: stream=True yields events, stream=False returns result.
3. Agent config application — _apply_config calls agent.configure + hooks.register + memory.store.
4. Tool.to_daemon_dict() — Python handler source serialized via inspect.getsource.
5. Tool.register_to_daemon() — tool.register_python RPC exec'd daemon-side, tool callable.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.sdk import Agent, AgentClient, Tool


async def _rpc_call(socket_path: str, method: str, params: dict | None = None) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store_db(tmp_path):
    yield str(tmp_path / "test_sdk.db")


@pytest.fixture
async def daemon_stub(socket_path, store_db):
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    d._gateway._default_client = None
    d._gateway._default_model = ""
    # 启动时 _attach_mlx_client 若探测到运行中的 fusion-mlx 会 register_model,
    # 使 route() 返回真实 model → chat/chat_stream 连真实服务 (本地 launchd-MLX
    # 在跑时 stream 测试挂起等 chunk). 清空 _models → route 返 None → stub 路径
    # 立即返错, 测试不依赖外部 MLX, 本地+CI 一致.
    d._gateway._models.clear()
    yield d
    await d.stop()


def _client(socket_path) -> AgentClient:
    return AgentClient(socket_path=socket_path)


class TestAgentDataclass:
    def test_new_fields_roundtrip(self):
        a = Agent(
            name="c12",
            hooks=[{"event": "tool.call", "action": "log"}],
            memory={"store": {"content": "x", "scope": "user"}},
            context_window=4096,
            tools=["t1"],
            max_iterations=5,
            temperature=0.3,
        )
        d = a.to_dict()
        assert d["hooks"] == [{"event": "tool.call", "action": "log"}]
        assert d["context_window"] == 4096
        assert d["tools"] == ["t1"]
        assert d["max_iterations"] == 5
        assert d["temperature"] == 0.3
        a2 = Agent.from_dict(d)
        assert a2.hooks == a.hooks
        assert a2.context_window == 4096
        assert a2.tools == ["t1"]

    def test_configure_sets_known_and_metadata(self):
        a = Agent(name="c12")
        a.configure(model="m1", context_window=2048, custom_key="custom_val")
        assert a.model == "m1"
        assert a.context_window == 2048
        assert a.metadata["custom_key"] == "custom_val"

    def test_agent_id_auto_generated(self):
        a = Agent(name="c12")
        assert a.agent_id.startswith("agent_")


class TestQuery:
    @pytest.mark.asyncio
    async def test_query_stream_yields_events(self, daemon_stub, socket_path):
        client = _client(socket_path)
        agent = Agent(name="c12_query_stream", system_prompt="test", model="stub-model")
        events = []
        async for ev in agent.query(client, "hello", stream=True):
            events.append(ev)
        # agent created + execute_stream ran (may be empty events without MLX).
        assert agent.agent_id
        assert agent.graph_id
        assert isinstance(events, list)

    @pytest.mark.asyncio
    async def test_query_non_stream_returns_dict(self, daemon_stub, socket_path):
        client = _client(socket_path)
        agent = Agent(name="c12_query_nostream", system_prompt="test", model="stub-model")
        result = await agent.query(client, "hello", stream=False)
        assert isinstance(result, dict)
        assert agent.agent_id

    @pytest.mark.asyncio
    async def test_query_idempotent_create(self, daemon_stub, socket_path):
        client = _client(socket_path)
        agent = Agent(name="c12_idem", system_prompt="test", model="stub-model")
        await agent.query(client, "first", stream=False)
        first_id = agent.agent_id
        # second query should reuse existing agent_id (not recreate).
        await agent.query(client, "second", stream=False)
        assert agent.agent_id == first_id


class TestAgentConfigApply:
    @pytest.mark.asyncio
    async def test_config_applied_hooks_memory(self, daemon_stub, socket_path):
        client = _client(socket_path)
        agent = Agent(
            name="c12_config",
            system_prompt="test",
            model="stub-model",
            context_window=8192,
            hooks=[{"event": "tool.call", "action": "log"}],
            memory={"store": {"content": "prefers concise", "scope": "user"}},
        )
        await agent.query(client, "hi", stream=False)

        # verify hooks registered via hooks.list RPC.
        hooks_resp = await _rpc_call(socket_path, "hooks.list")
        hooks = hooks_resp.get("result", {}).get("hooks", [])
        assert any(h.get("event") == "tool.call" for h in hooks)

        # verify memory stored via memory.count (at least 1 entry).
        mem_resp = await _rpc_call(socket_path, "memory.count")
        count = mem_resp.get("result", {}).get("count", 0)
        assert count >= 1

    @pytest.mark.asyncio
    async def test_configure_agent_rpc(self, daemon_stub, socket_path):
        client = _client(socket_path)
        agent = Agent(name="c12_cfg_rpc", system_prompt="initial", model="stub-model")
        await agent.query(client, "hi", stream=False)
        result = await client.configure_agent(
            agent.agent_id, {"context_window": 16384, "temperature": 0.5}
        )
        assert "error" not in result
        manifest = result.get("manifest", {})
        assert manifest.get("context_window") == 16384


class TestToolDaemonRegister:
    def test_to_daemon_dict_serializes_handler_source(self):
        def my_handler(query: str) -> str:
            return f"result: {query}"

        t = Tool(
            name="my_tool",
            description="demo",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=my_handler,
        )
        d = t.to_daemon_dict()
        assert d["name"] == "my_tool"
        assert d["type"] == "python"
        assert d["handler_name"] == "my_handler"
        assert "def my_handler" in d["source"]

    def test_to_daemon_dict_no_handler_terminal(self):
        t = Tool(name="schema_only", description="d", parameters={})
        d = t.to_daemon_dict()
        assert d["type"] == "terminal"
        assert "source" not in d

    @pytest.mark.asyncio
    async def test_register_to_daemon_python(self, daemon_stub, socket_path):
        def echo_tool(text: str) -> str:
            return f"echo: {text}"

        client = _client(socket_path)
        tool = Tool(
            name="sdk_echo",
            description="Echo back text",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=echo_tool,
        )
        result = await tool.register_to_daemon(client)
        assert result.get("status") == "ok", result
        assert result.get("kind") == "python"

        # verify tool is in the dynamic registry and executable.
        assert daemon_stub._dynamic_registry.has("sdk_echo")
        reg_tool = daemon_stub._dynamic_registry.get("sdk_echo")
        out = await reg_tool.execute(text="hello")
        assert out == "echo: hello"

    @pytest.mark.asyncio
    async def test_register_python_async_handler(self, daemon_stub, socket_path):
        async def async_tool(value: int) -> int:
            return value * 2

        client = _client(socket_path)
        tool = Tool(
            name="sdk_doubler",
            description="Double a number",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            handler=async_tool,
        )
        result = await tool.register_to_daemon(client)
        assert result.get("status") == "ok"
        reg_tool = daemon_stub._dynamic_registry.get("sdk_doubler")
        out = await reg_tool.execute(value=21)
        assert out == 42

    @pytest.mark.asyncio
    async def test_register_python_rpc_direct(self, daemon_stub, socket_path):
        # direct RPC call to tool.register_python (bypass SDK Tool).
        handler_src = "def adder(a, b):\n    return a + b\n"
        resp = await _rpc_call(
            socket_path,
            "tool.register_python",
            {
                "name": "sdk_adder",
                "description": "add two numbers",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                },
                "source": handler_src,
                "handler_name": "adder",
            },
        )
        result = resp.get("result", {})
        assert result.get("status") == "ok"
        assert result.get("kind") == "python"
        reg_tool = daemon_stub._dynamic_registry.get("sdk_adder")
        out = await reg_tool.execute(a=3, b=4)
        assert out == 7

    @pytest.mark.asyncio
    async def test_register_python_bad_source(self, daemon_stub, socket_path):
        resp = await _rpc_call(
            socket_path,
            "tool.register_python",
            {"name": "sdk_bad", "source": "def broken(", "handler_name": "broken"},
        )
        result = resp.get("result", {})
        assert result.get("status") == "error"
        assert "exec failed" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_register_python_missing_handler(self, daemon_stub, socket_path):
        resp = await _rpc_call(
            socket_path,
            "tool.register_python",
            {
                "name": "sdk_nothandler",
                "source": "x = 1\n",
                "handler_name": "nonexistent",
            },
        )
        result = resp.get("result", {})
        assert result.get("status") == "error"
        assert "not found" in result.get("message", "")
