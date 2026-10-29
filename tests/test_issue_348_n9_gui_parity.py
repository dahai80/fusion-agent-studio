"""#348 N9: GUI parity — permission cards, thinking channel, run artifact triple."""

from __future__ import annotations

import json

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.permission_cards import (
    CARD_CODE,
    CARD_FOLDER,
    CARD_NETWORK,
    CARD_REMOTE,
    CARD_TYPES,
    build_card,
    card_type_for_category,
)
from agent_runtime.run_artifacts import (
    SCHEMA,
    RunArtifactStore,
    record_run_artifact,
    snapshot_environment,
)
from agent_runtime.runtime import AgentRuntime
from agent_runtime.safety import SafetyGateway
from server.fusion_mlx_client import StreamChunk, _reasoning_delta
from tests._safety_mock import MockGuardClient, MockVerdict
from tools.registry import ToolRegistry


class MockThinkingClient:
    def __init__(self):
        self._StreamChunk = StreamChunk

    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in [
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason=None, delta_reasoning="why "),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason=None, delta_reasoning="this"),
            StreamChunk(delta_content="Hi", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop"),
        ]:
            yield chunk

    async def chat(self, model, messages, tools=None, **kwargs):
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="Hi",
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _make_runtime():
    gateway = LLMGateway()
    gateway.set_default_client(MockThinkingClient())
    gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    return AgentRuntime(llm_gateway=gateway, tool_registry=ToolRegistry())


def _make_graph(thinking: bool = True):
    g = AgentGraph(name="n9_thinking", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "llm1",
        NodeConfig(type="llm", label="llm1", model="test-model", tool_params={"thinking": thinking}),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "end")
    return g


async def _collect(runtime, graph, **kwargs):
    events = []
    async for event in runtime.execute_graph_stream(graph, "hi", **kwargs):
        events.append(event)
    return events


class TestPermissionCards:
    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            ("file_read", CARD_FOLDER),
            ("file_write", CARD_FOLDER),
            ("doc_retrieval", CARD_FOLDER),
            ("knowledge_search", CARD_FOLDER),
            ("code_analysis", CARD_CODE),
            ("code_edit", CARD_CODE),
            ("shell_exec", CARD_CODE),
            ("git_push", CARD_CODE),
            ("database_write", CARD_CODE),
            ("network_access", CARD_NETWORK),
            ("tool_call", CARD_REMOTE),
            ("llm_call", CARD_REMOTE),
        ],
    )
    def test_category_mapping(self, category, expected):
        assert card_type_for_category(category) == expected
        assert expected in CARD_TYPES

    def test_unknown_category_defaults_to_code(self):
        assert card_type_for_category("unknown_category") == CARD_CODE
        assert card_type_for_category("") == CARD_CODE

    def test_build_card_with_diff(self):
        card = build_card(
            {
                "action_id": "a1",
                "category": "file_write",
                "content": "write /tmp/x",
                "level": "L2",
                "diff": "--- original\n+++ proposed\n",
                "original": "a",
                "proposed": "b",
            }
        )
        assert card["card_type"] == CARD_FOLDER
        assert card["requires_diff"] is True
        assert card["diff"].startswith("---")
        assert card["original"] == "a"
        assert card["proposed"] == "b"

    def test_build_card_without_diff(self):
        card = build_card({"action_id": "a2", "category": "network_access", "content": "GET"})
        assert card["card_type"] == CARD_NETWORK
        assert card["requires_diff"] is False
        assert "diff" not in card
        assert card["status"] == "pending"

    def test_pending_actions_carry_card(self):
        gw = SafetyGateway(
            guard_client=MockGuardClient(verdict=MockVerdict(action="allow", risk_level="l1"))
        )
        request = gw.generate_diff_preview("old\n", "new\n", category="file_write")
        actions = gw.get_pending_actions()
        assert len(actions) == 1
        assert actions[0]["action_id"] == request.action_id
        assert actions[0]["card_type"] == CARD_FOLDER
        assert actions[0]["card"]["card_type"] == CARD_FOLDER
        assert actions[0]["card"]["requires_diff"] is True


class TestReasoningDelta:
    @pytest.mark.parametrize("key", ["reasoning_content", "reasoning", "thinking"])
    def test_known_keys(self, key):
        assert _reasoning_delta({key: "plan"}) == "plan"

    def test_missing_or_non_string(self):
        assert _reasoning_delta({}) == ""
        assert _reasoning_delta({"reasoning_content": None}) == ""
        assert _reasoning_delta({"reasoning_content": 42}) == ""

    @pytest.mark.asyncio
    async def test_gateway_forwards_delta_reasoning(self):
        gateway = LLMGateway()
        gateway.set_default_client(MockThinkingClient())
        gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
        chunks = [
            c
            async for c in gateway.chat_stream(
                messages=[{"role": "user", "content": "hi"}], model="test-model"
            )
        ]
        reasoning = "".join(c.get("delta_reasoning", "") for c in chunks)
        assert reasoning == "why this"


