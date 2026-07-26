"""Tests for agent runtime engine."""
from __future__ import annotations

import pytest

from agent_runtime.context import AgentContext, AgentEvent, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry
from tools.base import BaseTool


class MockTool(BaseTool):
    name = "mock_tool"
    description = "A mock tool for testing"
    parameters = {"input": {"type": "string", "description": "Input"}}

    async def execute(self, **kwargs) -> str:
        return f"Executed: {kwargs.get('input', '')}"


class FailingTool(BaseTool):
    name = "failing_tool"
    description = "A tool that always fails"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        raise RuntimeError("Tool failed")


class MockMLXClient:
    """Mock fusion-mlx HTTP client for testing."""

    def __init__(self):
        self.call_count = 0
        self.responses = []

    def add_response(self, content: str, tool_calls: list | None = None):
        self.responses.append({"content": content, "tool_calls": tool_calls or []})

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        if not self.responses:
            from server.fusion_mlx_client import LLMResponse
            return LLMResponse(content="Final answer", tool_calls=[])
        resp = self.responses.pop(0)
        from server.fusion_mlx_client import LLMResponse
        return LLMResponse(content=resp["content"], tool_calls=resp["tool_calls"])


@pytest.fixture
def tool_registry():
    registry = ToolRegistry()
    registry.register(MockTool())
    registry.register(FailingTool())
    return registry


@pytest.fixture
def mlx_client():
    return MockMLXClient()


@pytest.fixture
def simple_graph():
    graph = AgentGraph(name="Simple Test")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    return graph


