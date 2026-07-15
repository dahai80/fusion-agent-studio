"""Tests for agent graph data model."""
from __future__ import annotations

import json

import pytest

from agent_runtime.graph import AgentGraph, Edge, NodeConfig


class TestNodeConfig:
    def test_create_start_node(self):
        node = NodeConfig(type="start", label="Start", x=100, y=200)
        assert node.type == "start"
        assert node.label == "Start"
        assert node.x == 100.0
        assert node.y == 200.0

    def test_create_llm_node(self):
        node = NodeConfig(
            type="llm", label="LLM", model="qwen3.5-9b",
            system_prompt="You are helpful", temperature=0.5,
        )
        assert node.model == "qwen3.5-9b"
        assert node.temperature == 0.5

    def test_to_dict(self):
        node = NodeConfig(type="llm", label="LLM", model="test", temperature=0.3)
        d = node.to_dict()
        assert d["type"] == "llm"
        assert d["model"] == "test"
        assert d["temperature"] == 0.3

    def test_to_dict_skips_empty(self):
        node = NodeConfig(type="end", label="End")
        d = node.to_dict()
        assert "model" not in d
        assert "tool_name" not in d

    def test_from_dict(self):
        node = NodeConfig.from_dict({"type": "llm", "label": "Test", "model": "m1"})
        assert node.type == "llm"
        assert node.model == "m1"

    def test_tool_node_params(self):
        node = NodeConfig(
            type="tool", label="ReadFile", tool_name="file_read",
            tool_params={"path": "/tmp/test.txt"},
        )
        assert node.tool_name == "file_read"
        assert node.tool_params["path"] == "/tmp/test.txt"

    def test_condition_node(self):
        node = NodeConfig(
            type="condition", label="Check", condition_expr="has_tool_calls",
        )
        assert node.condition_expr == "has_tool_calls"

    def test_loop_node(self):
        node = NodeConfig(type="loop", label="Loop", max_iterations=5)
        assert node.max_iterations == 5


class TestEdge:
    def test_create_edge(self):
        edge = Edge(source_id="a", target_id="b", label="yes")
        assert edge.source_id == "a"
        assert edge.target_id == "b"
        assert edge.label == "yes"

    def test_to_dict(self):
        edge = Edge(source_id="a", target_id="b")
        d = edge.to_dict()
        assert d["source_id"] == "a"
        assert d["target_id"] == "b"

    def test_from_dict(self):
        edge = Edge.from_dict({"source_id": "a", "target_id": "b", "label": "no"})
        assert edge.label == "no"


