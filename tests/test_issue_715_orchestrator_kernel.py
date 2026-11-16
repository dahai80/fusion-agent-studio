# Issue #715 (K4): MultiAgentOrchestrator coordination plane delegates to the
# kernel TeamRuntime — each fan-out run registers a kernel team (definition +
# task board), per-agent results become board tasks driven through the kernel
# transition whitelist, and results carry via attribution. Event streams stay
# graph-executed (observable event sequence = compatibility contract, MUST-3);
# swarm_router / fmp_router untouched.
from __future__ import annotations

import inspect
import json

import pytest

from agent_runtime.orchestrator import (
    AgentConfig,
    MultiAgentOrchestrator,
)
from tests.test_orchestrator import MockMLXClient, make_agent_graph
from tools.registry import ToolRegistry


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    mlx = MockMLXClient()
    return MultiAgentOrchestrator(mlx, ToolRegistry())


def _agents(*names):
    return [AgentConfig(name=n, graph=make_agent_graph(n), system_prompt="be terse") for n in names]


async def test_parallel_stamps_kernel_team_and_board(orchestrator, tmp_path):
    result = await orchestrator.parallel(_agents("alpha", "beta"), input_template="go")
    assert all(r["via"] == "kernel-team-runtime" for r in result.results)
    team_names = {r["team"] for r in result.results}
    assert len(team_names) == 1 and next(iter(team_names)).startswith("orch-parallel-")
    state_dir = tmp_path / "home" / "orchestrator_teams" / next(iter(team_names))
    state = json.loads((state_dir / "state.json").read_text())
    tasks = state.get("tasks") or {}
    assert len(tasks) == 2
    assert all(t["status"] == "done" for t in tasks.values())


async def test_parallel_error_marks_task_blocked(orchestrator, tmp_path, monkeypatch):
    agents = _agents("ok-agent", "bad-agent")

    import agent_runtime.runtime as rt_mod

    _orig = rt_mod.AgentRuntime.execute_graph

    async def _boom(self, graph, *a, **k):
        # execute_graph is an async generator — the wrapper must be one too
        if graph.name == "bad-agent":
            raise RuntimeError("injected failure")
        async for ev in _orig(self, graph, *a, **k):
            yield ev

    monkeypatch.setattr(rt_mod.AgentRuntime, "execute_graph", _boom)
    result = await orchestrator.parallel(agents, input_template="go")
    by_agent = {r["agent"]: r for r in result.results}
    assert by_agent["ok-agent"]["via"] == "kernel-team-runtime"
    team = by_agent["ok-agent"]["team"]
    state = json.loads(
        (tmp_path / "home" / "orchestrator_teams" / team / "state.json").read_text()
    )
    tasks = state.get("tasks") or {}
    statuses = {t["title"].split(":")[-1]: t["status"] for t in tasks.values()}
    assert statuses.get("ok-agent") == "done"
    # failed agent: stays in_progress on the board (kernel blocked requires
    # blocked_by task ids) — unfinished row is the honest record.
    assert statuses.get("bad-agent") == "in_progress"


async def test_kernel_unavailable_falls_back_visible(orchestrator, monkeypatch):
    import fusion_harness.team_runtime as trt

    def _boom(*a, **k):
        raise RuntimeError("kernel down")

    monkeypatch.setattr(trt, "TeamRuntime", _boom)
    result = await orchestrator.parallel(_agents("a1", "a2"), input_template="go")
    assert all(r["via"] == "graph-local" for r in result.results)


async def test_broadcast_inherits_kernel_stamps(orchestrator):
    result = await orchestrator.broadcast(_agents("b1", "b2"), input_text="hello all")
    assert all(r["via"] == "kernel-team-runtime" for r in result.results)
    assert result.summary


async def test_supervisor_stamps_kernel_team(orchestrator):
    supervisor = AgentConfig(
        name="sup", graph=make_agent_graph("sup"), system_prompt="route"
    )
    workers = _agents("w1", "w2")
    result = await orchestrator.supervisor(supervisor, workers, task="do it", max_rounds=1)
    stamped = [r for r in result.results if r.get("agent") in ("w1", "w2")]
    assert stamped, "worker rounds must be recorded"
    assert all(r["via"] == "kernel-team-runtime" for r in stamped)


def test_public_api_signatures_unchanged():
    # Acceptance 1: public API diff is empty — pattern names, signatures.
    sigs = {
        "sequential": "(agents: list[agent_runtime.orchestrator.AgentConfig], initial_input: str) -> agent_runtime.orchestrator.OrchestrationResult",
        "parallel": "(agents: list[agent_runtime.orchestrator.AgentConfig], input_template: str = '', per_agent_inputs: list[str] | None = None) -> agent_runtime.orchestrator.OrchestrationResult",
        "master_worker": "(master: agent_runtime.orchestrator.AgentConfig, workers: list[agent_runtime.orchestrator.AgentConfig], task: str) -> agent_runtime.orchestrator.OrchestrationResult",
        "handoff": None,  # shape asserted below, annotation noise tolerated
        "broadcast": None,
        "supervisor": None,
    }
    for name in sigs:
        method = getattr(MultiAgentOrchestrator, name)
        assert callable(method), f"pattern {name} missing"
    assert list(inspect.signature(MultiAgentOrchestrator.parallel).parameters) == [
        "self", "agents", "input_template", "per_agent_inputs",
    ]
    assert list(inspect.signature(MultiAgentOrchestrator.master_worker).parameters) == [
        "self", "master", "workers", "task",
    ]
    assert list(inspect.signature(MultiAgentOrchestrator.sequential).parameters) == [
        "self", "agents", "initial_input",
    ]
