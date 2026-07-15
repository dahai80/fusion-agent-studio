"""Tests for multi-agent orchestrator."""
from __future__ import annotations

import pytest

from agent_runtime.orchestrator import MultiAgentOrchestrator, AgentConfig, OrchestrationResult
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
    def __init__(self):
        self.call_count = 0

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse
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
        ctx = type("MockCtx", (), {"messages": [
            {"role": "assistant", "content": '["task1", "task2", "task3"]'},
        ]})()
        tasks = orchestrator._extract_sub_tasks(ctx, 3)
        assert len(tasks) == 3
        assert tasks[0] == "task1"

    async def test_extract_sub_tasks_from_text(self, orchestrator):
        ctx = type("MockCtx", (), {"messages": [
            {"role": "assistant", "content": "task1\ntask2\ntask3"},
        ]})()
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