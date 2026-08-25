"""审计 0825 Bundle3 回归 — A-1 per-exec runtime state + A-2 sub-runtime 隔离.

A-1: AgentRuntime 进程级单例; 执行态写实例属性, 并发图互踩.
  Tier1: _safety_futures/_plan_futures 跨 RPC 注册表 keyed by uuid, 移除 .clear().
  Tier2: plan_mode/_tool_call_chain_count per-exec -> ctx.
  Tier3: variables per-exec -> ctx (A-2: sub-runtime 不共享父变量).
A-2: 子 runtime 写 input_mapping 污染父 singleton -> 改写 ctx.variables + copy.
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
        # Graph A 注册一个 safety future (模拟待审批). Graph B 启动不应 clear 它.
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
        # A 进 plan_mode (只读), B 普通. B 启动后 A 的 ctx.plan_mode 不变.
        runtime = _make_runtime()
        ctx_a = AgentContext()
        ctx_a.plan_mode = True
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        assert ctx_a.plan_mode is True, "B's start reset A's plan_mode"

    @pytest.mark.asyncio
    async def test_tool_call_chain_count_independent(self):
        # A 跑中 chain_count=3, B 启动归零自己的 ctx, 不动 A.
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
        # A 在 runtime.variables 种 declared var, B 启动后 ctx.variables 是 copy.
        # B 执行中写 ctx.variables 不应回流 runtime.variables (singleton seed 干净).
        runtime = _make_runtime()
        runtime.variables.set("declared", "seed-val")
        ctx_b = AgentContext()
        async for _ in runtime.execute_graph(_two_node_graph("B"), "go", context=ctx_b):
            pass
        # B 的 ctx 拿到了 seed 的 copy.
        assert ctx_b.variables.get("declared") == "seed-val"
        # B 写自己的 ctx 变量.
        ctx_b.variables.set("b_only", "b-set")
        # singleton seed 不被 B 的执行污染.
        assert runtime.variables.get("b_only") == "", "B's ctx var bled to singleton"
        assert runtime.variables.get("declared") == "seed-val"

    @pytest.mark.asyncio
    async def test_sub_runtime_does_not_pollute_parent_singleton(self):
        # A-2 核心: 子图 input_mapping 解析写父 ctx.variables (非 singleton),
        # 子 runtime 的变量变更不回流父 singleton self.variables.
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
        # 父 singleton 不被子图 input_mapping 污染.
        assert runtime.variables.get("parent_val") == "P"
        # 父 ctx 拿到 input_mapping 解析 (sub_var=input 写父 ctx).
        # (input_mapping {parent_val: input} -> sub_input=P, 不写父 ctx 额外 var)

    @pytest.mark.asyncio
    async def test_sub_runtime_output_writes_parent_ctx_not_singleton(self):
        # output_mapping 回写父 ctx.variables, 不回流 singleton.
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
        # output_mapping 回写父 ctx (sub_result 不存在 -> 默认 "").
        assert ctx.variables.get("parent_back") == ""
        # singleton 不被回写.
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
        # reap A 不应动 B.
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
