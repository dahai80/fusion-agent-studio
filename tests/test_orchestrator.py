"""Tests for multi-agent orchestrator."""

from __future__ import annotations

import pytest

from agent_runtime.orchestrator import (
    MultiAgentOrchestrator,
    AgentConfig,
    OrchestrationResult,
    HandoffContext,
)
from agent_runtime.graph import AgentGraph, NodeConfig
from tools.registry import ToolRegistry
from tools.base import BaseTool


class MockTool(BaseTool):
    name = "mock_tool"
    description = "Test tool"
    parameters = {"input": {"type": "string"}}

    async def execute(self, **kwargs) -> str:
        return "tool result"


class MockMLXClient:
    def __init__(self, responses=None):
        self.call_count = 0
        self.responses = responses or []

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse

        if self.responses:
            idx = min(self.call_count - 1, len(self.responses) - 1)
            return self.responses[idx]
        return LLMResponse(content="Final answer", tool_calls=[])


@pytest.fixture
def orchestrator():
    mlx = MockMLXClient()
    reg = ToolRegistry()
    reg.register(MockTool())
    return MultiAgentOrchestrator(mlx, reg)


def make_agent_graph(name: str) -> AgentGraph:
    graph = AgentGraph(name=name)
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    return graph


class TestMultiAgentOrchestrator:
    async def test_sequential_single_agent(self, orchestrator):
        agent = AgentConfig(name="agent1", graph=make_agent_graph("Agent 1"))
        result = await orchestrator.sequential([agent], "Hello")
        assert isinstance(result, OrchestrationResult)
        assert len(result.results) == 1
        assert result.results[0]["agent"] == "agent1"

    async def test_sequential_multiple_agents(self, orchestrator):
        agents = [
            AgentConfig(name="a1", graph=make_agent_graph("A1")),
            AgentConfig(name="a2", graph=make_agent_graph("A2")),
        ]
        result = await orchestrator.sequential(agents, "Start")
        assert len(result.results) == 2
        assert result.results[0]["agent"] == "a1"
        assert result.results[1]["agent"] == "a2"

    async def test_parallel(self, orchestrator):
        agents = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
            AgentConfig(name="w2", graph=make_agent_graph("W2")),
        ]
        result = await orchestrator.parallel(agents, "Do work")
        assert len(result.results) == 2

    async def test_sequential_empty(self, orchestrator):
        result = await orchestrator.sequential([], "Nothing")
        assert len(result.results) == 0

    async def test_parallel_empty(self, orchestrator):
        result = await orchestrator.parallel([], "Nothing")
        assert len(result.results) == 0

    async def test_agent_config_defaults(self):
        config = AgentConfig(name="test", graph=make_agent_graph("test"))
        assert config.system_prompt == ""
        assert config.model == ""

    async def test_orchestration_result_defaults(self):
        result = OrchestrationResult()
        assert result.results == []
        assert result.errors == []
        assert result.total_duration == 0.0
        assert result.summary == ""

    async def test_extract_sub_tasks_from_json(self, orchestrator):
        ctx = type(
            "MockCtx",
            (),
            {
                "messages": [
                    {"role": "assistant", "content": '["task1", "task2", "task3"]'},
                ]
            },
        )()
        tasks = orchestrator._extract_sub_tasks(ctx, 3)
        assert len(tasks) == 3
        assert tasks[0] == "task1"

    async def test_extract_sub_tasks_from_text(self, orchestrator):
        ctx = type(
            "MockCtx",
            (),
            {
                "messages": [
                    {"role": "assistant", "content": "task1\ntask2\ntask3"},
                ]
            },
        )()
        tasks = orchestrator._extract_sub_tasks(ctx, 2)
        assert len(tasks) == 2

    async def test_extract_sub_tasks_empty(self, orchestrator):
        ctx = type("MockCtx", (), {"messages": []})()
        tasks = orchestrator._extract_sub_tasks(ctx, 3)
        assert len(tasks) == 3
        assert tasks[0] == "Sub-task 1"

    async def test_master_worker(self, orchestrator):
        master = AgentConfig(name="master", graph=make_agent_graph("Master"))
        workers = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
        ]
        result = await orchestrator.master_worker(master, workers, "Do something")
        assert len(result.results) >= 2
        assert result.results[0]["phase"] == "decomposition"


