"""Tests for C9 true streaming — SSE endpoint + WS stream path + execute_graph_stream.

Verifies the boundary layers no longer buffer: WS pushes per-token TOKEN
events, SSE returns text/event-stream with per-event data lines, and the
agent.execute_stream RPC yields TOKEN events (not a single THINK).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_runtime import api_server
from agent_runtime.api_server import app
from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import StreamChunk
from tools.base import BaseTool
from tools.registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo input"
    parameters = {"text": {"type": "string", "description": "text"}}

    async def execute(self, text: str = "", **kwargs) -> str:
        return text


class MockStreamClient:
    def __init__(self):
        self._StreamChunk = StreamChunk

    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in [
            StreamChunk(delta_content="Hel", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="lo", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content=" world", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop"),
        ]:
            yield chunk

    async def chat(self, model, messages, tools=None, **kwargs):
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="Hello world",
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            finish_reason="stop",
        )

    async def embeddings(self, model, input, **kwargs):
        if isinstance(input, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in input]


def _make_runtime():
    client = MockStreamClient()
    gateway = LLMGateway()
    gateway.set_default_client(client)
    gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRuntime(llm_gateway=gateway, tool_registry=registry)


def _make_graph():
    g = AgentGraph(name="c9_stream_test", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test-model"))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "end")
    return g


@pytest.fixture
def patched_api(tmp_path, monkeypatch):
    store = AgentStore(db_path=tmp_path / "store.db")
    graph = _make_graph()
    store.save_graph(graph)
    runtime = _make_runtime()
    monkeypatch.setattr(api_server, "_store", store)
    monkeypatch.setattr(api_server, "_runtime", runtime)
    monkeypatch.setattr(api_server, "_daemon", None)
    return {"store": store, "runtime": runtime, "graph": graph}


# ---------------------------------------------------------------------------
# Runtime-level: execute_graph_stream yields TOKEN events
# ---------------------------------------------------------------------------

class TestRuntimeStream:

    @pytest.mark.asyncio
    async def test_execute_graph_stream_yields_tokens(self):
        rt = _make_runtime()
        g = _make_graph()
        events = []
        async for event in rt.execute_graph_stream(g, "hi"):
            events.append(event)
        token_types = [e.type for e in events if e.type == AgentEventType.TOKEN]
        assert len(token_types) >= 2

    @pytest.mark.asyncio
    async def test_execute_graph_non_stream_no_tokens(self):
        rt = _make_runtime()
        g = _make_graph()
        events = []
        async for event in rt.execute_graph(g, "hi"):
            events.append(event)
        assert not any(e.type == AgentEventType.TOKEN for e in events)


# ---------------------------------------------------------------------------
# SSE endpoint — real streaming, text/event-stream
# ---------------------------------------------------------------------------

class TestSSEEndpoint:

    def test_sse_returns_event_stream(self, patched_api):
        graph = patched_api["graph"]
        client = TestClient(app)
        with client.stream("GET", f"/v1/graphs/{graph.id}/execute/stream?input=hi") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
        text = body.decode("utf-8")
        # SSE data lines
        assert "data: " in text
        lines = [l[len("data: "):] for l in text.splitlines() if l.startswith("data: ")]
        assert lines, "no SSE data lines received"
        events = [json.loads(l) for l in lines]
        types = [e.get("type") for e in events]
        # per-token TOKEN events present (not a single buffered THINK)
        assert "token" in types, f"no token event in {types}"
        assert "done" in types

    def test_sse_missing_graph_404(self, patched_api, monkeypatch):
        client = TestClient(app)
        resp = client.get("/v1/graphs/nonexistent-graph/execute/stream?input=hi")
        assert resp.status_code == 404

    def test_sse_emits_multiple_token_events(self, patched_api):
        graph = patched_api["graph"]
        client = TestClient(app)
        with client.stream("GET", f"/v1/graphs/{graph.id}/execute/stream?input=hi") as resp:
            body = b"".join(resp.iter_bytes())
        lines = [l[len("data: "):] for l in body.decode().splitlines() if l.startswith("data: ")]
        events = [json.loads(l) for l in lines]
        token_events = [e for e in events if e.get("type") == "token"]
        assert len(token_events) >= 3, f"expected >=3 token chunks, got {len(token_events)}"


# ---------------------------------------------------------------------------
# WS endpoint — now connects execute_graph_stream, pushes TOKEN events
# ---------------------------------------------------------------------------

class TestWSEndpoint:

    def test_ws_pushes_token_events(self, patched_api):
        graph = patched_api["graph"]
        client = TestClient(app)
        with client.websocket_connect(f"/ws/execute/{graph.id}") as ws:
            ws.send_json({"input": "hi"})
            events = []
            while True:
                msg = ws.receive_json()
                events.append(msg)
                if msg.get("type") == "done":
                    break
        types = [e.get("type") for e in events]
        assert "token" in types, f"WS pushed no token event: {types}"
        assert "done" in types

    def test_ws_missing_graph_sends_error(self, patched_api):
        client = TestClient(app)
        with client.websocket_connect("/ws/execute/nonexistent-graph") as ws:
            msg = ws.receive_json()
        assert msg.get("type") == "error"


# ---------------------------------------------------------------------------
# agent.execute_stream RPC — now uses execute_graph_stream (TOKEN events)
# ---------------------------------------------------------------------------

class TestAgentExecuteStreamRPC:

    @pytest.mark.asyncio
    async def test_execute_stream_rpc_yields_token_events(self, tmp_path, monkeypatch):
        from agent_runtime.dispatchers.agent import AgentDispatcher

        # Build a minimal fake daemon exposing _agent_dir + _get_runtime + store
        rt = _make_runtime()
        store = AgentStore(db_path=tmp_path / "store.db")

        # minimal agent package on disk — handler builds graph from manifest.
        # AgentPackage stores content under base_path/.fusion-agent/.
        agent_dir = tmp_path / "agent-stream"
        pkg_dir = agent_dir / ".fusion-agent"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "manifest.json").write_text(json.dumps({
            "name": "stream-agent",
            "system_prompt": "you are helpful",
            "model": "test-model",
            "tools": [],
        }))

        class FakeDaemon:
            def _agent_dir(self, agent_id):
                return agent_dir

            def _get_runtime(self):
                return rt

            def _inject_knowledge_context(self, *a, **k):
                async def _nope(*a, **k):
                    return ""
                return _nope()

            def _get_style_manager(self):
                class _SM:
                    def apply(self, prompt, style):
                        return {"system_prompt": prompt}
                return _SM()

        # AgentPackage.to_graph_config reads graph.json; save_graph needs store attr
        daemon = FakeDaemon()
        daemon.store = store
        disp = AgentDispatcher(daemon)
        result = await disp._handle_agent_execute_stream({
            "agent_id": "stream-agent",
            "input": "hi",
        })
        assert result["status"] == "completed"
        types = [e.get("type") for e in result["events"]]
        assert "token" in types, f"RPC execute_stream emitted no token: {types}"
