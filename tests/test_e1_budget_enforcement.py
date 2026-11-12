"""E1: session-level run budget enforcement (budget as a first-class run object).

RunBudget dims (max_tokens / max_cost_usd / max_wall_s) are checked after
each llm node; exceeding any dim emits BUDGET_REACHED, sets stop_reason
budget_reached, and the managed-run pump parks the run (usage_limit frame)
plus fires the run.budget_reached webhook.
"""

from __future__ import annotations

import pytest

from agent_runtime.budget_engine import (
    RunBudget,
    RunBudgetTracker,
    check_run_budget,
    estimate_run_cost,
)
from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


class MockMLXClient:
    def __init__(self, usage=None):
        self.usage = usage or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        self.call_count = 0

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(content="ok", tool_calls=[], usage=dict(self.usage))


def _llm_graph() -> AgentGraph:
    g = AgentGraph(id="g_e1", name="e1")
    g.add_node("start", NodeConfig(type="start", label="Start"))
    g.add_node("a", NodeConfig(type="llm", label="a", model="m"))
    g.add_node("b", NodeConfig(type="llm", label="b", model="m"))
    g.add_node("end", NodeConfig(type="end", label="End"))
    g.add_edge("start", "a")
    g.add_edge("a", "b")
    g.add_edge("b", "end")
    g.start_node_id = "start"
    return g


class TestRunBudget:
    def test_from_dict_and_active(self):
        assert not RunBudget().active()
        assert RunBudget(max_tokens=100).active()
        assert RunBudget.from_dict({"max_tokens": "50"}).max_tokens == 50
        assert RunBudget.from_dict(None).active() is False
        assert RunBudget.from_dict({"bogus": "x"}).active() is False

    def test_to_dict_roundtrip(self):
        b = RunBudget(max_tokens=10, max_cost_usd=0.5, max_wall_s=60)
        assert RunBudget.from_dict(b.to_dict()) == b


class TestCheckRunBudget:
    def test_token_dim(self):
        s = check_run_budget(
            RunBudget(max_tokens=100),
            {"prompt_tokens": 60, "completion_tokens": 40},
            elapsed_s=1,
        )
        assert s.exceeded and s.dims == ["max_tokens"] and s.spent_tokens == 100

    def test_cost_dim_uses_rate_table(self, monkeypatch):
        monkeypatch.setenv(
            "FUSION_RUN_COST_RATES", json_dumps({"mymodel": [1.0, 2.0]})
        )
        s = check_run_budget(
            RunBudget(max_cost_usd=0.001),
            {"prompt_tokens": 500, "completion_tokens": 500},
            elapsed_s=1,
            model="mymodel",
        )
        assert s.exceeded and "max_cost_usd" in s.dims
        assert s.spent_cost_usd == pytest.approx((500 * 1.0 + 500 * 2.0) / 1_000_000)

    def test_wall_dim(self):
        s = check_run_budget(RunBudget(max_wall_s=5), {}, elapsed_s=6)
        assert s.exceeded and s.dims == ["max_wall_s"]

    def test_reasoning_tokens_counted(self):
        s = check_run_budget(
            RunBudget(max_tokens=100),
            {"prompt_tokens": 80, "completion_tokens": 10, "reasoning_tokens": 10},
            elapsed_s=1,
        )
        assert s.exceeded

    def test_default_rates_zero_cost_never_exceeds(self):
        s = check_run_budget(
            RunBudget(max_cost_usd=0.0), {"prompt_tokens": 10**9}, elapsed_s=1
        )
        assert not s.exceeded


def json_dumps(obj):
    import json

    return json.dumps(obj)


class TestEstimateRunCost:
    def test_default_rate_zero(self):
        assert estimate_run_cost("qwen", 1000, 1000) == 0.0

    def test_env_prefix_match(self, monkeypatch):
        monkeypatch.setenv(
            "FUSION_RUN_COST_RATES",
            json_dumps({"qwen": [2.0, 4.0], "default": [1.0, 1.0]}),
        )
        assert estimate_run_cost("qwen3-4b", 1_000_000, 0) == pytest.approx(2.0)
        assert estimate_run_cost("other", 1_000_000, 1_000_000) == pytest.approx(2.0)

    def test_env_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("FUSION_RUN_COST_RATES", "not-json")
        assert estimate_run_cost("m", 10, 10) == 0.0