class TestThinkingEvents:
    @pytest.mark.asyncio
    async def test_thinking_token_emitted_by_default(self):
        events = await _collect(_make_runtime(), _make_graph())
        thinking = [e for e in events if e.type == AgentEventType.THINKING_TOKEN]
        assert "".join(e.content for e in thinking) == "why this"
        assert all(e.metadata.get("channel") == "thinking" for e in thinking)
        assert any(e.type == AgentEventType.TOKEN for e in events)

    @pytest.mark.asyncio
    async def test_show_thinking_false_suppresses(self):
        events = await _collect(_make_runtime(), _make_graph(), show_thinking=False)
        assert not [e for e in events if e.type == AgentEventType.THINKING_TOKEN]
        assert [e for e in events if e.type == AgentEventType.TOKEN]

    @pytest.mark.asyncio
    async def test_node_opt_out_suppresses(self):
        events = await _collect(_make_runtime(), _make_graph(thinking=False))
        assert not [e for e in events if e.type == AgentEventType.THINKING_TOKEN]


class TestRunArtifacts:
    def test_versioning_and_lookup(self, tmp_path):
        store = RunArtifactStore(root=tmp_path / "artifacts")
        g = _make_graph()
        first = store.create(g, messages=[{"role": "user", "content": "one"}], run_id="r1")
        second = store.create(g, messages=[{"role": "user", "content": "two"}], run_id="r2")
        assert first["schema"] == SCHEMA
        assert first["version"] == 1
        assert second["version"] == 2
        assert first["graph"]["name"] == "n9_thinking"
        assert first["conversation"]["count"] == 1

        fetched = store.get(first["artifact_id"])
        assert fetched is not None
        assert fetched["run_id"] == "r1"
        assert store.get("run-missing") is None

        versions = store.versions("n9_thinking")
        assert [v["version"] for v in versions] == [1, 2]
        assert len(store.list()) == 2

    def test_list_limit(self, tmp_path):
        store = RunArtifactStore(root=tmp_path / "artifacts")
        for i in range(3):
            store.create(_make_graph(), name=f"g{i}")
        assert len(store.list(limit=2)) == 2
        assert len(store.list(limit=0)) == 3

    def test_environment_masks_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_CLIENT_KEY", "super-secret")
        snap = snapshot_environment(extra={"note": "x"})
        assert snap["env"]["FUSION_CLIENT_KEY"] == "set"
        assert "super-secret" not in json.dumps(snap)
        assert snap["note"] == "x"
        assert snap["python_version"]

    def test_record_run_artifact_swallows_errors(self):
        class Broken:
            def create(self, **kwargs):
                raise RuntimeError("disk full")

        assert record_run_artifact(_make_graph(), store=Broken()) is None


class TestRunArtifactRpc:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import agent_runtime.run_artifacts as run_artifacts

        art_store = RunArtifactStore(root=tmp_path / "artifacts")
        monkeypatch.setattr(run_artifacts, "_STORE", art_store)
        rec = art_store.create(
            _make_graph(),
            messages=[{"role": "user", "content": "hi"}],
            run_id="run-1",
            metadata={"source": "test"},
        )
        return art_store, rec

    @pytest.mark.asyncio
    async def test_list_returns_summaries(self, store):
        from agent_runtime.dispatchers.infra import InfraDispatcher

        _, rec = store
        result = await InfraDispatcher(None)._handle_run_artifact_list({})
        assert result["total"] == 1
        summary = result["artifacts"][0]
        assert summary["artifact_id"] == rec["artifact_id"]
        assert summary["message_count"] == 1
        assert "graph" not in summary
        assert "conversation" not in summary

    @pytest.mark.asyncio
    async def test_get_returns_full_record(self, store):
        from agent_runtime.dispatchers.infra import InfraDispatcher

        _, rec = store
        result = await InfraDispatcher(None)._handle_run_artifact_get(
            {"artifact_id": rec["artifact_id"]}
        )
        assert result["run_id"] == "run-1"
        assert result["graph"]["name"] == "n9_thinking"
        assert result["environment"]["gateway_base"] is not None

    @pytest.mark.asyncio
    async def test_get_missing_raises(self, store):
        from agent_runtime.dispatchers.infra import InfraDispatcher

        with pytest.raises(ValueError):
            await InfraDispatcher(None)._handle_run_artifact_get({})

    @pytest.mark.asyncio
    async def test_versions_by_name(self, store):
        from agent_runtime.dispatchers.infra import InfraDispatcher

        art_store, _ = store
        art_store.create(_make_graph(), run_id="run-2")
        result = await InfraDispatcher(None)._handle_run_artifact_versions(
            {"name": "n9_thinking"}
        )
        assert result["total"] == 2
        assert [v["version"] for v in result["versions"]] == [1, 2]

    def test_allowlist_contains_methods(self):
        from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

        assert {
            "run_artifact.list",
            "run_artifact.get",
            "run_artifact.versions",
        } <= WEB_RPC_ALLOWLIST
