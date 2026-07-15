"""Tests for node executor."""
from __future__ import annotations

import pytest

from agent_runtime.executor import NodeExecutor
from agent_runtime.context import AgentContext
from agent_runtime.graph import NodeConfig
from tools.registry import ToolRegistry
from tools.base import BaseTool


class MockTool(BaseTool):
    name = "mock_tool"
    description = "Test tool"
    parameters = {"input": {"type": "string"}}

    async def execute(self, **kwargs) -> str:
        return f"Result: {kwargs.get('input', '')}"


class MockMLXClient:
    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        from server.fusion_mlx_client import LLMResponse
        return LLMResponse(content="Mock response", tool_calls=[])


@pytest.fixture
def executor():
    mlx = MockMLXClient()
    reg = ToolRegistry()
    reg.register(MockTool())
    return NodeExecutor(mlx, reg)


class TestNodeExecutor:
    async def test_get_handler(self, executor):
        handler = executor.get_handler("start")
        assert callable(handler)

    async def test_get_handler_invalid(self, executor):
        with pytest.raises(ValueError, match="Unknown node type"):
            executor.get_handler("invalid")

    async def test_handle_start(self, executor):
        node = NodeConfig(type="start")
        ctx = AgentContext()
        result = await executor._handle_start(node, ctx)
        assert result["action"] == "next"

    async def test_handle_llm(self, executor):
        node = NodeConfig(type="llm", model="test", temperature=0.5)
        ctx = AgentContext()
        result = await executor._handle_llm(node, ctx, model="test", tools_schema=[])
        assert result["action"] == "llm_response"
        assert "Mock response" in result["output"]

    async def test_handle_tool(self, executor):
        node = NodeConfig(type="tool", tool_name="mock_tool", tool_params={"input": "hello"})
        ctx = AgentContext()
        result = await executor._handle_tool(node, ctx)
        assert result["action"] == "next"
        assert "Result: hello" in result["output"]

    async def test_handle_condition(self, executor):
        node = NodeConfig(type="condition", condition_expr="true")
        ctx = AgentContext()
        result = await executor._handle_condition(node, ctx)
        assert result["action"] == "branch"
        assert result["output"] == "true"

    async def test_handle_loop(self, executor):
        node = NodeConfig(type="loop", max_iterations=10)
        ctx = AgentContext()
        ctx.iteration_count = 3
        result = await executor._handle_loop(node, ctx)
        assert result["action"] == "next"

    async def test_handle_end(self, executor):
        node = NodeConfig(type="end")
        ctx = AgentContext()
        result = await executor._handle_end(node, ctx)
        assert result["action"] == "stop"