class TestAgentRuntime:
    async def test_execute_simple_graph(self, mlx_client, tool_registry, simple_graph):
        mlx_client.add_response("Hello, world!")
        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Hi"):
            events.append(event)

        assert len(events) >= 2
        assert events[0].type == AgentEventType.START
        assert any(e.type == AgentEventType.END for e in events)

        # LLM should have been called
        assert mlx_client.call_count == 1

    async def test_execute_with_tool_call(self, mlx_client, tool_registry, simple_graph):
        # First response has a tool call, second is final
        mlx_client.add_response(
            "Let me check", tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "mock_tool", "arguments": '{"input": "test"}'},
            }]
        )
        mlx_client.add_response("Here is the result")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Check something"):
            events.append(event)

        # Should have tool_call and tool_result events
        tool_calls = [e for e in events if e.type == AgentEventType.TOOL_CALL]
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_calls) >= 1
        assert len(tool_results) >= 1
        assert tool_calls[0].name == "mock_tool"

    async def test_execute_with_invalid_tool_json(self, mlx_client, tool_registry, simple_graph):
        mlx_client.add_response(
            "Let me check", tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "mock_tool", "arguments": "invalid json"},
            }]
        )

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Check"):
            events.append(event)

        # Should error on invalid JSON
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1

    async def test_execute_with_nonexistent_tool(self, mlx_client, tool_registry, simple_graph):
        mlx_client.add_response(
            "Let me check", tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "nonexistent_tool", "arguments": '{}'},
            }]
        )
        mlx_client.add_response("Done")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Check"):
            events.append(event)

        # Should handle missing tool gracefully
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1
        assert "not found" in tool_results[0].content

    async def test_execute_with_failing_tool(self, mlx_client, tool_registry, simple_graph):
        mlx_client.add_response(
            "Let me check", tool_calls=[{
                "id": "call_2",
                "type": "function",
                "function": {"name": "failing_tool", "arguments": '{}'},
            }]
        )
        mlx_client.add_response("Done")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Check"):
            events.append(event)

        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1
        assert "Error" in tool_results[0].content

    async def test_execute_invalid_graph(self, mlx_client, tool_registry):
        graph = AgentGraph(name="Empty")
        # No nodes -> validation error
        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(graph, "Hi"):
            events.append(event)

        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1

    async def test_execute_graph_no_llm_model(self, mlx_client, tool_registry):
        graph = AgentGraph(name="No LLM")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "end")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(graph, "Hi"):
            events.append(event)

        # No LLM nodes -> should just run through start -> end
        end_events = [e for e in events if e.type == AgentEventType.END]
        assert len(end_events) >= 1

    async def test_execute_with_existing_context(self, mlx_client, tool_registry, simple_graph):
        mlx_client.add_response("Continuing...")
        ctx = AgentContext(session_id="existing-session")
        ctx.add_message("user", "Previous message")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "New input", ctx):
            events.append(event)

        # Context should have been reused
        assert ctx.session_id == "existing-session"
        assert ctx.iteration_count > 0

    async def test_execute_node_condition_true(self, mlx_client, tool_registry):
        graph = AgentGraph(name="Condition Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("cond", NodeConfig(
            type="condition", label="Check", condition_expr="true",
        ))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "cond")
        graph.add_edge("cond", "end", "true")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(graph, "Test"):
            events.append(event)

        # Condition node runs, no LLM needed, should reach END
        end_events = [e for e in events if e.type == AgentEventType.END]
        assert len(end_events) >= 1

    async def test_execute_tool_node(self, mlx_client, tool_registry):
        graph = AgentGraph(name="Tool Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("tool_node", NodeConfig(
            type="tool", label="Read", tool_name="mock_tool",
            tool_params={"input": "hello"},
        ))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "tool_node")
        graph.add_edge("tool_node", "end")

        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(graph, ""):
            events.append(event)

        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1
        assert "Executed" in tool_results[0].content

    async def test_max_iterations(self, mlx_client, tool_registry):
        """Test that max iterations limit is respected."""
        graph = AgentGraph(name="Loop Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "llm")  # self-loop -> infinite
        graph.add_edge("llm", "end")

        # Always respond with tool calls to keep looping
        mlx_client.add_response(
            "Thinking...", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "mock_tool", "arguments": '{"input": "x"}'},
            }],
        )

        runtime = AgentRuntime(mlx_client, tool_registry, max_iterations=3)
        events = []
        async for event in runtime.execute_graph(graph, "Loop"):
            events.append(event)

        # Should have hit max iterations
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1

    async def test_llm_call_failure(self, mlx_client, tool_registry, simple_graph):
        """Test that LLM call failures are handled."""
        runtime = AgentRuntime(mlx_client, tool_registry)

        # MockMLXClient with no responses returns empty LLMResponse
        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "Hi"):
            events.append(event)

        # Should still complete (empty response is valid)
        end_events = [e for e in events if e.type == AgentEventType.END]
        assert len(end_events) >= 1

    async def test_start_node_system_prompt(self, mlx_client, tool_registry):
        graph = AgentGraph(name="System Prompt Test")
        graph.add_node("start", NodeConfig(
            type="start", label="Start", system_prompt="You are a test assistant.",
        ))
        graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "end")

        mlx_client.add_response("OK")
        runtime = AgentRuntime(mlx_client, tool_registry)
        events = []
        async for event in runtime.execute_graph(graph, "Test"):
            events.append(event)

        assert mlx_client.call_count == 1

    def test_evaluate_condition_true(self, mlx_client, tool_registry):
        runtime = AgentRuntime(mlx_client, tool_registry)
        ctx = AgentContext()
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("true", ctx, vm) == "true"
        assert runtime.condition_engine.evaluate("false", ctx, vm) == "false"

    def test_evaluate_condition_has_tool_calls(self, mlx_client, tool_registry):
        runtime = AgentRuntime(mlx_client, tool_registry)
        ctx = AgentContext()
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("has_tool_calls", ctx, vm) == "false"
        ctx.add_message("assistant", "test", tool_calls=[{"id": "1"}])
        assert runtime.condition_engine.evaluate("has_tool_calls", ctx, vm) == "true"

    def test_evaluate_condition_has_error(self, mlx_client, tool_registry):
        runtime = AgentRuntime(mlx_client, tool_registry)
        ctx = AgentContext()
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("has_error", ctx, vm) == "false"
        ctx.error = "error"
        assert runtime.condition_engine.evaluate("has_error", ctx, vm) == "true"

    def test_evaluate_condition_iteration(self, mlx_client, tool_registry):
        runtime = AgentRuntime(mlx_client, tool_registry)
        ctx = AgentContext()
        ctx.iteration_count = 5
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("iteration >= 3", ctx, vm) == "true"
        assert runtime.condition_engine.evaluate("iteration >= 10", ctx, vm) == "false"
        assert runtime.condition_engine.evaluate("iteration <= 5", ctx, vm) == "true"
        assert runtime.condition_engine.evaluate("iteration <= 3", ctx, vm) == "false"
        assert runtime.condition_engine.evaluate("iteration > 3", ctx, vm) == "true"
        assert runtime.condition_engine.evaluate("iteration < 5", ctx, vm) == "false"
        assert runtime.condition_engine.evaluate("iteration == 5", ctx, vm) == "true"
        assert runtime.condition_engine.evaluate("iteration == 3", ctx, vm) == "false"

    def test_evaluate_condition_unknown(self, mlx_client, tool_registry):
        runtime = AgentRuntime(mlx_client, tool_registry)
        ctx = AgentContext()
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("unknown_expression", ctx, vm) == "false"