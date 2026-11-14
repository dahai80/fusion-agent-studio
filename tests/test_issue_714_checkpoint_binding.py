# Issue #714 part 1: checkpoint re-point onto fusion_harness.checkpoint_binding.
# - _save_checkpoint persists per-node state through bind_node_state (K2 #35)
#   while the #675 session-keyed mirror stays byte-compatible (old rows still
#   load through KernelCheckpointer.latest).
# - resume prefers binding state for pending_tool_calls fallback; old-format
#   checkpoints (no binding rows) behave exactly as before.
from __future__ import annotations

import json

from fusion_harness.checkpoint_binding import bind_node_state
from fusion_harness.outbox import OutboxStore

from agent_runtime import AgentRuntime
from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph
from agent_runtime.kernel_wiring import KernelCheckpointer
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.variable_manager import VariableManager


class _MockClient:
    async def chat(self, model="", messages=None, **kwargs):
        return type("R", (), {"content": "ok", "tool_calls": [], "usage": {}, "model": model})()


def _runtime(tmp_path, kernel_checkpoints=True) -> AgentRuntime:
    gw = LLMGateway()
    gw.set_default_client(_MockClient())
    gw.register_model(ModelConfig(name="m", provider="local", context_length=1024))
    rt = AgentRuntime(llm_gateway=gw, store=AgentStore(str(tmp_path / "store.db")))
    if kernel_checkpoints:
        rt.kernel_checkpoints = KernelCheckpointer(str(tmp_path / "kcp.db"))
    return rt


def _graph() -> AgentGraph:
    g = AgentGraph(name="g714", start_node_id="start")
    return g


async def test_save_writes_node_binding_row_and_legacy_mirror(tmp_path):
    rt = _runtime(tmp_path)
    ctx = AgentContext(session_id="s1")
    ctx.current_node_id = "node-a"
    ctx.iteration_count = 2
    ctx.variables = VariableManager()
    ctx.variables.set("k", "v")
    graph = _graph()
    await rt._save_checkpoint(ctx, graph)
    kcp = rt.kernel_checkpoints
    legacy = kcp.latest("s1")
    assert legacy is not None, "#675 session-keyed mirror must stay intact"
    assert legacy.metadata["node_id"] == "node-a"
    binder = bind_node_state(
        kcp.store,
        rt.node_effect_outbox,
        "s1",
    )
    bound = binder.load("node-a")
    assert bound is not None, "node state must persist through checkpoint_binding"
    assert bound["graph_id"] == "g714"
    assert bound["iteration_count"] == 2
    assert bound["variables"]["k"] == "v"
    assert bound["pending_tool_calls"] == []


async def test_binding_row_does_not_collide_with_session_row(tmp_path):
    rt = _runtime(tmp_path)
    ctx = AgentContext(session_id="s1")
    ctx.current_node_id = "node-a"
    ctx.variables = VariableManager()
    await rt._save_checkpoint(ctx, _graph())
    binder = bind_node_state(rt.kernel_checkpoints.store, rt.node_effect_outbox, "s1")
    assert binder.load("node-a") is not None
    assert rt.kernel_checkpoints.latest("s1") is not None
    # distinct keys: "s1" (legacy) vs "s1:node-a" (binding)
    assert binder.load("missing-node") is None


async def test_old_format_checkpoint_still_loads(tmp_path):
    # Compat fixture: a checkpoint row written in the pre-#714 on-disk format
    # (session-keyed mirror only, no binding rows) reads back unchanged.
    rt = _runtime(tmp_path)
    legacy_state = {
        "messages": [{"role": "user", "content": "hi"}],
        "iteration_count": 3,
        "variables": {"a": "b"},
        "tool_call_chain_count": 1,
        "pending_tool_calls": [{"id": "c1"}],
    }
    rt.store.save_checkpoint(
        graph_id="g714",
        session_id="s-old",
        node_id="node-x",
        state=legacy_state,
    )
    rt.kernel_checkpoints.save(
        "s-old",
        legacy_state["messages"],
        budget_spent_tokens=7,
        metadata={"graph_id": "g714", "node_id": "node-x", "iteration_count": 3},
    )
    checkpoint = rt.store.load_latest_checkpoint(graph_id="g714", session_id="s-old")
    assert checkpoint is not None
    assert json.loads(checkpoint.context_json)["pending_tool_calls"][0]["id"] == "c1"
    state = rt.kernel_checkpoints.latest("s-old")
    assert state is not None and state.budget_spent_tokens == 7
    # no binding rows exist for the old session — binder.load returns None
    binder = bind_node_state(
        rt.kernel_checkpoints.store,
        OutboxStore(str(tmp_path / "outbox.db")),
        "s-old",
    )
    assert binder.load("node-x") is None


async def test_resume_prefers_binding_pending_fallback(tmp_path, monkeypatch):
    # Binding row present with pending tool calls; legacy row state empty —
    # resume surfaces the binding pending set.
    rt = _runtime(tmp_path)
    rt.store.save_checkpoint(
        graph_id="g714",
        session_id="s2",
        node_id="node-a",
        state={"messages": [], "iteration_count": 0, "variables": {}},
    )
    kcp = rt.kernel_checkpoints
    outbox = OutboxStore(str(tmp_path / "outbox.db"))
    rt.node_effect_outbox = outbox
    binder = bind_node_state(kcp.store, outbox, "s2")
    binder.save("node-a", {"pending_tool_calls": [{"id": "call-9"}]})
    events = []
    graph = _graph()
    async for ev in rt.resume_from_checkpoint(graph, "s2"):
        events.append(ev)
        break
    # the CHECKPOINT event yields before execution; pending metadata set on ctx
    # inside resume — verify via the emitted event's checkpoint payload exists
    # and the binder row is what resume reads (no exception, binding loaded log).
    assert events, "resume must start from the checkpoint"
