"""#556: iteration + variable_aggregator node types, FlowState, cap breach.

Bounded execution is asserted with asyncio.wait_for — a regression that
hangs fails the test instead of stalling CI.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse
from tools.registry import ToolRegistry


class MockClient:
    async def chat(self, model, messages, tools=None, **kwargs):
        return LLMResponse(
            content="ok",
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )

    async def chat_stream(self, messages, model="", **kwargs):
        raise NotImplementedError

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _runtime(seed: dict | None = None) -> AgentRuntime:
    gateway = LLMGateway()
    gateway.set_default_client(MockClient())
    gateway.register_model(ModelConfig(name="m", provider="local", context_length=1024))
    rt = AgentRuntime(llm_gateway=gateway, tool_registry=ToolRegistry(), max_iterations=50)
    if seed:
        rt.variables.load_from(seed)
    return rt


def _sub_graph_json() -> str:
    g = AgentGraph(name="sub", start_node_id="s")
    g.add_node("s", NodeConfig(type="start", label="s"))
    g.add_node("llm", NodeConfig(type="llm", label="llm", model="m"))
    g.add_node("out", NodeConfig(type="end", label="out"))
    g.add_edge("s", "llm")
    g.add_edge("llm", "out")
    return g.to_json()


def _iteration_graph(items_var="items", max_items=100, flow_state=None) -> AgentGraph:
    g = AgentGraph(name="iter", start_node_id="start")
    g.add_node(
        "start",
        NodeConfig(
            type="start",
            label="start",
            tool_params={"flow_state": flow_state if flow_state is not None else ["items"]},
        ),
    )
    g.add_node(
        "iter1",
        NodeConfig(
            type="iteration",
            label="iter1",
            tool_name="__sub_graph__",
            tool_params={
                "items_var": items_var,
                "max_items": max_items,
                "output_var": "results",
                "graph_json": _sub_graph_json(),
                "input_mapping": {"item": "input"},
            },
        ),
    )
    g.add_node(
        "agg",
        NodeConfig(
            type="variable_aggregator",
            label="agg",
            tool_params={"input_vars": ["results"], "output_var": "merged"},
        ),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "iter1")
    g.add_edge("iter1", "agg")
    g.add_edge("agg", "end")
    return g


async def _collect(rt, graph, inp):
    events = []
    async for ev in rt.execute_graph(graph, inp):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_iteration_runs_subgraph_per_item_and_aggregates():
    rt = _runtime()
    g = _iteration_graph()
    events = await asyncio.wait_for(
        _collect(rt, g, json.dumps({"items": ["a", "b", "c"]})), timeout=15
    )
    iter_events = [
        e for e in events if e.type == AgentEventType.RESULT and e.node_id == "iter1"
    ]
    assert len(iter_events) == 1
    assert json.loads(iter_events[0].content) == ["ok", "ok", "ok"]
    agg = [e for e in events if e.type == AgentEventType.RESULT and e.node_id == "agg"]
    assert len(agg) == 1
    assert json.loads(agg[0].content) == ["ok", "ok", "ok"]


@pytest.mark.asyncio
async def test_start_seeds_flow_state_from_json_input():
    rt = _runtime()
    g = _iteration_graph(flow_state=["region", "items"])
    events = await asyncio.wait_for(
        _collect(rt, g, json.dumps({"items": ["x", "y"], "region": "cn"})), timeout=15
    )
    iter_events = [
        e for e in events if e.node_id == "iter1" and e.type == AgentEventType.RESULT
    ]
    assert json.loads(iter_events[0].content) == ["ok", "ok"]
    snap = iter_events[0].metadata.get("flow_state") or {}
    assert snap.get("region") == "cn"
    assert snap.get("items") == ["x", "y"]


@pytest.mark.asyncio
async def test_iteration_cap_breach_fails_visibly():
    rt = _runtime()
    g = _iteration_graph(max_items=2)
    events = await asyncio.wait_for(
        _collect(rt, g, json.dumps({"items": ["a", "b", "c"]})), timeout=15
    )
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert errors, "cap breach must surface an ERROR event"
    assert "exceeds cap 2" in errors[0].content
    assert errors[0].metadata.get("cap_breach") is True
    # aggregation never ran after the visible failure
    assert not [e for e in events if e.node_id == "agg" and e.type == AgentEventType.RESULT]


@pytest.mark.asyncio
async def test_iteration_missing_or_non_list_items_errors():
    rt = _runtime()
    g = _iteration_graph()
    events = await asyncio.wait_for(_collect(rt, g, "plain text"), timeout=15)
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert errors and "not found" in errors[0].content

    events2 = await asyncio.wait_for(
        _collect(rt, g, json.dumps({"items": "not-a-list"})), timeout=15
    )
    errors2 = [e for e in events2 if e.type == AgentEventType.ERROR]
    assert errors2 and "expected list" in errors2[0].content


@pytest.mark.asyncio
async def test_variable_aggregator_requires_input_vars():
    rt = _runtime()
    g = AgentGraph(name="agg-only", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("agg", NodeConfig(type="variable_aggregator", label="agg", tool_params={}))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "agg")
    g.add_edge("agg", "end")
    events = await asyncio.wait_for(_collect(rt, g, "hi"), timeout=15)
    errors = [e for e in events if e.type == AgentEventType.ERROR]
    assert errors and "input_vars" in errors[0].content


@pytest.mark.asyncio
async def test_aggregator_merges_multiple_vars_as_dict():
    rt = _runtime(seed={"branch_a": 1, "branch_b": "two"})
    g = AgentGraph(name="multi", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "agg",
        NodeConfig(
            type="variable_aggregator",
            label="agg",
            tool_params={"input_vars": ["branch_a", "branch_b"], "output_var": "merged"},
        ),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "agg")
    g.add_edge("agg", "end")
    events = await asyncio.wait_for(_collect(rt, g, "hi"), timeout=15)
    agg = [e for e in events if e.node_id == "agg" and e.type == AgentEventType.RESULT]
    assert json.loads(agg[0].content) == {"branch_a": 1, "branch_b": "two"}


@pytest.mark.asyncio
async def test_bounded_execution_no_hang_on_large_iter():
    """Bounded: even at the cap the run completes inside the wait_for."""
    rt = _runtime()
    g = _iteration_graph(max_items=50)
    events = await asyncio.wait_for(
        _collect(rt, g, json.dumps({"items": list(range(50))})), timeout=30
    )
    assert [e for e in events if e.type == AgentEventType.END]


def test_graph_validate_accepts_new_types():
    g = _iteration_graph()
    issues = g.validate()
    assert not [e for e in issues if not e.startswith("warning:")]
