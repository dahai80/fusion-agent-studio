"""Audit 0825 Bundle3 regression — A-1 per-exec runtime state + A-2 sub-runtime
isolation.

A-1: AgentRuntime was a process-level singleton; execution state written to
instance attributes let concurrent graphs stomp each other.
  Tier1: _safety_futures/_plan_futures cross-RPC registries keyed by uuid,
  .clear() removed.
  Tier2: plan_mode/_tool_call_chain_count per-exec -> ctx.
  Tier3: variables per-exec -> ctx (A-2: sub-runtime does not share parent
  variables).
A-2: sub-runtime input_mapping writes polluted the parent singleton -> rewritten
to ctx.variables + copy.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.variable_manager import VariableManager
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _MockMLXClient:
    """Deterministic mock — returns final answer immediately, no network."""

    def __init__(self):
        self.calls = 0

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.calls += 1
        return LLMResponse(content="Final answer", tool_calls=[])


class _PlainTool(BaseTool):
    name = "plain_tool"
    description = "plain"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "ok"


def _make_runtime() -> AgentRuntime:
    mlx = _MockMLXClient()
    reg = ToolRegistry()
    reg.register(_PlainTool())
    return AgentRuntime(mlx, reg)


def _two_node_graph(gid: str) -> AgentGraph:
    g = AgentGraph(id=gid, name=gid)
    g.add_node("s", NodeConfig(type="start", label="S"))
    g.add_node("e", NodeConfig(type="end", label="E"))
    g.add_edge("s", "e")
    g.start_node_id = "s"
    return g


# ── A-1 Tier1: cross-RPC futures survive concurrent exec ──


class TestA1Tier1FuturesNotCleared:
    @pytest.mark.asyncio
    async def test_safety_future_survives_concurrent_exec_start(self):
        # Graph A registers a safety future (simulating pending approval). Graph B
        # starting must not clear it.
        runtime = _make_runtime()
        runtime._safety_futures["A-action-xyz"] = asyncio.get_event_loop().create_future()
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        assert "A-action-xyz" in runtime._safety_futures, (
            "concurrent exec B cleared A's pending safety future"
        )

    @pytest.mark.asyncio
    async def test_plan_future_survives_concurrent_exec_start(self):
        runtime = _make_runtime()
        runtime._plan_futures["A-plan-xyz"] = asyncio.get_event_loop().create_future()
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        assert "A-plan-xyz" in runtime._plan_futures, (
            "concurrent exec B cleared A's pending plan future"
        )


# ── A-1 Tier2: plan_mode / tool_call_chain_count per-exec ──


class TestA1Tier2PerExecState:
    @pytest.mark.asyncio
    async def test_plan_mode_independent_across_execs(self):
        # A enters plan_mode (read-only), B normal. After B starts, A's ctx.plan_mode
        # is unchanged.
        runtime = _make_runtime()
        ctx_a = AgentContext()
        ctx_a.plan_mode = True
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        assert ctx_a.plan_mode is True, "B's start reset A's plan_mode"

    @pytest.mark.asyncio
    async def test_tool_call_chain_count_independent(self):
        # A mid-run chain_count=3; B start zeroes only its own ctx, leaving A alone.
        runtime = _make_runtime()
        ctx_a = AgentContext()
        ctx_a.tool_call_chain_count = 3
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        assert ctx_b.tool_call_chain_count == 0, "B's ctx chain not zeroed at dispatch"
        assert ctx_a.tool_call_chain_count == 3, "B's start stomped A's chain count"


# ── A-1 Tier3 / A-2: variables per-exec isolation ──


class TestA1Tier3VariablesIsolation:
    @pytest.mark.asyncio
    async def test_variables_not_shared_across_execs(self):
        # A seeds a declared var in runtime.variables; after B starts, ctx.variables
        # is a copy. Writes to ctx.variables during B's run must not flow back into
        # runtime.variables (clean singleton seed).
        runtime = _make_runtime()
        runtime.variables.set("declared", "seed-val")
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        # B's ctx got a copy of the seed.
        assert ctx_b.variables.get("declared") == "seed-val"
        # B writes its own ctx variable.
        ctx_b.variables.set("b_only", "b-set")
        # The singleton seed is not polluted by B's execution.
        assert runtime.variables.get("b_only") == "", "B's ctx var bled to singleton"
        assert runtime.variables.get("declared") == "seed-val"

    @pytest.mark.asyncio
    async def test_sub_runtime_does_not_pollute_parent_singleton(self):
        # A-2 core: sub-graph input_mapping resolution writes the parent
        # ctx.variables (not the singleton); the sub-runtime's variable changes do
        # not flow back to the parent singleton self.variables.
        runtime = _make_runtime()
        runtime.variables.set("parent_val", "P")
        sub_g = _two_node_graph("sub")
        sub_json = sub_g.to_json()
        parent_g = AgentGraph(id="parent", name="parent")
        parent_g.add_node("s", NodeConfig(type="start", label="S"))
        parent_g.add_node(
            "sub",
            NodeConfig(
                type="tool",
                label="sub",
                tool_name="__sub_graph__",
                tool_params={
                    "graph_json": sub_json,
                    "input_mapping": {"parent_val": "input"},
                    "output_mapping": {},
                },
            ),
        )
        parent_g.add_node("e", NodeConfig(type="end", label="E"))
        parent_g.add_edge("s", "sub")
        parent_g.add_edge("sub", "e")
        parent_g.start_node_id = "s"
        ctx = AgentContext()
        async for _ in runtime.execute_graph(parent_g, "go", context=ctx):
            pass
        # The parent singleton is not polluted by the sub-graph input_mapping.
        assert runtime.variables.get("parent_val") == "P"
        # The parent ctx receives the input_mapping resolution (sub_var=input is
        # written to the parent ctx).
        # (input_mapping {parent_val: input} -> sub_input=P, no extra var written to
        # the parent ctx)

    @pytest.mark.asyncio
    async def test_sub_runtime_output_writes_parent_ctx_not_singleton(self):
        # output_mapping writes back to the parent ctx.variables, not the singleton.
        runtime = _make_runtime()
        sub_g = _two_node_graph("sub")
        sub_json = sub_g.to_json()
        parent_g = AgentGraph(id="parent2", name="parent2")
        parent_g.add_node("s", NodeConfig(type="start", label="S"))
        parent_g.add_node(
            "sub",
            NodeConfig(
                type="tool",
                label="sub",
                tool_name="__sub_graph__",
                tool_params={
                    "graph_json": sub_json,
                    "input_mapping": {},
                    "output_mapping": {"sub_result": "parent_back"},
                },
            ),
        )
        parent_g.add_node("e", NodeConfig(type="end", label="E"))
        parent_g.add_edge("s", "sub")
        parent_g.add_edge("sub", "e")
        parent_g.start_node_id = "s"
        ctx = AgentContext()
        async for _ in runtime.execute_graph(parent_g, "go", context=ctx):
            pass
        # output_mapping writes back to parent ctx (sub_result missing -> default "").
        assert ctx.variables.get("parent_back") == ""
        # The singleton is not written back to.
        assert runtime.variables.get("parent_back") == ""


# ── A-1 Tier1: exec reaper cleans stranded futures ──


class TestA1Reaper:
    @pytest.mark.asyncio
    async def test_reap_cancels_stranded_future(self):
        runtime = _make_runtime()
        fut = asyncio.get_event_loop().create_future()
        runtime._safety_futures["stray"] = fut
        runtime._register_exec_future("exec-X", "stray")
        runtime._reap_exec_futures("exec-X")
        assert fut.cancelled(), "reaper did not cancel stranded future"
        assert "stray" not in runtime._safety_futures

    @pytest.mark.asyncio
    async def test_reap_does_not_touch_other_exec(self):
        runtime = _make_runtime()
        fut_a = asyncio.get_event_loop().create_future()
        fut_b = asyncio.get_event_loop().create_future()
        runtime._safety_futures["a-key"] = fut_a
        runtime._safety_futures["b-key"] = fut_b
        runtime._register_exec_future("exec-A", "a-key")
        runtime._register_exec_future("exec-B", "b-key")
        # Reaping A must not touch B.
        runtime._reap_exec_futures("exec-A")
        assert fut_a.cancelled()
        assert not fut_b.cancelled()
        assert "b-key" in runtime._safety_futures


# ── checkpoint roundtrip preserves per-exec state ──


class TestA1CheckpointRoundtrip:
    def test_ctx_roundtrip_preserves_plan_mode_and_variables(self):
        ctx = AgentContext()
        ctx.plan_mode = True
        ctx.tool_call_chain_count = 4
        vm = VariableManager()
        vm.set("k", "v")
        ctx.variables = vm
        d = ctx.to_dict()
        assert d["plan_mode"] is True
        assert d["tool_call_chain_count"] == 4
        assert d["variables"].get("k") == "v"
        restored = AgentContext.from_dict(d)
        assert restored.plan_mode is True
        assert restored.tool_call_chain_count == 4
        assert restored.variables is not None
        assert restored.variables.get("k") == "v"