class TestHandoffPattern:
    async def test_handoff_chain(self, orchestrator):
        agents = [
            AgentConfig(name="agent_a", graph=make_agent_graph("A")),
            AgentConfig(name="agent_b", graph=make_agent_graph("B")),
        ]
        result = await orchestrator.handoff(agents, "Start task")
        assert len(result.results) == 2
        assert result.results[0]["agent"] == "agent_a"
        assert result.results[0]["handoff_to"] == "agent_b"
        assert result.results[1]["handoff_to"] == "__end__"

    async def test_handoff_single_agent(self, orchestrator):
        agents = [AgentConfig(name="solo", graph=make_agent_graph("Solo"))]
        result = await orchestrator.handoff(agents, "Do it")
        assert len(result.results) == 1
        assert result.results[0]["handoff_to"] == "__end__"

    async def test_handoff_early_completion(self):
        from server.fusion_mlx_client import LLMResponse

        mlx = MockMLXClient(
            responses=[
                LLMResponse(content="Done [COMPLETE]", tool_calls=[]),
                LLMResponse(content="Should not reach", tool_calls=[]),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        agents = [
            AgentConfig(name="a1", graph=make_agent_graph("A1")),
            AgentConfig(name="a2", graph=make_agent_graph("A2")),
        ]
        result = await orch.handoff(agents, "Task")
        assert len(result.results) == 1
        assert result.results[0]["agent"] == "a1"

    async def test_handoff_error_stops_chain(self):
        from unittest.mock import patch
        from agent_runtime.runtime import AgentRuntime

        reg = ToolRegistry()
        reg.register(MockTool())

        agents = [
            AgentConfig(name="fail_agent", graph=make_agent_graph("Fail")),
            AgentConfig(name="never_runs", graph=make_agent_graph("Never")),
        ]

        with patch.object(
            AgentRuntime, "execute_graph", side_effect=RuntimeError("boom")
        ):
            mlx = MockMLXClient()
            orch = MultiAgentOrchestrator(mlx, reg)
            result = await orch.handoff(agents, "Task")
            assert len(result.errors) == 1
            assert len(result.results) == 0

    async def test_handoff_context_dataclass(self):
        ctx = HandoffContext(
            sender="a", receiver="b", content="hello", metadata={"k": 1}, timestamp=1.0
        )
        assert ctx.sender == "a"
        assert ctx.receiver == "b"
        assert ctx.content == "hello"

    async def test_format_handoff_history(self, orchestrator):
        chain = [
            HandoffContext(sender="a1", receiver="a2", content="step1 done"),
            HandoffContext(sender="a2", receiver="__end__", content="step2 done"),
        ]
        text = orchestrator._format_handoff_history(chain)
        assert "a1 -> a2" in text
        assert "a2 -> __end__" in text
        assert "step1 done" in text

    async def test_is_handoff_complete_markers(self, orchestrator):
        assert orchestrator._is_handoff_complete("result [COMPLETE]")
        assert orchestrator._is_handoff_complete("[DONE]")
        assert orchestrator._is_handoff_complete("[TASK_COMPLETE] now")
        assert orchestrator._is_handoff_complete("[NO_HANDOFF]")
        assert not orchestrator._is_handoff_complete("still working")


class TestBroadcastPattern:
    async def test_broadcast_concat(self, orchestrator):
        agents = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
            AgentConfig(name="w2", graph=make_agent_graph("W2")),
        ]
        result = await orchestrator.broadcast(agents, "Analyze this")
        assert len(result.results) == 2
        assert "w1" in result.summary
        assert "w2" in result.summary

    async def test_broadcast_best(self, orchestrator):
        agents = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
        ]
        result = await orchestrator.broadcast(agents, "Analyze", merge_strategy="best")
        assert len(result.results) == 1

    async def test_broadcast_json(self, orchestrator):
        agents = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
        ]
        result = await orchestrator.broadcast(agents, "Analyze", merge_strategy="json")
        assert "w1" in result.summary

    async def test_broadcast_empty(self, orchestrator):
        result = await orchestrator.broadcast([], "Empty")
        assert len(result.results) == 0

    async def test_merge_outputs_concat(self, orchestrator):
        outputs = {"a": "hello", "b": "world"}
        merged = orchestrator._merge_outputs(outputs, "concat")
        assert "=== a ===" in merged
        assert "hello" in merged
        assert "world" in merged

    async def test_merge_outputs_best(self, orchestrator):
        outputs = {"short": "hi", "long": "hello world this is longer"}
        merged = orchestrator._merge_outputs(outputs, "best")
        assert merged == "hello world this is longer"

    async def test_merge_outputs_best_empty(self, orchestrator):
        merged = orchestrator._merge_outputs({}, "best")
        assert merged == ""

    async def test_merge_outputs_json(self, orchestrator):
        import json

        outputs = {"a": "hello", "b": "world"}
        merged = orchestrator._merge_outputs(outputs, "json")
        parsed = json.loads(merged)
        assert parsed["a"] == "hello"

    async def test_merge_outputs_unknown_strategy(self, orchestrator):
        outputs = {"a": "hello"}
        merged = orchestrator._merge_outputs(outputs, "unknown")
        assert "hello" in merged


