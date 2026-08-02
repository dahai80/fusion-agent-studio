"""Tests for Phase 1 capabilities: streaming, safety, checkpoint, embeddings, effort, MCP."""

from __future__ import annotations


import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry
from tools.mcp_tool import MCPTool, MCPRegistry


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo input"
    parameters = {"text": {"type": "string", "description": "text to echo"}}

    async def execute(self, text: str = "", **kwargs) -> str:
        return text


class DangerousTool(BaseTool):
    name = "dangerous"
    description = "A dangerous tool"
    parameters = {"cmd": {"type": "string", "description": "command"}}

    async def execute(self, cmd: str = "", **kwargs) -> str:
        return f"executed: {cmd}"


class MockStreamClient:
    def __init__(self, chunks=None):
        from server.fusion_mlx_client import StreamChunk

        self._StreamChunk = StreamChunk
        self._chunks = chunks or [
            StreamChunk(delta_content="Hello", delta_tool_calls=[], finish_reason=None),
            StreamChunk(
                delta_content=" world", delta_tool_calls=[], finish_reason=None
            ),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop"),
        ]

    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in self._chunks:
            yield chunk

    async def chat(self, model, messages, tools=None, **kwargs):
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="Hello world",
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            finish_reason="stop",
        )

    async def embeddings(self, model, input, **kwargs):
        if isinstance(input, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in input]


class MockGateway(LLMGateway):
    def __init__(self, client):
        super().__init__()
        self.set_default_client(client)
        from agent_runtime.llm_gateway import ModelConfig

        self.register_model(
            ModelConfig(name="test-model", provider="local", context_length=4096)
        )
        self.register_model(
            ModelConfig(name="test", provider="local", context_length=4096)
        )


class MockStore:
    def __init__(self):
        self.checkpoints = []

    def save_checkpoint(self, graph_id, session_id, node_id, state):
        self.checkpoints.append(
            {
                "graph_id": graph_id,
                "session_id": session_id,
                "node_id": node_id,
                "state": state,
            }
        )

    def load_latest_checkpoint(self, graph_id, session_id):
        if not self.checkpoints:
            return None
        return self.checkpoints[-1]


class TestStreamingExecution:
    @pytest.fixture
    def registry(self):
        r = ToolRegistry()
        r.register(EchoTool())
        return r

    @pytest.fixture
    def stream_client(self):
        return MockStreamClient()

    @pytest.fixture
    def simple_graph(self):
        g = AgentGraph(name="stream_test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test-model"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "llm1")
        g.add_edge("llm1", "end")
        return g

    @pytest.mark.asyncio
    async def test_execute_graph_stream_yields_token_events(
        self, stream_client, registry, simple_graph
    ):
        gateway = MockGateway(stream_client)
        runtime = AgentRuntime(llm_gateway=gateway, tool_registry=registry)
        events = []
        async for event in runtime.execute_graph_stream(simple_graph, "hello"):
            events.append(event)

        token_events = [e for e in events if e.type == AgentEventType.TOKEN]
        assert len(token_events) >= 2
        assert token_events[0].content == "Hello"
        assert token_events[1].content == " world"

    @pytest.mark.asyncio
    async def test_execute_graph_non_stream_no_token_events(
        self, stream_client, registry, simple_graph
    ):
        gateway = MockGateway(stream_client)
        runtime = AgentRuntime(llm_gateway=gateway, tool_registry=registry)
        events = []
        async for event in runtime.execute_graph(simple_graph, "hello"):
            events.append(event)

        token_events = [e for e in events if e.type == AgentEventType.TOKEN]
        assert len(token_events) == 0

        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert len(think_events) >= 1


