"""#202: direct tool node returning an error must stop the DAG cascade when the
graph opts in via stop_on_tool_error=True. Default False preserves the existing
"error-as-result, cascade continues" behavior so existing graphs are unaffected.

Covers three error shapes a tool node can produce:
  - raises an exception            -> runtime wraps as "Error: {e}"
  - returns a string "Error: ..."  -> prefix match
  - returns JSON {"error": ...}    -> key match (fusion-operation convention)
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry


class RaiseTool(BaseTool):
    name = "raise_tool"
    description = "always raises"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        raise RuntimeError("boom")


class ErrorStringTool(BaseTool):
    name = "error_string_tool"
    description = "returns Error: prefix"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "Error: bad thing"


class ErrorJsonTool(BaseTool):
    name = "error_json_tool"
    description = "returns {\"error\": ...} JSON"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return json.dumps({"error": "model 500", "status": "failed"})


class OkTool(BaseTool):
    name = "ok_tool"
    description = "returns ok result"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return json.dumps({"status": "ok", "video_path": "/tmp/v.mp4"})


class MockMLXClient:
    async def chat(self, *a, **k):
        return None


def _two_tool_graph(stop_on_tool_error: bool, first_tool: str) -> AgentGraph:
    graph = AgentGraph(name="ErrCascade")
    graph.stop_on_tool_error = stop_on_tool_error
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "t1",
        NodeConfig(type="tool", label="T1", tool_name=first_tool),
    )
    graph.add_node(
        "t2",
        NodeConfig(type="tool", label="T2", tool_name="ok_tool"),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "t1")
    graph.add_edge("t1", "t2")
    graph.add_edge("t2", "end")
    return graph


class TestStopOnToolError:
    @pytest.mark.asyncio
    async def test_raise_stops_cascade_when_opted_in(self):
        registry = ToolRegistry()
        registry.register(RaiseTool())
        registry.register(OkTool())
        graph = _two_tool_graph(stop_on_tool_error=True, first_tool="raise_tool")
        runtime = AgentRuntime(MockMLXClient(), registry)
        events = []
        async for event in runtime.execute_graph(graph, "go"):
            events.append(event)
        error_events = [e for e in events if e.type == AgentEventType.ERROR]
        assert error_events, "expected an ERROR event from the failed tool node"
        assert error_events[0].metadata.get("tool_error") is True
        assert error_events[0].metadata.get("tool") == "raise_tool"
        # t1 runs (TOOL_RESULT emitted before error detection), t2 must NOT.
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        names = [e.name for e in tool_results]
        assert "raise_tool" in names
        assert "ok_tool" not in names, "cascade should stop before ok_tool"

    @pytest.mark.asyncio
    async def test_error_string_stops_cascade_when_opted_in(self):
        registry = ToolRegistry()
        registry.register(ErrorStringTool())
        registry.register(OkTool())
        graph = _two_tool_graph(stop_on_tool_error=True, first_tool="error_string_tool")
        runtime = AgentRuntime(MockMLXClient(), registry)
        events = []
        async for event in runtime.execute_graph(graph, "go"):
            events.append(event)
        error_events = [e for e in events if e.type == AgentEventType.ERROR]
        assert error_events
        assert error_events[0].metadata.get("tool_error") is True
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "ok_tool" not in [e.name for e in tool_results]

    @pytest.mark.asyncio
    async def test_error_json_stops_cascade_when_opted_in(self):
        registry = ToolRegistry()
        registry.register(ErrorJsonTool())
        registry.register(OkTool())
        graph = _two_tool_graph(stop_on_tool_error=True, first_tool="error_json_tool")
        runtime = AgentRuntime(MockMLXClient(), registry)
        events = []
        async for event in runtime.execute_graph(graph, "go"):
            events.append(event)
        error_events = [e for e in events if e.type == AgentEventType.ERROR]
        assert error_events
        assert error_events[0].metadata.get("tool_error") is True
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "ok_tool" not in [e.name for e in tool_results]

    @pytest.mark.asyncio
    async def test_default_false_error_as_result_cascade_continues(self):
        # Default stop_on_tool_error=False: the error result is treated as a
        # normal result, output_mapping applied, downstream ok_tool runs.
        registry = ToolRegistry()
        registry.register(ErrorJsonTool())
        registry.register(OkTool())
        graph = _two_tool_graph(stop_on_tool_error=False, first_tool="error_json_tool")
        runtime = AgentRuntime(MockMLXClient(), registry)
        events = []
        async for event in runtime.execute_graph(graph, "go"):
            events.append(event)
        error_events = [e for e in events if e.type == AgentEventType.ERROR]
        assert not error_events, "default off must NOT emit an ERROR event"
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert "ok_tool" in [e.name for e in tool_results], "cascade must continue"


class TestStopOnToolErrorGraphRoundtrip:
    def test_to_dict_from_dict_preserves_field(self):
        graph = AgentGraph(name="Roundtrip", stop_on_tool_error=True)
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "end")
        d = graph.to_dict()
        assert d["stop_on_tool_error"] is True
        restored = AgentGraph.from_dict(d)
        assert restored.stop_on_tool_error is True

    def test_from_dict_default_false_when_absent(self):
        graph = AgentGraph(name="Legacy")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "end")
        d = graph.to_dict()
        d.pop("stop_on_tool_error", None)
        restored = AgentGraph.from_dict(d)
        assert restored.stop_on_tool_error is False