class TestSupervisorPattern:
    async def test_supervisor_single_round_done(self):
        from server.fusion_mlx_client import LLMResponse

        mlx = MockMLXClient(
            responses=[
                LLMResponse(
                    content='{"worker": "__end__", "instruction": "task done", "done": true}',
                    tool_calls=[],
                ),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        supervisor = AgentConfig(name="boss", graph=make_agent_graph("Boss"))
        workers = [AgentConfig(name="w1", graph=make_agent_graph("W1"))]
        result = await orch.supervisor(supervisor, workers, "Do task")
        assert len(result.results) >= 1
        assert any(r.get("action") == "completed" for r in result.results)

    async def test_supervisor_routes_to_worker(self):
        from server.fusion_mlx_client import LLMResponse

        mlx = MockMLXClient(
            responses=[
                LLMResponse(
                    content='{"worker": "w1", "instruction": "do subtask", "done": false}',
                    tool_calls=[],
                ),
                LLMResponse(
                    content='{"worker": "__end__", "instruction": "all done", "done": true}',
                    tool_calls=[],
                ),
                LLMResponse(content="worker output", tool_calls=[]),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        supervisor = AgentConfig(name="boss", graph=make_agent_graph("Boss"))
        workers = [AgentConfig(name="w1", graph=make_agent_graph("W1"))]
        result = await orch.supervisor(supervisor, workers, "Complex task")
        assert len(result.results) >= 2

    async def test_supervisor_timeout(self):
        from server.fusion_mlx_client import LLMResponse

        mlx = MockMLXClient(
            responses=[
                LLMResponse(
                    content='{"worker": "w1", "instruction": "keep going", "done": false}',
                    tool_calls=[],
                ),
                LLMResponse(
                    content='{"worker": "w1", "instruction": "keep going", "done": false}',
                    tool_calls=[],
                ),
                LLMResponse(content="worker output", tool_calls=[]),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        supervisor = AgentConfig(name="boss", graph=make_agent_graph("Boss"))
        workers = [AgentConfig(name="w1", graph=make_agent_graph("W1"))]
        result = await orch.supervisor(supervisor, workers, "Never done", max_rounds=2)
        assert any(r.get("action") == "timeout" for r in result.results)

    async def test_supervisor_unknown_worker_fallback(self):
        from server.fusion_mlx_client import LLMResponse

        mlx = MockMLXClient(
            responses=[
                LLMResponse(
                    content='{"worker": "unknown_worker", "instruction": "try", "done": false}',
                    tool_calls=[],
                ),
                LLMResponse(
                    content='{"worker": "__end__", "instruction": "done", "done": true}',
                    tool_calls=[],
                ),
                LLMResponse(content="fallback output", tool_calls=[]),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        supervisor = AgentConfig(name="boss", graph=make_agent_graph("Boss"))
        workers = [AgentConfig(name="w1", graph=make_agent_graph("W1"))]
        result = await orch.supervisor(supervisor, workers, "Route to unknown")
        assert len(result.results) >= 1

    async def test_parse_route_decision_valid_json(self, orchestrator):
        output = '{"worker": "w1", "instruction": "do it", "done": false}'
        decision = orchestrator._parse_route_decision(output)
        assert decision["worker"] == "w1"

    async def test_parse_route_decision_code_block(self, orchestrator):
        output = '```json\n{"worker": "w1", "instruction": "do it", "done": false}\n```'
        decision = orchestrator._parse_route_decision(output)
        assert decision["worker"] == "w1"

    async def test_parse_route_decision_invalid(self, orchestrator):
        decision = orchestrator._parse_route_decision("not json at all")
        assert decision["done"] is False

    async def test_supervisor_worker_error(self):
        from server.fusion_mlx_client import LLMResponse
        from unittest.mock import patch
        from agent_runtime.runtime import AgentRuntime

        mlx = MockMLXClient(
            responses=[
                LLMResponse(
                    content='{"worker": "w1", "instruction": "do it", "done": false}',
                    tool_calls=[],
                ),
                LLMResponse(
                    content='{"worker": "__end__", "instruction": "done", "done": true}',
                    tool_calls=[],
                ),
            ]
        )
        reg = ToolRegistry()
        reg.register(MockTool())
        orch = MultiAgentOrchestrator(mlx, reg)

        supervisor = AgentConfig(name="boss", graph=make_agent_graph("Boss"))
        workers = [AgentConfig(name="w1", graph=make_agent_graph("W1"))]

        call_count = 0
        original_execute = AgentRuntime.execute_graph

        async def patched_execute(self, graph, input_text, ctx=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("worker crashed")
            async for event in original_execute(self, graph, input_text, ctx):
                yield event

        with patch.object(AgentRuntime, "execute_graph", patched_execute):
            result = await orch.supervisor(supervisor, workers, "Error task")
            assert len(result.errors) >= 1


class TestCompositionWireIn:
    # Orchestrator optionally wires SwarmRouter (handoff) + Plaza (broadcast).

    async def test_handoff_with_swarm_router_injected(self):
        from agent_runtime.swarm_router import SwarmRouter

        mlx = MockMLXClient()
        reg = ToolRegistry()
        reg.register(MockTool())
        swarm = SwarmRouter()
        orch = MultiAgentOrchestrator(mlx, reg, swarm_router=swarm)
        agents = [
            AgentConfig(name="a1", graph=make_agent_graph("A1")),
            AgentConfig(name="a2", graph=make_agent_graph("A2")),
        ]
        result = await orch.handoff(agents, "Task")
        assert len(result.results) == 2
        assert swarm.get_agent("a1") is not None
        assert swarm.get_agent("a2") is not None
        assert swarm.fmp._stats["sent"] >= 1

    async def test_handoff_swarm_blocks_at_max_hops(self):
        from agent_runtime.swarm_router import SwarmRouter

        mlx = MockMLXClient()
        reg = ToolRegistry()
        reg.register(MockTool())
        swarm = SwarmRouter(max_hops=1)
        orch = MultiAgentOrchestrator(mlx, reg, swarm_router=swarm)
        agents = [
            AgentConfig(name=f"a{i}", graph=make_agent_graph(f"A{i}")) for i in range(4)
        ]
        result = await orch.handoff(agents, "Task")
        assert any(r.get("swarm_blocked") for r in result.results)
        assert len(result.results) < 4

    async def test_broadcast_with_plaza_injected(self):
        from agent_runtime.plaza import Plaza

        mlx = MockMLXClient()
        reg = ToolRegistry()
        reg.register(MockTool())

        class _SpyPlaza(Plaza):
            def __init__(self):
                super().__init__()
                self.broadcast_count = 0
                self.created = []

            def create_channel(self, name, participants):
                self.created.append(name)
                return super().create_channel(name, participants)

            def broadcast(self, channel, sender, content, mentions=None):
                self.broadcast_count += 1
                return super().broadcast(channel, sender, content, mentions)

        plaza = _SpyPlaza()
        orch = MultiAgentOrchestrator(mlx, reg, plaza=plaza)
        agents = [
            AgentConfig(name="w1", graph=make_agent_graph("W1")),
            AgentConfig(name="w2", graph=make_agent_graph("W2")),
        ]
        result = await orch.broadcast(agents, "Analyze")
        assert len(result.results) == 2
        assert len(plaza.created) == 1
        assert plaza.broadcast_count == 2