class TestSafetyGateway:
    @pytest.fixture
    def registry(self):
        r = ToolRegistry()
        r.register(EchoTool())
        r.register(DangerousTool())
        return r

    @pytest.fixture
    def safety_gateway(self):
        from agent_runtime.safety import SafetyGateway, SafetyPolicy, SafetyLevel

        gw = SafetyGateway()
        gw.add_policy(
            SafetyPolicy(
                category="llm_call",
                default_level=SafetyLevel.L1,
                requires_diff=False,
                description="LLM calls auto-approved",
            )
        )
        gw.add_policy(
            SafetyPolicy(
                category="tool_call",
                default_level=SafetyLevel.L1,
                requires_diff=False,
                description="Tool calls auto-approved",
            )
        )
        return gw

    @pytest.mark.asyncio
    async def test_safety_events_in_llm_node(self, registry, safety_gateway):
        client = MockStreamClient()
        gateway = MockGateway(client)
        runtime = AgentRuntime(
            llm_gateway=gateway,
            tool_registry=registry,
            safety_gateway=safety_gateway,
        )
        g = AgentGraph(name="safety_test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "llm1")
        g.add_edge("llm1", "end")

        events = []
        async for event in runtime.execute_graph(g, "hello"):
            events.append(event)

        safety_events = [e for e in events if e.type == AgentEventType.SAFETY_APPROVAL]
        assert len(safety_events) >= 1
        assert safety_events[0].metadata.get("action") == "approved"


class TestAutoCheckpoint:
    @pytest.fixture
    def registry(self):
        r = ToolRegistry()
        r.register(EchoTool())
        return r

    @pytest.mark.asyncio
    async def test_auto_checkpoint_saves_on_llm_node(self, registry):
        store = MockStore()
        client = MockStreamClient()
        gateway = MockGateway(client)
        runtime = AgentRuntime(
            llm_gateway=gateway,
            tool_registry=registry,
            store=store,
            auto_checkpoint=True,
        )
        g = AgentGraph(name="ckpt_test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "llm1")
        g.add_edge("llm1", "end")

        events = []
        async for event in runtime.execute_graph(g, "hello"):
            events.append(event)

        assert len(store.checkpoints) >= 1
        assert store.checkpoints[0]["graph_id"] == "ckpt_test"
        assert "messages" in store.checkpoints[0]["state"]

    @pytest.mark.asyncio
    async def test_no_checkpoint_without_auto_flag(self, registry):
        store = MockStore()
        client = MockStreamClient()
        gateway = MockGateway(client)
        runtime = AgentRuntime(
            llm_gateway=gateway,
            tool_registry=registry,
            store=store,
            auto_checkpoint=False,
        )
        g = AgentGraph(name="no_ckpt_test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "llm1")
        g.add_edge("llm1", "end")

        events = []
        async for event in runtime.execute_graph(g, "hello"):
            events.append(event)

        assert len(store.checkpoints) == 0


class TestEffortLevel:
    def test_node_config_effort_field(self):
        node = NodeConfig(type="llm", label="effort_test", model="test", effort="high")
        assert node.effort == "high"
        d = node.to_dict()
        assert d.get("effort") == "high"

    def test_node_config_empty_effort_omitted(self):
        node = NodeConfig(type="llm", label="no_effort", model="test")
        d = node.to_dict()
        assert "effort" not in d


class TestMCPTool:
    def test_mcp_tool_schema(self):
        tool = MCPTool(
            server_url="http://localhost:3000",
            tool_name="read_file",
            tool_description="Read a file",
            tool_parameters={
                "path": {
                    "type": "string",
                    "description": "file path",
                    "required": True,
                },
            },
        )
        assert tool.name == "read_file"
        schema = tool.openai_schema()
        assert schema["function"]["name"] == "read_file"
        assert "path" in schema["function"]["parameters"]["properties"]
        assert "path" in schema["function"]["parameters"]["required"]

    def test_mcp_registry_init(self):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        assert mcp.list_servers() == {}

    def test_mcp_registry_unregister_nonexistent(self):
        registry = ToolRegistry()
        mcp = MCPRegistry(registry)
        mcp.unregister_server("http://localhost:9999")


class TestEventTypes:
    def test_new_event_types_exist(self):
        assert AgentEventType.TOKEN.value == "token"
        assert AgentEventType.THINKING_TOKEN.value == "thinking_token"
        assert AgentEventType.TOOL_CALL_START.value == "tool_call_start"
        assert AgentEventType.TOOL_CALL_END.value == "tool_call_end"
        assert AgentEventType.SAFETY_APPROVAL.value == "safety_approval"
        assert AgentEventType.CHECKPOINT.value == "checkpoint"
