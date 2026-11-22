# Issue #756: human_input node queue mechanics run on fusion_harness.HumanGate
# (harness #66 generic queue surface). The #557 RPC surface
# (human_input.respond / human_input.list_pending) is unchanged: resolve
# single-winner (double resolve returns False, never raises), pending list
# maps to HumanGate.list_questions(status="pending"), and queue state
# persists across runtime restarts under <studio_home>/human_gate/.
from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.dispatchers.safety import SafetyDispatcher
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


def _gateway() -> LLMGateway:
    gw = LLMGateway()
    gw.set_default_client(MockClient())
    gw.register_model(ModelConfig(name="m", provider="local", context_length=1024))
    return gw


def _runtime() -> AgentRuntime:
    return AgentRuntime(
        llm_gateway=_gateway(),
        tool_registry=ToolRegistry(),
        max_iterations=50,
    )


def _hit_graph(timeout_seconds=10) -> AgentGraph:
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


@pytest.fixture(autouse=True)
def _studio_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    yield
    monkeypatch.delenv("FUSION_AGENT_STUDIO_HOME", raising=False)


async def test_roundtrip_via_rpc_surface_unchanged():
    rt = _runtime()
    disp = SafetyDispatcher(type("D", (), {"_runtime": rt})())
    respond = disp.get_handlers()["human_input.respond"]
    listing = disp.get_handlers()["human_input.list_pending"]
    graph = _hit_graph(timeout_seconds=10)
    events = []

    async def run():
        ctx = AgentContext(session_id="s-756")
        async for ev in rt.execute_graph(graph, "", context=ctx):
            events.append(ev)
            if ev.type == AgentEventType.SAFETY_APPROVAL:
                pend = await listing({})
                row = [i for i in pend["inputs"] if i["input_id"] == ev.metadata["input_id"]]
                assert row, "pending list should carry the open question"
                assert row[0]["input_var"] == "decision"
                assert row[0]["session_id"] == "s-756"
                assert row[0]["done"] is False
                r = await respond(
                    {"input_id": ev.metadata["input_id"], "approved": True, "value": "go"}
                )
                assert r["status"] == "ok"

    await asyncio.wait_for(run(), timeout=10)
    results = [ev for ev in events if ev.type == AgentEventType.RESULT and "decision" in ev.content]
    assert results and json.loads(results[-1].content)["decision"] == "go"
    # resolved question leaves the pending list
    pend = await listing({})
    assert all(
        i["input_id"] not in {e.metadata.get("input_id") for e in events} for i in pend["inputs"]
    )


async def test_double_resolve_returns_false_no_raise():
    rt = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    events = []

    async def run():
        ctx = AgentContext(session_id="s-double")
        async for ev in rt.execute_graph(graph, "", context=ctx):
            events.append(ev)
            if ev.type == AgentEventType.SAFETY_APPROVAL:
                input_id = ev.metadata["input_id"]
                assert (
                    rt.resolve_human_input(input_id, {"approved": True, "value": "first"}) is True
                )
                # single-winner: second resolve loses, no exception
                assert (
                    rt.resolve_human_input(input_id, {"approved": False, "reason": "second"})
                    is False
                )

    await asyncio.wait_for(run(), timeout=10)
    results = [ev for ev in events if ev.type == AgentEventType.RESULT and "decision" in ev.content]
    assert results and json.loads(results[-1].content)["decision"] == "first"


async def test_queue_state_survives_runtime_restart(tmp_path):
    rt1 = _runtime()
    graph = _hit_graph(timeout_seconds=10)
    captured = {}

    async def run():
        ctx = AgentContext(session_id="s-restart")
        async for ev in rt1.execute_graph(graph, "", context=ctx):
            if ev.type == AgentEventType.SAFETY_APPROVAL:
                captured["input_id"] = ev.metadata["input_id"]
                # do not resolve — the runtime "crashes" here

    # kill the run mid-pause without resolving
    task = asyncio.ensure_future(run())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    gate_path = os.path.join(
        os.environ["FUSION_AGENT_STUDIO_HOME"], "human_gate", "human_gate_queue.json"
    )
    assert os.path.exists(gate_path), "gate queue persisted on disk"

    rt2 = _runtime()
    pend = rt2.pending_human_inputs()
    assert any(p["input_id"] == captured["input_id"] for p in pend)
    assert (
        rt2.resolve_human_input(captured["input_id"], {"approved": True, "value": "late"}) is True
    )
    pend2 = rt2.pending_human_inputs()
    assert all(p["input_id"] != captured["input_id"] for p in pend2)


async def test_resolve_unknown_input_id_returns_false():
    rt = _runtime()
    assert rt.resolve_human_input("hq-unknown", {"approved": True}) is False
    assert rt.pending_human_inputs() == []


async def test_runtime_construction_does_not_materialize_gate(tmp_path):
    # no-eager-mkdir (#591): constructing a runtime must not create the gate
    # state dir — only an actual human_input ask / resolve / list does
    # (lazy first-use materialization is intended for consumers).
    home = os.environ["FUSION_AGENT_STUDIO_HOME"]
    rt = _runtime()
    assert not os.path.exists(os.path.join(home, "human_gate"))
    assert rt.pending_human_inputs() == []
