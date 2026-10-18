"""#349 N10: runtime-level standing reviewer — deterministic event-stream checks."""

from __future__ import annotations

import pytest

from agent_runtime.context import AgentEvent, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.resident_reviewer import (
    RUNTIME_REVIEW_ENV,
    build_runtime_reviewer,
    runtime_review_enabled,
)
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


def _graph(resident: bool = False):
    g = AgentGraph(name="n10_review", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("tool1", NodeConfig(type="tool", label="tool1", tool_name="echo"))
    g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="m"))
    g.add_node("end", NodeConfig(type="end", label="end"))
    verify_params = {"resident": True} if resident else {}
    g.add_node("verify1", NodeConfig(type="verify", label="verify1", tool_params=verify_params))
    g.add_edge("start", "tool1")
    g.add_edge("tool1", "llm1")
    g.add_edge("llm1", "end")
    return g


def _ev(etype, content="", node_id=""):
    return AgentEvent(type=etype, content=content, node_id=node_id)


class TestRuntimeReviewToggle:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_REVIEW_ENV, raising=False)
        assert runtime_review_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE"])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv(RUNTIME_REVIEW_ENV, value)
        assert runtime_review_enabled() is False

    def test_level_marker(self):
        assert build_runtime_reviewer(_graph()).level == "runtime"


class TestRuntimeReviewEvents:
    @pytest.mark.asyncio
    async def test_untraceable_conclusion_flagged(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_REVIEW_ENV, raising=False)
        rt = AgentRuntime(tool_registry=ToolRegistry())
        g = _graph()

        async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
            yield _ev(AgentEventType.TOOL_RESULT, "evidence1234", node_id="tool1")
            yield _ev(AgentEventType.THINK, "Therefore zzzz", node_id="llm1")
            yield _ev(AgentEventType.RESULT, "done", node_id="end")

        monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
        events = [e async for e in rt.execute_graph(g, "run")]
        findings = [
            e
            for e in events
            if e.name == "runtime_reviewer" and e.metadata.get("kind") == "untraceable_conclusion"
        ]
        assert len(findings) == 1
        assert findings[0].metadata["level"] == "runtime"
        assert "[runtime]" in findings[0].content

    @pytest.mark.asyncio
    async def test_graph_mismatch_flagged(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_REVIEW_ENV, raising=False)
        rt = AgentRuntime(tool_registry=ToolRegistry())
        g = _graph()

        async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
            yield _ev(AgentEventType.START, "", node_id="start")
            yield _ev(AgentEventType.RESULT, "", node_id="ghost")

        monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
        events = [e async for e in rt.execute_graph(g, "run")]
        findings = [
            e
            for e in events
            if e.name == "runtime_reviewer" and e.metadata.get("kind") == "graph_mismatch"
        ]
        assert len(findings) == 1
        assert findings[0].node_id == "ghost"

    @pytest.mark.asyncio
    async def test_disable_env_suppresses_review(self, monkeypatch):
        monkeypatch.setenv(RUNTIME_REVIEW_ENV, "0")
        rt = AgentRuntime(tool_registry=ToolRegistry())
        g = _graph()

        async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
            yield _ev(AgentEventType.START, "", node_id="start")
            yield _ev(AgentEventType.RESULT, "", node_id="ghost")

        monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
        events = [e async for e in rt.execute_graph(g, "run")]
        assert not [e for e in events if e.name == "runtime_reviewer"]

    @pytest.mark.asyncio
    async def test_resident_mode_takes_precedence(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_REVIEW_ENV, raising=False)
        rt = AgentRuntime(tool_registry=ToolRegistry())
        g = _graph(resident=True)

        async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
            yield _ev(AgentEventType.START, "", node_id="start")
            yield _ev(AgentEventType.RESULT, "", node_id="ghost")

        monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
        events = [e async for e in rt.execute_graph(g, "run")]
        assert not [e for e in events if e.name == "runtime_reviewer"]
        assert [e for e in events if e.name == "resident_reviewer"]

    @pytest.mark.asyncio
    async def test_summary_written_to_ctx(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_REVIEW_ENV, raising=False)
        rt = AgentRuntime(tool_registry=ToolRegistry())
        g = _graph()

        async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
            yield _ev(AgentEventType.START, "", node_id="start")
            yield _ev(AgentEventType.RESULT, "", node_id="ghost")

        monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
        from agent_runtime.context import AgentContext
        from agent_runtime.variable_manager import VariableManager

        ctx = AgentContext()
        ctx.variables = VariableManager()
        _ = [e async for e in rt.execute_graph(g, "run", context=ctx)]
        counts = ctx.variables.get("runtime_review_counts")
        assert counts["graph_mismatch"] == 1
        assert counts["untraceable_conclusion"] == 0
