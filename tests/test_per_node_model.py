from __future__ import annotations

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


class ModelRecordingClient:
    def __init__(self):
        self.models_used: list[str] = []
        self.responses: list[str] = []

    def add_response(self, content: str):
        self.responses.append(content)

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.models_used.append(model)
        from server.fusion_mlx_client import LLMResponse

        content = self.responses.pop(0) if self.responses else "ok"
        return LLMResponse(content=content, tool_calls=[])

    async def chat_stream(self, **kwargs):
        return None

    def set_compactor(self, compactor):
        pass


async def test_per_node_model_override_routes_each_node():
    # Issue #176: two LLM nodes with distinct models must each route to its
    # own model, not the graph-level first-llm fallback.
    client = ModelRecordingClient()
    client.add_response("first node done")
    client.add_response("second node done")
    registry = ToolRegistry()

    graph = AgentGraph(name="Multi-Model")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "llm_a",
        NodeConfig(type="llm", label="LLM A", model="model-alpha"),
    )
    graph.add_node(
        "llm_b",
        NodeConfig(type="llm", label="LLM B", model="model-beta"),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm_a")
    graph.add_edge("llm_a", "llm_b")
    graph.add_edge("llm_b", "end")

    runtime = AgentRuntime(client, registry)
    events = []
    async for event in runtime.execute_graph(graph, "hi"):
        events.append(event)

    assert client.models_used == ["model-alpha", "model-beta"], (
        f"expected per-node models, got {client.models_used}"
    )
    assert any(e.type == AgentEventType.END for e in events)


async def test_per_node_model_falls_back_to_graph_model_when_unset():
    # Node without explicit model must inherit the graph-level model.
    client = ModelRecordingClient()
    client.add_response("done")
    registry = ToolRegistry()

    graph = AgentGraph(name="Fallback-Model")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "llm_default",
        NodeConfig(type="llm", label="LLM", model="graph-model"),
    )
    graph.add_node(
        "llm_empty",
        NodeConfig(type="llm", label="LLM Empty", model=""),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm_default")
    graph.add_edge("llm_default", "llm_empty")
    graph.add_edge("llm_empty", "end")

    runtime = AgentRuntime(client, registry)
    async for _ in runtime.execute_graph(graph, "hi"):
        pass

    assert client.models_used == ["graph-model", "graph-model"], (
        f"empty node.model should fall back, got {client.models_used}"
    )
