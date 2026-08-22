"""Tests for C13 TelemetryEngine runtime instrumentation + OTLP HTTP export
(P1-7, issue #195).

Covers:
1. Runtime instrumentation actually fires start_span/end_span on the 3 hot
   paths (graph.execute / llm.call / tool.call) — counters/latencies non-zero
   after running a graph with telemetry_engine wired.
2. OTLP export path — export(fmt="otlp", push=True) POSTs resourceSpans JSON
   to configured endpoint; failures log-only (no raise).
3. telemetry.export RPC passes push param through to engine.export().
4. Disabled engine is a no-op (instrumentation must not block main path).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch
from urllib.error import URLError

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.telemetry import TelemetryEngine
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo_tool"
    description = "Echo input back"
    parameters = {"input": {"type": "string", "description": "Input"}}

    async def execute(self, **kwargs) -> str:
        return f"echo:{kwargs.get('input', '')}"


class MockMLXClient:
    def __init__(self):
        self.call_count = 0
        self.responses: list[dict] = []

    def add_response(self, content: str, tool_calls: list | None = None,
                     usage: dict | None = None):
        self.responses.append({
            "content": content,
            "tool_calls": tool_calls or [],
            "usage": usage or {"prompt_tokens": 12, "completion_tokens": 8},
        })

    async def chat(self, model, messages, tools=None, temperature=0.7,
                   max_tokens=4096, **kwargs):
        self.call_count += 1
        if not self.responses:
            return LLMResponse(content="done", tool_calls=[],
                               usage={"prompt_tokens": 5, "completion_tokens": 3})
        resp = self.responses.pop(0)
        return LLMResponse(content=resp["content"], tool_calls=resp["tool_calls"],
                           usage=resp["usage"])


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


@pytest.fixture
def mlx_client():
    return MockMLXClient()


@pytest.fixture
def telemetry():
    return TelemetryEngine()


@pytest.fixture
def graph_with_tool():
    g = AgentGraph(name="c13")
    g.add_node("start", NodeConfig(type="start", label="Start"))
    g.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
    g.add_node("end", NodeConfig(type="end", label="End"))
    g.add_edge("start", "llm")
    g.add_edge("llm", "end")
    return g


async def _drain(runtime, graph, text="hi"):
    events = []
    async for ev in runtime.execute_graph(graph, text):
        events.append(ev)
    return events


class TestInstrumentationFires:
    async def test_graph_execute_span_counted(self, mlx_client, tool_registry,
                                              telemetry, graph_with_tool):
        mlx_client.add_response("answer")
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=telemetry)
        await _drain(rt, graph_with_tool)
        m = telemetry.metrics()
        assert m["counters"]["graph_executions"] >= 1
        assert m["counters"]["llm_calls"] >= 1

    async def test_llm_call_tokens_recorded(self, mlx_client, tool_registry,
                                            telemetry, graph_with_tool):
        mlx_client.add_response("answer", usage={"prompt_tokens": 40, "completion_tokens": 20})
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=telemetry)
        await _drain(rt, graph_with_tool)
        m = telemetry.metrics()
        assert m["counters"]["llm_calls"] >= 1
        assert m["counters"]["tokens_prompt"] >= 40
        assert m["counters"]["tokens_completion"] >= 20

    async def test_tool_call_span_counted(self, mlx_client, tool_registry,
                                          telemetry, graph_with_tool):
        mlx_client.add_response("call tool", tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "echo_tool", "arguments": '{"input": "x"}'}},
        ])
        mlx_client.add_response("final")
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=telemetry)
        await _drain(rt, graph_with_tool)
        m = telemetry.metrics()
        assert m["counters"]["tool_calls"] >= 1
        assert m["counters"]["llm_calls"] >= 1

    async def test_latency_populated(self, mlx_client, tool_registry,
                                     telemetry, graph_with_tool):
        mlx_client.add_response("answer")
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=telemetry)
        await _drain(rt, graph_with_tool)
        m = telemetry.metrics()
        assert m["latencies"]["avg_graph_executions_ms"] > 0
        assert m["latencies"]["avg_llm_calls_ms"] >= 0

    async def test_error_span_status_counted(self, mlx_client, tool_registry, telemetry):
        g = AgentGraph(name="err")
        g.add_node("start", NodeConfig(type="start", label="Start"))
        g.add_node("llm", NodeConfig(type="llm", label="LLM", model="bad"))
        g.add_node("end", NodeConfig(type="end", label="End"))
        g.add_edge("start", "llm")
        g.add_edge("llm", "end")

        async def boom(**kwargs):
            raise RuntimeError("boom")

        mlx_client.chat = boom
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=telemetry)
        await _drain(rt, g)
        m = telemetry.metrics()
        assert m["counters"]["errors"] >= 1

    async def test_no_telemetry_engine_no_crash(self, mlx_client, tool_registry,
                                                graph_with_tool):
        mlx_client.add_response("answer")
        rt = AgentRuntime(mlx_client, tool_registry, telemetry_engine=None)
        events = await _drain(rt, graph_with_tool)
        assert any(e.type == AgentEventType.END for e in events)


class TestOTLPExport:
    def test_otlp_payload_structure(self, telemetry):
        s = telemetry.start_span("llm.call", trace_id="t1",
                                 attributes={"prompt_tokens": 10})
        telemetry.end_span(s.span_id)
        payload = telemetry.export("otlp")
        data = json.loads(payload)
        assert "resourceSpans" in data
        assert len(data["resourceSpans"]) >= 1
        rs = data["resourceSpans"][0]
        assert rs["resource"]["attributes"]["service.name"] == "fusion-agent-studio"
        assert len(rs["scopeSpans"][0]["spans"]) >= 1

    def test_otlp_push_no_endpoint_no_raise(self, telemetry):
        telemetry.configure({"enabled": True, "endpoint": ""})
        s = telemetry.start_span("llm.call", trace_id="t1")
        telemetry.end_span(s.span_id)
        payload = telemetry.export("otlp", push=True)
        assert "resourceSpans" in payload

    def test_otlp_push_posts_to_endpoint(self, telemetry):
        telemetry.configure({"enabled": True,
                             "endpoint": "http://otel:4318/v1/traces"})
        s = telemetry.start_span("tool.call", trace_id="t2",
                                 attributes={"tool": "echo"})
        telemetry.end_span(s.span_id)
        captured = {}

        class FakeResp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            telemetry.export("otlp", push=True)
        assert captured["url"] == "http://otel:4318/v1/traces"
        assert captured["method"] == "POST"
        assert b"resourceSpans" in captured["data"]
        assert captured["timeout"] == 5

    def test_otlp_push_failure_logs_only(self, telemetry):
        telemetry.configure({"enabled": True,
                             "endpoint": "http://bad:4318/v1/traces"})
        s = telemetry.start_span("graph.execute", trace_id="t3")
        telemetry.end_span(s.span_id)
        with patch("urllib.request.urlopen",
                   side_effect=URLError("connection refused")):
            payload = telemetry.export("otlp", push=True)
        assert "resourceSpans" in payload


class TestTelemetryExportRPC:
    async def _rpc(self, daemon, method, params=None):
        reader, writer = await asyncio.open_unix_connection(daemon._socket_path,
                                                            limit=2**20)
        req = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params:
            req["params"] = params
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        return json.loads(data)

    @pytest.fixture
    async def daemon(self, tmp_path):
        from agent_runtime.daemon_server import DaemonServer
        sock = str(tmp_path / "d.sock")
        d = DaemonServer(socket_path=sock, ws_port=0, cluster_port=0,
                         http_port=0, store_path=str(tmp_path / "s.db"))
        await d.start()
        d._gateway._default_client = None
        d._gateway._default_model = ""
        yield d
        await d.stop()

    async def test_export_rpc_passes_push_false(self, daemon):
        resp = await self._rpc(daemon, "telemetry.export",
                               {"format": "json", "push": False})
        assert resp["result"]["format"] == "json"
        assert resp["result"]["push"] is False
        assert '"spans"' in resp["result"]["data"]

    async def test_export_rpc_push_true_no_endpoint_no_raise(self, daemon):
        resp = await self._rpc(daemon, "telemetry.export",
                               {"format": "otlp", "push": True})
        assert resp["result"]["push"] is True
        assert "resourceSpans" in resp["result"]["data"]

    async def test_metrics_rpc_after_instrumented_run(self, daemon):
        eng = daemon._get_telemetry_engine()
        s = eng.start_span("llm.call", trace_id="t",
                           attributes={"prompt_tokens": 7, "completion_tokens": 3})
        eng.end_span(s.span_id)
        resp = await self._rpc(daemon, "telemetry.metrics")
        assert resp["result"]["counters"]["llm_calls"] >= 1
        assert resp["result"]["counters"]["tokens_prompt"] >= 7