class TestTracker:
    def test_tracker_wall_and_status(self):
        t = RunBudgetTracker(RunBudget(max_wall_s=5))
        t.started_at -= 10
        s = t.check({}, model="m")
        assert s.exceeded and s.dims == ["max_wall_s"]
        assert t.last_status is s


class TestRuntimeEnforcement:
    def _rt(self) -> AgentRuntime:
        return AgentRuntime(mlx_client=MockMLXClient(), tool_registry=ToolRegistry())

    async def test_budget_reached_stops_run(self):
        rt = self._rt()
        events = []
        async for ev in rt.execute_graph(
            _llm_graph(), "go", run_budget={"max_tokens": 15}
        ):
            events.append(ev)
        kinds = [e.type for e in events]
        assert AgentEventType.BUDGET_REACHED in kinds
        # stop right after the first llm node crosses the cap — second llm
        # node never calls the model
        assert rt.mlx.call_count == 1

    async def test_no_budget_runs_to_completion(self):
        rt = self._rt()
        kinds = []
        async for ev in rt.execute_graph(_llm_graph(), "go"):
            kinds.append(ev.type)
        assert AgentEventType.BUDGET_REACHED not in kinds
        assert kinds[-1] == AgentEventType.END

    async def test_budget_dict_invalid_ignored(self):
        rt = self._rt()
        kinds = []
        async for ev in rt.execute_graph(_llm_graph(), "go", run_budget={"bogus": 1}):
            kinds.append(ev.type)
        assert AgentEventType.BUDGET_REACHED not in kinds

    async def test_stop_reason_budget_reached(self):
        rt = self._rt()
        ctx = AgentContext()
        async for _ in rt.execute_graph(
            _llm_graph(), "go", context=ctx, run_budget={"max_tokens": 1}
        ):
            pass
        assert ctx.stop_reason == "budget_reached"


class TestManagedRunPark:
    def _run_stub(self):
        from agent_runtime.run_registry import ManagedRun

        return ManagedRun("g_e1")

    async def test_budget_reached_parks_and_fires_webhook(self, monkeypatch, tmp_path):
        fired = []

        class FakeRegistry:
            async def fire(self, event, payload):
                fired.append((event, payload))

        import agent_runtime.agent_platform.webhooks as wh

        monkeypatch.setattr(wh, "get_webhook_registry", lambda: FakeRegistry())
        monkeypatch.setenv("FUSION_PARKED_RUNS_PATH", str(tmp_path / "parked.json"))

        class StubRuntime:
            async def execute_graph_stream(self, graph, input, **kwargs):
                yield _fake_event(
                    AgentEventType.BUDGET_REACHED,
                    metadata={"dims": ["max_tokens"]},
                )

        from agent_runtime.api_server import _execute_managed_run

        run = self._run_stub()
        await _execute_managed_run(
            run, StubRuntime(), _llm_graph(), "go",
            run_budget={"max_tokens": 15},
        )
        assert run.parked and run.park_reason == "usage_limit"
        types = [f.get("type") for f in run.buffer]
        assert "usage_limit" in types and "parked" in types
        assert fired and fired[0][0] == "run.budget_reached"
        assert fired[0][1]["run_id"] == run.run_id
        assert fired[0][1]["dims"] == ["max_tokens"]

    async def test_normal_run_not_parked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_PARKED_RUNS_PATH", str(tmp_path / "parked.json"))

        class StubRuntime:
            async def execute_graph_stream(self, graph, input, **kwargs):
                yield _fake_event(AgentEventType.RESULT, content="done")

        from agent_runtime.api_server import _execute_managed_run

        run = self._run_stub()
        await _execute_managed_run(run, StubRuntime(), _llm_graph(), "go")
        assert not run.parked and run.done


def _fake_event(ev_type, content="", metadata=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        type=ev_type,
        content=content,
        metadata=metadata or {},
        to_dict=lambda: {
            "type": ev_type.value,
            "content": content,
            "metadata": metadata or {},
        },
    )
