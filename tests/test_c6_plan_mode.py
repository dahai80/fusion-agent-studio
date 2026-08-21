"""Tests for C6 plan-as-mode — read-only explore gating + ExitPlanMode + planner block.

Three P0 gaps from audit P0-2:
1. plan_mode flag gates write tools off (read-only explore phase).
2. ExitPlanMode tool transitions plan->execution (flips plan_mode off).
3. planner node blocks in-graph for approval when await_approval set.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse, StreamChunk
from tools.base import BaseTool
from tools.plan_tools import EXIT_PLAN_MODE_SENTINEL, ExitPlanModeTool
from tools.registry import ToolRegistry


class WriteTool(BaseTool):
    name = "file_write"
    description = "Write file (blocked in plan mode)"
    parameters = {"path": {"type": "string"}, "content": {"type": "string"}}

    async def execute(self, path: str = "", content: str = "", **kwargs) -> str:
        return f"Wrote {path}"


class ReadTool(BaseTool):
    name = "file_read"
    description = "Read file (allowed in plan mode)"
    parameters = {"path": {"type": "string"}}

    async def execute(self, path: str = "", **kwargs) -> str:
        return f"Content of {path}"


def _llm_response(content="", tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
        finish_reason="stop",
    )


class ToolCallClient:
    # Returns a scripted sequence of LLM responses (content + tool_calls).
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0

    async def chat(self, model, messages, tools=None, **kwargs):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return _llm_response(content="done")

    async def chat_stream(self, messages, model="", **kwargs):
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
    r.register(WriteTool())
    r.register(ReadTool())
    r.register(ExitPlanModeTool())
    return r


def _make_runtime(responses):
    client = ToolCallClient(responses)
    gw = LLMGateway()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    return AgentRuntime(llm_gateway=gw, tool_registry=_make_registry())


def _tc(name, args):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _plan_graph():
    g = AgentGraph(name="c6_plan_test", start_node_id="start", plan_mode=True)
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "llm1",
        NodeConfig(
            type="llm",
            label="llm1",
            model="test-model",
            loop_mode="agent",
            max_loop_iterations=10,
        ),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "end")
    return g


# ---------------------------------------------------------------------------
# Gap 1: plan_mode gates write tools, allows read tools
# ---------------------------------------------------------------------------


class TestPlanModeGating:
    @pytest.mark.asyncio
    async def test_write_tool_blocked_in_plan_mode(self):
        # LLM tries file_write (write) — should be blocked, not executed.
        responses = [
            _llm_response(
                content="",
                tool_calls=[_tc("file_write", {"path": "a.txt", "content": "x"})],
            ),
            _llm_response(content="plan done"),
        ]
        rt = _make_runtime(responses)
        g = _plan_graph()
        events = []
        async for e in rt.execute_graph(g, "write a file"):
            events.append(e)
        blocked = [
            e
            for e in events
            if e.type == AgentEventType.TOOL_RESULT and e.metadata.get("plan_mode_blocked")
        ]
        assert blocked, "write tool was not blocked in plan_mode"
        assert "plan_mode" in blocked[0].content.lower()

    @pytest.mark.asyncio
    async def test_read_tool_allowed_in_plan_mode(self):
        # LLM calls file_read (read-only) — should execute, not blocked.
        responses = [
            _llm_response(
                content="",
                tool_calls=[_tc("file_read", {"path": "a.txt"})],
            ),
            _llm_response(content="done"),
        ]
        rt = _make_runtime(responses)
        g = _plan_graph()
        events = []
        async for e in rt.execute_graph(g, "read a file"):
            events.append(e)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert tool_results, "no tool result emitted"
        assert "Content of a.txt" in tool_results[0].content
        assert not tool_results[0].metadata.get("plan_mode_blocked")

    @pytest.mark.asyncio
    async def test_write_tool_allowed_when_plan_mode_off(self):
        # plan_mode=False (default graph) — write tool executes normally.
        responses = [
            _llm_response(
                content="",
                tool_calls=[_tc("file_write", {"path": "a.txt", "content": "x"})],
            ),
            _llm_response(content="done"),
        ]
        rt = _make_runtime(responses)
        g = AgentGraph(name="c6_exec", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test-model"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "llm1")
        g.add_edge("llm1", "end")
        events = []
        async for e in rt.execute_graph(g, "write a file"):
            events.append(e)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert tool_results, "no tool result"
        assert "Wrote a.txt" in tool_results[0].content
        assert not tool_results[0].metadata.get("plan_mode_blocked")


# ---------------------------------------------------------------------------
# Gap 2: ExitPlanMode transitions plan->execution
# ---------------------------------------------------------------------------


class TestExitPlanMode:
    @pytest.mark.asyncio
    async def test_exit_plan_mode_flips_flag(self):
        # LLM calls exit_plan_mode — sentinel detected, plan_mode->False,
        # PLAN_MODE_EXIT event emitted, then a write tool succeeds.
        responses = [
            _llm_response(
                content="",
                tool_calls=[
                    _tc("exit_plan_mode", {"plan": "Step 1: write file"}),
                ],
            ),
            _llm_response(
                content="",
                tool_calls=[_tc("file_write", {"path": "b.txt", "content": "y"})],
            ),
            _llm_response(content="done"),
        ]
        rt = _make_runtime(responses)
        g = _plan_graph()
        events = []
        async for e in rt.execute_graph(g, "plan then write"):
            events.append(e)
        exits = [e for e in events if e.type == AgentEventType.PLAN_MODE_EXIT]
        assert exits, "no PLAN_MODE_EXIT event emitted"
        assert "Step 1: write file" in exits[0].content
        # After exit, plan_mode flipped — subsequent write must succeed.
        assert rt.plan_mode is False
        writes = [
            e for e in events if e.type == AgentEventType.TOOL_RESULT and "Wrote b.txt" in e.content
        ]
        assert writes, "write tool did not execute after exit_plan_mode"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_tool_sentinel(self):
        # Unit: the tool itself returns the sentinel prefix.
        tool = ExitPlanModeTool()
        result = await tool.execute(plan="my plan", ready=True)
        assert result.startswith(EXIT_PLAN_MODE_SENTINEL)
        assert result[len(EXIT_PLAN_MODE_SENTINEL) :] == "my plan"

    @pytest.mark.asyncio
    async def test_exit_plan_mode_tool_not_ready(self):
        tool = ExitPlanModeTool()
        result = await tool.execute(plan="x", ready=False)
        assert "not ready" in result.lower()


# ---------------------------------------------------------------------------
# Gap 3: planner node blocks in-graph for approval
# ---------------------------------------------------------------------------


class TestPlannerBlock:
    @pytest.mark.asyncio
    async def test_planner_node_blocks_until_approved(self):
        # planner node with await_approval=True blocks, then a concurrent
        # approve_plan_in_graph resolves the future -> plan_approved event.
        rt = _make_runtime([])
        # Stub PlannerEngine.create_plan to avoid LLM call.
        import time as _time

        from agent_runtime.planner import ExecutionPlan, PlannerEngine, PlanStep

        orig_create_plan = PlannerEngine.create_plan

        async def stub_plan(self, task, context="", files=None):
            p = ExecutionPlan(
                id="plan_test_1",
                task=task,
                steps=[
                    PlanStep(
                        id="s1",
                        description="do thing",
                        target_files=[],
                        action="modify",
                        estimated_complexity="low",
                        dependencies=[],
                    )
                ],
                created_at=_time.time(),
                status="pending_approval",
            )
            self._plans[p.id] = p
            return p

        PlannerEngine.create_plan = stub_plan
        try:
            g = AgentGraph(name="c6_planner_block", start_node_id="start")
            g.add_node("start", NodeConfig(type="start", label="start"))
            g.add_node(
                "planner1",
                NodeConfig(
                    type="planner",
                    label="planner1",
                    tool_params={"await_approval": True, "approval_timeout": 5.0},
                ),
            )
            g.add_node("end", NodeConfig(type="end", label="end"))
            g.add_edge("start", "planner1")
            g.add_edge("planner1", "end")

            async def run_graph():
                events = []
                async for e in rt.execute_graph(g, "do a task"):
                    events.append(e)
                return events

            task = asyncio.create_task(run_graph())
            # Wait for the planner to register its pending future.
            await asyncio.sleep(0.3)
            assert "plan_test_1" in rt._plan_futures, (
                f"planner future not registered: {list(rt._plan_futures)}"
            )
            rt.approve_plan_in_graph("plan_test_1")
            events = await task

            approvals = [e for e in events if e.type == AgentEventType.PLAN_APPROVAL]
            actions = [a.metadata.get("action") for a in approvals]
            assert "pending_approval" in actions, f"no pending event: {actions}"
            assert "approved" in actions, f"no approved event: {actions}"
            assert rt.plan_mode is False  # graph not in plan_mode
        finally:
            PlannerEngine.create_plan = orig_create_plan

    @pytest.mark.asyncio
    async def test_planner_node_rejected_stops(self):
        rt = _make_runtime([])
        import time as _time

        from agent_runtime.planner import ExecutionPlan, PlannerEngine, PlanStep

        orig_create_plan = PlannerEngine.create_plan

        async def stub_plan(self, task, context="", files=None):
            p = ExecutionPlan(
                id="plan_test_2",
                task=task,
                steps=[
                    PlanStep(
                        id="s1",
                        description="do thing",
                        target_files=[],
                        action="modify",
                        estimated_complexity="low",
                        dependencies=[],
                    )
                ],
                created_at=_time.time(),
                status="pending_approval",
            )
            self._plans[p.id] = p
            return p

        PlannerEngine.create_plan = stub_plan
        try:
            g = AgentGraph(name="c6_planner_reject", start_node_id="start")
            g.add_node("start", NodeConfig(type="start", label="start"))
            g.add_node(
                "planner1",
                NodeConfig(
                    type="planner",
                    label="planner1",
                    tool_params={"await_approval": True, "approval_timeout": 5.0},
                ),
            )
            g.add_node("end", NodeConfig(type="end", label="end"))
            g.add_edge("start", "planner1")
            g.add_edge("planner1", "end")

            async def run_graph():
                events = []
                async for e in rt.execute_graph(g, "do a task"):
                    events.append(e)
                return events

            task = asyncio.create_task(run_graph())
            await asyncio.sleep(0.3)
            rt.reject_plan_in_graph("plan_test_2")
            events = await task

            approvals = [e for e in events if e.type == AgentEventType.PLAN_APPROVAL]
            actions = [a.metadata.get("action") for a in approvals]
            assert "rejected" in actions, f"no rejected event: {actions}"
            # graph should end in error (rejected stops execution)
            errors = [e for e in events if e.type == AgentEventType.ERROR]
            assert errors, "no error emitted after rejection"
        finally:
            PlannerEngine.create_plan = orig_create_plan

    @pytest.mark.asyncio
    async def test_planner_node_no_await_does_not_block(self):
        # Without await_approval, planner node generates plan and proceeds.
        rt = _make_runtime([])
        import time as _time

        from agent_runtime.planner import ExecutionPlan, PlannerEngine, PlanStep

        orig_create_plan = PlannerEngine.create_plan

        async def stub_plan(self, task, context="", files=None):
            p = ExecutionPlan(
                id="plan_test_3",
                task=task,
                steps=[
                    PlanStep(
                        id="s1",
                        description="do thing",
                        target_files=[],
                        action="modify",
                        estimated_complexity="low",
                        dependencies=[],
                    )
                ],
                created_at=_time.time(),
                status="pending_approval",
            )
            self._plans[p.id] = p
            return p

        PlannerEngine.create_plan = stub_plan
        try:
            g = AgentGraph(name="c6_planner_noblock", start_node_id="start")
            g.add_node("start", NodeConfig(type="start", label="start"))
            g.add_node(
                "planner1",
                NodeConfig(type="planner", label="planner1", tool_params={}),
            )
            g.add_node("end", NodeConfig(type="end", label="end"))
            g.add_edge("start", "planner1")
            g.add_edge("planner1", "end")

            events = []
            async for e in rt.execute_graph(g, "do a task"):
                events.append(e)
            # No PLAN_APPROVAL events (await not set).
            approvals = [e for e in events if e.type == AgentEventType.PLAN_APPROVAL]
            assert not approvals, "PLAN_APPROVAL emitted without await_approval"
            # Graph completes (END event present).
            ends = [e for e in events if e.type == AgentEventType.END]
            assert ends, "graph did not reach END"
        finally:
            PlannerEngine.create_plan = orig_create_plan
