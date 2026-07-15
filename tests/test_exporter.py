"""Tests for graph exporter."""
from __future__ import annotations

import pytest

from agent_runtime.exporter import GraphExporter
from agent_runtime.graph import AgentGraph, NodeConfig


@pytest.fixture
def sample_graph():
    graph = AgentGraph(name="Test Agent", description="A simple test agent")
    graph.add_node("start", NodeConfig(type="start", label="Start", x=100, y=200))
    graph.add_node("llm", NodeConfig(
        type="llm", label="Think", model="qwen3.5-9b",
        system_prompt="You are helpful.", temperature=0.5, x=300, y=200,
    ))
    graph.add_node("end", NodeConfig(type="end", label="End", x=500, y=200))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    return graph


class TestGraphExporter:
    def test_to_python_contains_graph_name(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert "Test Agent" in output
        assert "import asyncio" in output
        assert "import httpx" in output

    def test_to_python_contains_nodes(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert "'start'" in output
        assert "'llm'" in output
        assert "'end'" in output

    def test_to_python_contains_edges(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert "'source': 'start'" in output
        assert "'target': 'llm'" in output

    def test_to_python_without_runtime(self, sample_graph):
        output = GraphExporter.to_python(sample_graph, include_runtime=False)
        assert "async def call_llm" not in output

    def test_to_python_with_runtime(self, sample_graph):
        output = GraphExporter.to_python(sample_graph, include_runtime=True)
        assert "async def call_llm" in output
        assert "async def execute_agent" in output

    def test_to_python_contains_main(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert 'if __name__ == "__main__":' in output
        assert "async def main():" in output

    def test_to_python_contains_system_prompt(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert "You are helpful." in output

    def test_to_python_contains_model_name(self, sample_graph):
        output = GraphExporter.to_python(sample_graph)
        assert "qwen3.5-9b" in output

    def test_to_json(self, sample_graph):
        output = GraphExporter.to_json(sample_graph)
        assert '"name": "Test Agent"' in output
        assert '"start"' in output

    def test_to_yaml(self, sample_graph):
        output = GraphExporter.to_yaml(sample_graph)
        assert "Test Agent" in output
        assert "start:" in output
        assert "llm:" in output

    def test_to_yaml_contains_edges(self, sample_graph):
        output = GraphExporter.to_yaml(sample_graph)
        assert "source: start" in output
        assert "target: llm" in output

    def test_export_empty_graph(self):
        graph = AgentGraph(name="Empty")
        output = GraphExporter.to_python(graph)
        assert "Empty" in output
        assert "nodes = {" in output

    def test_export_graph_with_tool_node(self):
        graph = AgentGraph(name="Tool Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("tool", NodeConfig(
            type="tool", label="Read", tool_name="file_read",
            tool_params={"path": "/tmp/test.txt"},
        ))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "tool")
        graph.add_edge("tool", "end")

        output = GraphExporter.to_python(graph)
        assert "file_read" in output
        assert "/tmp/test.txt" in output

    def test_export_graph_with_condition(self):
        graph = AgentGraph(name="Condition Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("cond", NodeConfig(
            type="condition", label="Check", condition_expr="has_tool_calls",
        ))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "cond")
        graph.add_edge("cond", "end", "true")
        graph.add_edge("cond", "end", "false")

        output = GraphExporter.to_yaml(graph)
        assert "has_tool_calls" in output

    def test_runtime_code_executable(self, sample_graph):
        """Verify the generated Python code is syntactically valid."""
        output = GraphExporter.to_python(sample_graph)
        # Check it's valid Python syntax
        compile(output, "<test>", "exec")