class TestAgentGraph:
    def test_create_empty_graph(self):
        graph = AgentGraph()
        assert graph.id
        assert len(graph.id) == 16
        assert graph.nodes == {}
        assert graph.edges == []

    def test_create_with_name(self):
        graph = AgentGraph(name="Test Agent", description="A test")
        assert graph.name == "Test Agent"
        assert graph.description == "A test"

    def test_add_node(self):
        graph = AgentGraph()
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        assert "start" in graph.nodes
        assert graph.start_node_id == "start"

    def test_add_edge(self):
        graph = AgentGraph()
        graph.add_edge("a", "b", "yes")
        assert len(graph.edges) == 1
        assert graph.edges[0].label == "yes"

    def test_get_node(self):
        graph = AgentGraph()
        node = NodeConfig(type="llm", label="Test")
        graph.add_node("n1", node)
        assert graph.get_node("n1") is node
        assert graph.get_node("nonexistent") is None

    def test_get_outgoing_edges(self):
        graph = AgentGraph()
        graph.add_edge("a", "b")
        graph.add_edge("a", "c")
        graph.add_edge("d", "e")
        edges = graph.get_outgoing_edges("a")
        assert len(edges) == 2

    def test_get_next_node_single_edge(self):
        graph = AgentGraph()
        graph.add_edge("a", "b")
        assert graph.get_next_node("a") == "b"

    def test_get_next_node_no_edges(self):
        graph = AgentGraph()
        assert graph.get_next_node("a") is None

    def test_get_next_node_with_condition_label(self):
        graph = AgentGraph()
        graph.add_edge("a", "b", "true")
        graph.add_edge("a", "c", "false")
        assert graph.get_next_node("a", "true") == "b"
        assert graph.get_next_node("a", "false") == "c"

    def test_get_next_node_condition_fallback(self):
        graph = AgentGraph()
        graph.add_edge("a", "b", "true")
        graph.add_edge("a", "c")  # no label
        assert graph.get_next_node("a", "unknown") == "c"

    def test_find_llm_model(self):
        graph = AgentGraph()
        graph.add_node("llm1", NodeConfig(type="llm", model="qwen3.5-9b"))
        graph.add_node("llm2", NodeConfig(type="llm", model="deepseek"))
        assert graph.find_llm_model() == "qwen3.5-9b"

    def test_find_llm_model_empty(self):
        graph = AgentGraph()
        graph.add_node("start", NodeConfig(type="start"))
        assert graph.find_llm_model() == ""

    def test_validate_no_nodes(self):
        graph = AgentGraph()
        errors = graph.validate()
        assert "Graph has no nodes" in errors

    def test_validate_no_start(self):
        graph = AgentGraph(name="test")
        graph.add_node("llm1", NodeConfig(type="llm"))
        errors = graph.validate()
        assert "Graph has no start node" in errors

    def test_validate_invalid_start(self):
        graph = AgentGraph(name="test")
        graph.add_node("start", NodeConfig(type="start"))
        graph.start_node_id = "nonexistent"
        errors = graph.validate()
        assert any("Start node" in e for e in errors)

    def test_validate_invalid_edge(self):
        graph = AgentGraph()
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.start_node_id = "start"
        graph.add_edge("start", "nonexistent")
        errors = graph.validate()
        assert any("target 'nonexistent'" in e for e in errors)

    def test_validate_unreachable_node(self):
        graph = AgentGraph()
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_node("orphan", NodeConfig(type="llm"))
        graph.start_node_id = "start"
        graph.add_edge("start", "end")
        errors = graph.validate()
        assert any("unreachable" in e for e in errors)

    def test_validate_valid_graph(self):
        graph = AgentGraph()
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("llm", NodeConfig(type="llm", model="test"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "end")
        assert graph.validate() == []

    def test_to_dict_roundtrip(self):
        graph = AgentGraph(name="Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("llm", NodeConfig(type="llm", model="test"))
        graph.add_edge("start", "llm")
        d = graph.to_dict()
        assert d["name"] == "Test"
        assert "start" in d["nodes"]
        assert len(d["edges"]) == 1

    def test_from_dict(self):
        data = {
            "id": "abc123",
            "name": "Test",
            "nodes": {
                "s": {"type": "start", "label": "Start"},
                "e": {"type": "end", "label": "End"},
            },
            "edges": [{"source_id": "s", "target_id": "e"}],
            "start_node_id": "s",
        }
        graph = AgentGraph.from_dict(data)
        assert graph.id == "abc123"
        assert graph.name == "Test"
        assert len(graph.nodes) == 2
        assert len(graph.edges) == 1

    def test_to_json(self):
        graph = AgentGraph(name="Test")
        graph.add_node("s", NodeConfig(type="start"))
        graph.add_node("e", NodeConfig(type="end"))
        graph.add_edge("s", "e")
        json_str = graph.to_json()
        parsed = json.loads(json_str)
        assert parsed["name"] == "Test"

    def test_from_json(self):
        json_str = '{"name": "Test", "nodes": {"s": {"type": "start"}}, "edges": [], "start_node_id": "s"}'
        graph = AgentGraph.from_json(json_str)
        assert graph.name == "Test"

    def test_create_default(self):
        graph = AgentGraph.create_default("Default Agent")
        assert graph.name == "Default Agent"
        assert len(graph.nodes) == 3
        assert len(graph.edges) == 2
        assert graph.start_node_id == "start"

    def test_reachable_nodes(self):
        graph = AgentGraph()
        graph.add_node("s", NodeConfig(type="start"))
        graph.add_node("a", NodeConfig(type="llm"))
        graph.add_node("b", NodeConfig(type="llm"))
        graph.add_node("c", NodeConfig(type="end"))
        graph.add_edge("s", "a")
        graph.add_edge("a", "b")
        graph.add_edge("b", "c")
        reachable = graph._reachable_nodes()
        assert reachable == {"s", "a", "b", "c"}

    def test_add_node_sets_start(self):
        graph = AgentGraph()
        graph.add_node("s", NodeConfig(type="start"))
        assert graph.start_node_id == "s"

    def test_add_node_does_not_override_start(self):
        graph = AgentGraph()
        graph.add_node("s1", NodeConfig(type="start"))
        graph.add_node("s2", NodeConfig(type="start"))
        assert graph.start_node_id == "s1"