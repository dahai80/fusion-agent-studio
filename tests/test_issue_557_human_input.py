"""#557: human_input HiTL node + sub_flow (flow-as-tool).

Bounded execution asserted with asyncio.wait_for — a regression that hangs
fails the test instead of stalling CI. Restart E2E: runtime1 pauses at the
human_input node, a NEW runtime with the SAME store resumes with the human
decision injected and reaches end_turn.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.dispatchers.safety import SafetyDispatcher
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
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


def _gateway() -> LLMGateway:
    gw = LLMGateway()
    gw.set_default_client(MockClient())
    gw.register_model(ModelConfig(name="m", provider="local", context_length=1024))
    return gw


def _runtime(store: AgentStore | None = None) -> AgentRuntime:
    return AgentRuntime(
        llm_gateway=_gateway(),
        tool_registry=ToolRegistry(),
        max_iterations=50,
        store=store,
    )


def _hit_graph(timeout_seconds=300) -> AgentGraph:
    g = AgentGraph(name="hit", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "ask",
        NodeConfig(
            type="human_input",
            label="ask",
            tool_params={
                "input_var": "decision",
                "prompt": "Proceed with deploy?",
                "fields": [{"key": "note", "label": "Note", "widget": "text"}],
                "timeout_seconds": timeout_seconds,
            },
        ),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "ask")
    g.add_edge("ask", "end")
    return g


async def _collect(rt, graph, session_id, seed=None) -> list:
    if seed:
        rt.variables.load_from(seed)
    ctx = AgentContext(session_id=session_id)
    events = []
    async for ev in rt.execute_graph(graph, "", context=ctx):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_human_input_approve_resolves_and_continues():
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    events = []

    async def run():
        ctx = AgentContext(session_id="s-approve")
        async for ev in rt.execute_graph(graph, "", context=ctx):
            events.append(ev)
            if (
                ev.type == AgentEventType.SAFETY_APPROVAL
                and (ev.metadata or {}).get("kind") == "human_input"
            ):
                ok = rt.resolve_human_input(ev.metadata["input_id"], {"approved": True, "value": "go"})
                assert ok

    await asyncio.wait_for(run(), timeout=10)
    kinds = [ev.type for ev in events]
    assert AgentEventType.SAFETY_APPROVAL in kinds
    assert AgentEventType.END in kinds
    results = [ev for ev in events if ev.type == AgentEventType.RESULT and "decision" in ev.content]
    assert results, "expected RESULT carrying the input_var value"
    assert json.loads(results[-1].content)["decision"] == "go"


@pytest.mark.asyncio
async def test_human_input_reject_terminates_with_reason():
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    events = []

    async def run():
        ctx = AgentContext(session_id="s-reject")
        async for ev in rt.execute_graph(graph, "", context=ctx):
            events.append(ev)
            if ev.type == AgentEventType.SAFETY_APPROVAL:
                rt.resolve_human_input(
                    ev.metadata["input_id"], {"approved": False, "reason": "not today"}
                )

    await asyncio.wait_for(run(), timeout=10)
    errs = [ev for ev in events if ev.type == AgentEventType.ERROR]
    assert errs and "not today" in errs[-1].content
    assert (errs[-1].metadata or {}).get("rejected") is True
    assert all(ev.type != AgentEventType.END for ev in events)


@pytest.mark.asyncio
async def test_human_input_timeout_pauses():
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=0.1)
    events = await asyncio.wait_for(_collect(rt, graph, "s-timeout"), timeout=10)
    paused = [ev for ev in events if ev.type == AgentEventType.CHECKPOINT]
    assert paused and paused[-1].metadata.get("paused") is True
    assert paused[-1].metadata.get("input_var") == "decision"
    # END never emitted — run paused mid-graph
    assert all(ev.type != AgentEventType.END for ev in events)


@pytest.mark.asyncio
async def test_human_input_consume_path_seeded_variable():
    # decision already present (resume path) — node returns immediately
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    events = await asyncio.wait_for(
        _collect(rt, graph, "s-seed", seed={"decision": "pre-set"}), timeout=10
    )
    results = [ev for ev in events if ev.type == AgentEventType.RESULT and "decision" in ev.content]
    assert results and json.loads(results[-1].content)["decision"] == "pre-set"
    assert results[-1].metadata.get("resumed") is True
    assert all(ev.type != AgentEventType.SAFETY_APPROVAL for ev in events)
    assert any(ev.type == AgentEventType.END for ev in events)


@pytest.mark.asyncio
async def test_human_input_restart_e2e_resume_from_checkpoint(tmp_path):
    store = AgentStore(str(tmp_path / "store.db"))
    graph = _hit_graph(timeout_seconds=0.1)
    session_id = "s-restart"

    rt1 = _runtime(store)
    events1 = await asyncio.wait_for(_collect(rt1, graph, session_id), timeout=10)
    paused = [
        ev
        for ev in events1
        if ev.type == AgentEventType.CHECKPOINT and (ev.metadata or {}).get("paused")
    ]
    assert paused, "runtime1 should pause at the human_input node"
    assert all(ev.type != AgentEventType.END for ev in events1)
    cp = store.load_latest_checkpoint(graph_id="hit", session_id=session_id)
    assert cp is not None

    # backend restart: NEW runtime, SAME store, decision injected
    rt2 = _runtime(store)
    events2 = []
    async for ev in rt2.resume_from_checkpoint(graph, session_id, human_inputs={"decision": "ship it"}):
        events2.append(ev)
    results = [
        ev for ev in events2 if ev.type == AgentEventType.RESULT and "decision" in ev.content
    ]
    assert results and json.loads(results[-1].content)["decision"] == "ship it"
    ends = [ev for ev in events2 if ev.type == AgentEventType.END]
    assert ends and ends[-1].metadata.get("stop_reason") == "end_turn"


def _sub_flow_graph() -> AgentGraph:
    g = AgentGraph(name="parent", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "call",
        NodeConfig(
            type="sub_flow",
            label="call",
            tool_params={
                "flow_id": "sub-flow-id",
                "input_mapping": {"greeting": "echo"},
                "output_mapping": {"echo": "sub_out"},
            },
        ),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "call")
    g.add_edge("call", "end")
    return g


def _flow_graph() -> AgentGraph:
    g = AgentGraph(name="sub", id="sub-flow-id", start_node_id="s")
    g.add_node("s", NodeConfig(type="start", label="s"))
    g.add_node("llm", NodeConfig(type="llm", label="llm", model="m"))
    g.add_node("out", NodeConfig(type="end", label="out"))
    g.add_edge("s", "llm")
    g.add_edge("llm", "out")
    return g


@pytest.mark.asyncio
async def test_sub_flow_passes_values_in_and_out(tmp_path):
    store = AgentStore(str(tmp_path / "store.db"))
    store.save_graph(_flow_graph())
    rt = _runtime(store)
    rt.variables.load_from({"greeting": "hello"})
    graph = _sub_flow_graph()
    ctx = AgentContext(session_id="s-flow")
    events = []
    async for ev in rt.execute_graph(graph, "", context=ctx):
        events.append(ev)
    assert any(ev.type == AgentEventType.END for ev in events)
    sub_events = [ev for ev in events if str(ev.content).startswith("[sub:sub]")]
    assert sub_events, "sub flow events should be prefixed [sub:<name>]"
    # value flowed out of the flow into the parent variable space
    assert rt.variables.get("sub_out") in ("", None) or ctx.variables.get("sub_out") == "hello"
    assert ctx.variables.get("sub_out") == "hello"


@pytest.mark.asyncio
async def test_sub_flow_missing_flow_id_errors():
    rt = _runtime()
    g = AgentGraph(name="p2", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("call", NodeConfig(type="sub_flow", label="call", tool_params={}))
    g.add_edge("start", "call")
    events = await asyncio.wait_for(_collect(rt, g, "s-missing"), timeout=10)
    errs = [ev for ev in events if ev.type == AgentEventType.ERROR]
    assert errs and "flow_id" in errs[-1].content


@pytest.mark.asyncio
async def test_sub_flow_unknown_flow_errors(tmp_path):
    store = AgentStore(str(tmp_path / "store.db"))
    rt = _runtime(store)
    g = AgentGraph(name="p3", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "call", NodeConfig(type="sub_flow", label="call", tool_params={"flow_id": "nope"})
    )
    g.add_edge("start", "call")
    events = await asyncio.wait_for(_collect(rt, g, "s-unknown"), timeout=10)
    errs = [ev for ev in events if ev.type == AgentEventType.ERROR]
    assert errs and "not found" in errs[-1].content


@pytest.mark.asyncio
async def test_human_input_respond_rpc():
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    daemon = type("D", (), {"_runtime": rt})()
    disp = SafetyDispatcher(daemon)

    respond = disp.get_handlers()["human_input.respond"]
    listing = disp.get_handlers()["human_input.list_pending"]

    res = await respond({"input_id": "hi-none", "approved": True, "value": "x"})
    assert res["status"] == "not_found"

    events = []

    async def run():
        ctx = AgentContext(session_id="s-rpc")
        async for ev in rt.execute_graph(graph, "", context=ctx):
            events.append(ev)
            if ev.type == AgentEventType.SAFETY_APPROVAL:
                pend = await listing({})
                assert pend["inputs"], "pending list should show the open input"
                r = await respond(
                    {"input_id": ev.metadata["input_id"], "approved": True, "value": "via rpc"}
                )
                assert r["status"] == "ok"

    await asyncio.wait_for(run(), timeout=10)
    results = [ev for ev in events if ev.type == AgentEventType.RESULT and "decision" in ev.content]
    assert results and json.loads(results[-1].content)["decision"] == "via rpc"


@pytest.mark.asyncio
async def test_human_input_validate_accepts_new_types():
    g = _hit_graph()
    g.add_node("sf", NodeConfig(type="sub_flow", label="sf", tool_params={"flow_id": "x"}))
    g.add_edge("end", "sf")
    errs = g.validate()
    assert all("unknown type" not in e for e in errs)
