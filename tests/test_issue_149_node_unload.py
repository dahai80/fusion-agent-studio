"""Issue #149: optional per-node explicit model unload.

When FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE is enabled, the runtime asks
fusion-mlx to unload the served model after an LLM node fully advances
to the next node. The unload must never fire during tool-call re-entry
(same node) and must be non-fatal so a failed/already-evicted unload
never aborts the workflow.
"""

from __future__ import annotations

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _NoopTool(BaseTool):
    name = "noop_tool"
    description = "no-op tool"
    parameters = {"input": {"type": "string", "description": "in"}}

    async def execute(self, **kwargs) -> str:
        return "ok"


class _UnloadTrackingClient:
    """Mock fusion-mlx client that records unload_model calls.

    Mirrors the real FusionMLXClient surface used by the runtime: a ``chat``
    method returning LLMResponse, and an ``unload_model`` coroutine. The
    ``unload_fail`` flag simulates fusion-mlx returning an error (e.g. 404
    for an already-evicted model) to exercise the non-fatal path.
    """

    def __init__(self, *, unload_fail: bool = False):
        self.call_count = 0
        self.unloaded: list[str] = []
        self.unload_fail = unload_fail
        self.last_tools = None

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.call_count += 1
        self.last_tools = tools
        return LLMResponse(content=f"reply-{self.call_count}", tool_calls=[])

    async def unload_model(self, model_id: str):
        self.unloaded.append(model_id)
        if self.unload_fail:
            raise RuntimeError("simulated unload failure")


@pytest.fixture
def tool_registry():
    reg = ToolRegistry()
    reg.register(_NoopTool())
    return reg


def _chain_graph() -> AgentGraph:
    graph = AgentGraph(name="Chain")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm_a", NodeConfig(type="llm", label="A", model="model-a"))
    graph.add_node("llm_b", NodeConfig(type="llm", label="B", model="model-b"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm_a")
    graph.add_edge("llm_a", "llm_b")
    graph.add_edge("llm_b", "end")
    return graph


class TestNodeUnloadFlagDefaults:
    """Env-gated flag plumbing."""

    def test_flag_default_off(self, tool_registry, monkeypatch):
        monkeypatch.delenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", raising=False)
        client = _UnloadTrackingClient()
        runtime = AgentRuntime(client, tool_registry)
        assert runtime.unload_model_after_node is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_flag_truthy_values(self, tool_registry, monkeypatch, val):
        monkeypatch.setenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", val)
        runtime = AgentRuntime(_UnloadTrackingClient(), tool_registry)
        assert runtime.unload_model_after_node is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "garbage"])
    def test_flag_falsy_values(self, tool_registry, monkeypatch, val):
        monkeypatch.setenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", val)
        runtime = AgentRuntime(_UnloadTrackingClient(), tool_registry)
        assert runtime.unload_model_after_node is False


class TestNodeUnloadBehaviour:
    """Runtime calls unload after an LLM node advances, not during re-entry."""

    @pytest.mark.asyncio
    async def test_off_does_not_unload(self, tool_registry, monkeypatch):
        monkeypatch.delenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", raising=False)
        client = _UnloadTrackingClient()
        runtime = AgentRuntime(client, tool_registry)
        async for _ in runtime.execute_graph(_chain_graph(), "go"):
            pass
        assert client.unloaded == []
        assert client.call_count == 2

    @pytest.mark.asyncio
    async def test_on_unloads_each_served_model(self, tool_registry, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", "1")
        client = _UnloadTrackingClient()
        runtime = AgentRuntime(client, tool_registry)
        async for _ in runtime.execute_graph(_chain_graph(), "go"):
            pass
        assert client.unloaded == ["model-a", "model-b"]
        assert client.call_count == 2

    @pytest.mark.asyncio
    async def test_not_fired_during_tool_reentry(self, tool_registry, monkeypatch):
        # With loop_mode="agent", a tool-call round re-enters the SAME node
        # (the tool result is fed back to the LLM). Unload must only fire
        # once per node — on advance — NOT after each tool re-entry round.
        monkeypatch.setenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", "1")

        class _ToolReentryClient(_UnloadTrackingClient):
            def __init__(self):
                super().__init__()
                self._round = 0

            async def chat(self, model, messages, tools=None, **kwargs):
                self.call_count += 1
                self._round += 1
                self.last_tools = tools
                # Round 1 (node A): tool call -> re-enter same node.
                # Round 2 (node A): final -> node advances.
                # Round 3 (node B): final -> node advances.
                if self._round == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "noop_tool",
                                    "arguments": '{"input": "x"}',
                                },
                            }
                        ],
                    )
                return LLMResponse(content="done", tool_calls=[])

        graph = AgentGraph(name="AgentLoopChain")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "llm_a",
            NodeConfig(
                type="llm",
                label="A",
                model="model-a",
                loop_mode="agent",
                max_loop_iterations=5,
            ),
        )
        graph.add_node("llm_b", NodeConfig(type="llm", label="B", model="model-b"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "llm_a")
        graph.add_edge("llm_a", "llm_b")
        graph.add_edge("llm_b", "end")

        client = _ToolReentryClient()
        runtime = AgentRuntime(client, tool_registry)
        async for _ in runtime.execute_graph(graph, "go"):
            pass
        # 3 LLM calls total: 2 on node A (tool round + final round via the
        # agent loop re-entry) + 1 on node B. But only ONE unload per node
        # — model-a and model-b — each fired when the node advances, never
        # during the tool re-entry round on node A.
        assert client.call_count == 3
        assert client.unloaded == ["model-a", "model-b"]

    @pytest.mark.asyncio
    async def test_unload_failure_is_non_fatal(self, tool_registry, monkeypatch):
        monkeypatch.setenv("FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", "1")
        client = _UnloadTrackingClient(unload_fail=True)
        runtime = AgentRuntime(client, tool_registry)
        events = []
        async for event in runtime.execute_graph(_chain_graph(), "go"):
            events.append(event)
        # Workflow still completes despite unload raising.
        assert client.unloaded == ["model-a", "model-b"]
        assert client.call_count == 2
        assert any(e.type == AgentEventType.END for e in events)
        assert not any(e.type == AgentEventType.ERROR for e in events)


class TestGatewayUnloadProxy:
    """LLMGateway.unload_model proxies to the default client, non-fatal."""

    @pytest.mark.asyncio
    async def test_no_default_client_returns_false(self):
        gw = LLMGateway()
        assert await gw.unload_model("model-a") is False

    @pytest.mark.asyncio
    async def test_empty_model_returns_false(self, tool_registry):
        client = _UnloadTrackingClient()
        gw = LLMGateway()
        gw.set_default_client(client)
        assert await gw.unload_model("") is False
        assert client.unloaded == []

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        client = _UnloadTrackingClient()
        gw = LLMGateway()
        gw.set_default_client(client)
        assert await gw.unload_model("model-a") is True
        assert client.unloaded == ["model-a"]

    @pytest.mark.asyncio
    async def test_client_without_unload_method_returns_false(self):
        class _BareClient:
            async def chat(self, *a, **k):
                return LLMResponse(content="x", tool_calls=[])

        gw = LLMGateway()
        gw.set_default_client(_BareClient())
        assert await gw.unload_model("model-a") is False

    @pytest.mark.asyncio
    async def test_client_error_returns_false(self):
        client = _UnloadTrackingClient(unload_fail=True)
        gw = LLMGateway()
        gw.set_default_client(client)
        assert await gw.unload_model("model-a") is False
        assert client.unloaded == ["model-a"]
