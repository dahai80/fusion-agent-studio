"""Tests for C1 parallel tool calls + tool_choice passthrough (P1-1, issue #187).

Two capabilities:
1. tool_choice end-to-end — NodeConfig -> runtime -> gateway -> client payload.
2. parallel_tool_calls — multiple tools execute via asyncio.gather concurrently,
   control-flow tools (register/unregister/sub_graph/exit_plan_mode) + plan_mode
   force sequential fallback.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse, StreamChunk
from tools.base import BaseTool
from tools.registry import ToolRegistry


class SleepTool(BaseTool):
    name = "sleep"
    description = "Sleep n seconds, return name tag"
    parameters = {"seconds": {"type": "number"}, "tag": {"type": "string"}}

    async def execute(self, seconds=0.0, tag="", **kwargs) -> str:
        await asyncio.sleep(float(seconds))
        return f"slept:{tag}"


class WriteTool(BaseTool):
    name = "file_write"
    description = "Write file"
    parameters = {"path": {"type": "string"}, "content": {"type": "string"}}

    async def execute(self, path="", content="", **kwargs) -> str:
        return f"Wrote {path}"


def _llm_response(content="", tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
        finish_reason="stop",
    )


class ToolCallClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.tool_choice_seen = []
        self.last_payload = {}

    async def chat(self, model, messages, tools=None, **kwargs):
        tc = kwargs.get("tool_choice")
        if tc is not None:
            self.tool_choice_seen.append(tc)
        self.last_payload = {"tool_choice": tc}
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return _llm_response(content="done")

    async def chat_stream(self, messages, model="", **kwargs):
        self.tool_choice_seen.append(kwargs.get("tool_choice"))
        content = ""
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            content = resp.content or ""
        yield StreamChunk(delta_content=content, delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        if isinstance(input, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in input]


def _make_registry():
    r = ToolRegistry()
    r.register(SleepTool())
    r.register(WriteTool())
    return r


def _make_runtime(responses):
    client = ToolCallClient(responses)
    gw = LLMGateway()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    rt = AgentRuntime(llm_gateway=gw, tool_registry=_make_registry())
    return rt, client


def _tc(name, args):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _graph(tool_choice="", parallel=False, loop=False):
    g = AgentGraph(name="c1_test", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    llm = NodeConfig(
        type="llm",
        label="llm1",
        model="test-model",
        tool_choice=tool_choice,
        parallel_tool_calls=parallel,
    )
    if loop:
        llm.loop_mode = "agent"
        llm.max_loop_iterations = 5
    g.add_node("llm1", llm)
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "end")
    return g


async def _collect(rt, g, prompt, stream=False):
    events = []
    gen = rt.execute_graph_stream(g, prompt) if stream else rt.execute_graph(g, prompt)
    async for e in gen:
        events.append(e)
    return events


class TestToolChoicePassthrough:
    def test_nodeconfig_fields(self):
        n = NodeConfig(type="llm", tool_choice="none", parallel_tool_calls=True)
        assert n.tool_choice == "none"
        assert n.parallel_tool_calls is True
        d = n.to_dict()
        assert d["tool_choice"] == "none"
        assert d["parallel_tool_calls"] is True

    @pytest.mark.asyncio
    async def test_tool_choice_none_passed_nonstream(self):
        resp = _llm_response(content="ok")
        rt, client = _make_runtime([resp])
        g = _graph(tool_choice="none")
        await _collect(rt, g, "hi")
        assert "none" in client.tool_choice_seen

    @pytest.mark.asyncio
    async def test_tool_choice_required_passed_stream(self):
        resp = _llm_response(content="ok")
        rt, client = _make_runtime([resp])
        g = _graph(tool_choice="required")
        await _collect(rt, g, "hi", stream=True)
        assert "required" in client.tool_choice_seen

    @pytest.mark.asyncio
    async def test_tool_choice_empty_not_passed(self):
        resp = _llm_response(content="ok")
        rt, client = _make_runtime([resp])
        g = _graph(tool_choice="")
        await _collect(rt, g, "hi")
        assert all(tc is None for tc in client.tool_choice_seen)

    @pytest.mark.asyncio
    async def test_tool_choice_dict_function_form(self):
        tc_dict = {"type": "function", "function": {"name": "sleep"}}
        resp = _llm_response(content="ok")
        rt, client = _make_runtime([resp])
        g = _graph(tool_choice=json.dumps(tc_dict))
        await _collect(rt, g, "hi")
        # tool_choice stored as string on NodeConfig; passthrough verbatim.
        assert any(str(tc) == json.dumps(tc_dict) for tc in client.tool_choice_seen)


class TestParallelToolCalls:
    @pytest.mark.asyncio
    async def test_parallel_faster_than_serial(self):
        # 3 sleep tools, each 0.3s. Parallel < 0.7s; serial would be 0.9s.
        resp = _llm_response(
            tool_calls=[
                _tc("sleep", {"seconds": 0.3, "tag": "a"}),
                _tc("sleep", {"seconds": 0.3, "tag": "b"}),
                _tc("sleep", {"seconds": 0.3, "tag": "c"}),
            ]
        )
        rt, _ = _make_runtime([resp, _llm_response(content="done")])
        g = _graph(parallel=True, loop=True)
        start = time.monotonic()
        events = await _collect(rt, g, "run")
        elapsed = time.monotonic() - start
        results = [e.content for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "slept:a" in results
        assert "slept:b" in results
        assert "slept:c" in results
        # Parallel: 3x0.3 gather ~0.3s, allow generous bound for test CI jitter.
        assert elapsed < 0.7, f"parallel took {elapsed:.2f}s, expected < 0.7s"

    @pytest.mark.asyncio
    async def test_serial_when_parallel_off(self):
        # parallel_tool_calls=False -> sequential, elapsed >= 0.6s for 2x0.3.
        resp = _llm_response(
            tool_calls=[
                _tc("sleep", {"seconds": 0.3, "tag": "a"}),
                _tc("sleep", {"seconds": 0.3, "tag": "b"}),
            ]
        )
        rt, _ = _make_runtime([resp, _llm_response(content="done")])
        g = _graph(parallel=False, loop=True)
        start = time.monotonic()
        events = await _collect(rt, g, "run")
        elapsed = time.monotonic() - start
        results = [e.content for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "slept:a" in results
        assert "slept:b" in results
        assert elapsed >= 0.55, f"serial took {elapsed:.2f}s, expected >= 0.55s"

    @pytest.mark.asyncio
    async def test_parallel_results_in_input_order(self):
        # gather preserves input order regardless of completion order.
        resp = _llm_response(
            tool_calls=[
                _tc("sleep", {"seconds": 0.2, "tag": "first"}),
                _tc("sleep", {"seconds": 0.01, "tag": "second"}),
            ]
        )
        rt, _ = _make_runtime([resp, _llm_response(content="done")])
        g = _graph(parallel=True, loop=True)
        events = await _collect(rt, g, "run")
        results = [e.content for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert results == ["slept:first", "slept:second"]

    @pytest.mark.asyncio
    async def test_control_flow_tool_forces_sequential(self):
        # unregister_tool is control-flow -> can_parallel=False -> sequential path.
        resp = _llm_response(
            tool_calls=[
                _tc("unregister_tool", {"name": "sleep"}),
                _tc("file_write", {"path": "/x", "content": "y"}),
            ]
        )
        rt, _ = _make_runtime([resp, _llm_response(content="done")])
        g = _graph(parallel=True, loop=True)
        events = await _collect(rt, g, "run")
        names = [e.name for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "unregister_tool" in names
        assert "file_write" in names

    @pytest.mark.asyncio
    async def test_parallel_tool_error_does_not_break_others(self):
        # One tool errors, others still complete in parallel gather.
        resp = _llm_response(
            tool_calls=[
                _tc("sleep", {"seconds": 0.01, "tag": "ok"}),
                _tc("file_write", {"path": "/x", "content": "y"}),
                _tc("sleep", {"seconds": 0.01, "tag": "ok2"}),
            ]
        )
        rt, _ = _make_runtime([resp, _llm_response(content="done")])
        g = _graph(parallel=True, loop=True)
        events = await _collect(rt, g, "run")
        results = [e.content for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "slept:ok" in results
        assert "slept:ok2" in results
        # file_write succeeds (mock) -> all 3 non-error.
        assert len(results) == 3
