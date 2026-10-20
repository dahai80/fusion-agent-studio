"""Issues #393-#398 (audit-1020 langflow parity) — server surface tests.

#393 single-node test run (only_node SSE), #395 graph.update validation
(soft default + strict opt-in), #398 NodeConfig.parent group persistence,
tool.list shape for the canvas inspector, workflow RPC integration for the
orchestration GUI, plus source assertions on the canvas/workflow pages.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime import api_server
from agent_runtime.api_server import app
from agent_runtime.daemon_server import DaemonServer
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import StreamChunk
from tools.base import BaseTool
from tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_HOME_DIR", str(tmp_path))
    d = DaemonServer.__new__(DaemonServer)
    d.store = AgentStore(db_path=str(tmp_path / "store.db"))
    d._cached_tool_registry = None
    return d


def _gid(d):
    return asyncio.run(d._handle_graph_create({
        "name": "T",
        "graph_data": {
            "nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"}},
            "edges": [{"source_id": "n1", "target_id": "n2"}],
        },
    }))["graph_id"]


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo input"
    parameters = {"text": {"type": "string", "description": "text"}}

    async def execute(self, text: str = "", **kwargs) -> str:
        return text


class MockStreamClient:
    def __init__(self):
        self._StreamChunk = StreamChunk

    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in [
            StreamChunk(delta_content="Hel", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="lo", delta_tool_calls=[], finish_reason="stop"),
        ]:
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


def _make_runtime():
    client = MockStreamClient()
    gateway = LLMGateway()
    gateway.set_default_client(client)
    gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRuntime(llm_gateway=gateway, tool_registry=registry)


def _three_node_graph():
    g = AgentGraph(name="only_node_test", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("a", NodeConfig(type="llm", label="a", model="test-model"))
    g.add_node("b", NodeConfig(type="llm", label="b", model="test-model"))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "a")
    g.add_edge("a", "b")
    g.add_edge("b", "end")
    return g


@pytest.fixture
def patched_api(tmp_path, monkeypatch):
    store = AgentStore(db_path=tmp_path / "store.db")
    graph = _three_node_graph()
    store.save_graph(graph)
    monkeypatch.setattr(api_server, "_store", store)
    monkeypatch.setattr(api_server, "_runtime", _make_runtime())
    monkeypatch.setattr(api_server, "_daemon", None)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    return {"store": store, "graph": graph}


def _sse_frames(resp) -> list[dict]:
    frames = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[6:]))
    return frames


# ---------------------------------------------------------------------------
# #393: single-node test run — only_node SSE
# ---------------------------------------------------------------------------


class TestOnlyNodeSse:

    def test_sse_only_node_skips_upstream(self, patched_api):
        with TestClient(app) as client:
            with client.stream(
                "GET",
                f"/v1/graphs/{patched_api['graph'].id}/execute/stream"
                "?input=hi&only_node=b",
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                resp.read()
                frames = _sse_frames(resp)
        node_ids = {f.get("node_id") for f in frames if f.get("node_id")}
        assert "a" not in node_ids
        assert "b" in node_ids

    def test_sse_only_node_invalid_returns_400(self, patched_api):
        # Regression: invalid only_node raised ErrorCode.PARAM_INVALID, which
        # does not exist in the ErrorCode enum (AttributeError, not 400).
        with TestClient(app) as client:
            resp = client.get(
                f"/v1/graphs/{patched_api['graph'].id}/execute/stream"
                "?input=hi&only_node=ghost"
            )
        assert resp.status_code == 400

    def test_sse_only_node_validates_before_run(self, patched_api):
        with TestClient(app) as client:
            with client.stream(
                "GET",
                f"/v1/graphs/{patched_api['graph'].id}/execute/stream"
                "?input=hi&only_node=a",
            ) as resp:
                resp.read()
                frames = _sse_frames(resp)
        node_ids = {f.get("node_id") for f in frames if f.get("node_id")}
        assert "b" not in node_ids
        assert "a" in node_ids


# ---------------------------------------------------------------------------
# #395: graph.update validation — soft default, strict opt-in
# ---------------------------------------------------------------------------


class TestGraphUpdateValidation:

    def test_soft_validation_returns_issues_and_saves(self, daemon):
        gid = _gid(daemon)
        res = asyncio.run(daemon._handle_graph_update({
            "graph_id": gid,
            "graph_data": {
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"},
                          "orphan": {"type": "llm"}},
                "edges": [{"source_id": "n1", "target_id": "n2"}],
            },
        }))
        assert any("orphan" in i and "unreachable" in i for i in res["issues"])

    def test_strict_rejects_hard_error(self, daemon):
        gid = _gid(daemon)
        with pytest.raises(ValueError, match="strict_validate"):
            asyncio.run(daemon._handle_graph_update({
                "graph_id": gid,
                "strict_validate": True,
                "graph_data": {
                    "nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"}},
                    "edges": [{"source_id": "ghost", "target_id": "n2"}],
                },
            }))

    def test_strict_clean_graph_passes(self, daemon):
        gid = _gid(daemon)
        res = asyncio.run(daemon._handle_graph_update({
            "graph_id": gid,
            "strict_validate": True,
            "graph_data": {
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"},
                          "n3": {"type": "end"}},
                "edges": [{"source_id": "n1", "target_id": "n2"},
                          {"source_id": "n2", "target_id": "n3"}],
            },
        }))
        assert res["issues"] == []

    def test_soft_never_raises_on_structure_error(self, daemon):
        gid = _gid(daemon)
        res = asyncio.run(daemon._handle_graph_update({
            "graph_id": gid,
            "graph_data": {
                "nodes": {"n1": {"type": "llm"}},
                "edges": [{"source_id": "ghost", "target_id": "n1"}],
            },
        }))
        assert res["issues"]
        assert any("ghost" in i for i in res["issues"])


# ---------------------------------------------------------------------------
# #398: NodeConfig.parent — visual grouping persistence
# ---------------------------------------------------------------------------


class TestParentGrouping:

    def test_node_config_parent_roundtrip(self):
        cfg = NodeConfig(type="llm", parent="group_1")
        d = cfg.to_dict()
        assert d["parent"] == "group_1"
        restored = NodeConfig.from_dict(d)
        assert restored.parent == "group_1"

    def test_update_persists_parent(self, daemon):
        gid = _gid(daemon)
        res = asyncio.run(daemon._handle_graph_update({
            "graph_id": gid,
            "graph_data": {
                "nodes": {"n1": {"type": "start", "parent": "group_a"},
                          "n2": {"type": "llm"}},
                "edges": [],
            },
        }))
        assert res["nodes"]["n1"]["parent"] == "group_a"
        # to_dict drops falsy fields — absent key means no group
        assert res["nodes"]["n2"].get("parent", "") == ""

    def test_parent_survives_store_roundtrip(self, tmp_path):
        store = AgentStore(db_path=str(tmp_path / "s.db"))
        g = AgentGraph(name="grp")
        g.add_node("start", NodeConfig(type="start"))
        g.add_node("llm1", NodeConfig(type="llm", parent="g1"))
        store.save_graph(g)
        loaded = store.load_graph(g.id)
        assert loaded.nodes["llm1"].parent == "g1"

    def test_group_type_rejected_by_validate(self):
        # Group nodes are canvas-only; "group" is not a runtime node type.
        g = AgentGraph(name="bad")
        g.add_node("start", NodeConfig(type="start"))
        g.add_node("grp", NodeConfig(type="group"))
        issues = g.validate(None)
        assert any("unknown type 'group'" in i for i in issues)


# ---------------------------------------------------------------------------
# tool.list — canvas inspector dropdown source
# ---------------------------------------------------------------------------


class TestToolList:

    def test_tool_list_shape(self, daemon):
        registry = ToolRegistry()
        registry.register(EchoTool())
        daemon._cached_tool_registry = registry
        res = asyncio.run(daemon._handle_tool_list({}))
        assert "tools" in res
        names = [t["name"] for t in res["tools"]]
        assert "echo" in names
        entry = next(t for t in res["tools"] if t["name"] == "echo")
        assert "description" in entry


# ---------------------------------------------------------------------------
# #397: workflow RPC integration — orchestration GUI backend
# ---------------------------------------------------------------------------


class _StubEngineDaemon:
    def __init__(self, engine):
        self._engine = engine

    def _get_workflow_engine(self):
        return self._engine


class TestWorkflowRpc:

    def _dispatcher(self):
        from agent_runtime.dispatchers.workflow import WorkflowDispatcher
        from agent_runtime.workflow_engine import WorkflowEngine

        engine = WorkflowEngine()
        return WorkflowDispatcher(_StubEngineDaemon(engine)), engine

    async def test_create_list_get_roundtrip(self):
        dispatcher, _ = self._dispatcher()
        created = await dispatcher.get_handlers()["workflow.create"]({
            "name": "orch",
            "phases": [
                {"name": "p1", "pattern": "pipeline",
                 "agent_configs": [{"agent_id": "a1", "name": "a1"}]},
                {"name": "p2", "pattern": "parallel_barrier",
                 "agent_configs": [{"agent_id": "a1"}, {"agent_id": "a2"}]},
            ],
        })
        wf_id = created["id"]
        assert len(created["phases"]) == 2
        assert created["phases"][1]["pattern"] == "parallel_barrier"
        listed = await dispatcher.get_handlers()["workflow.list"]({})
        assert any(w["id"] == wf_id for w in listed["workflows"])
        got = await dispatcher.get_handlers()["workflow.get"]({"workflow_id": wf_id})
        assert got["name"] == "orch"

    async def test_execute_and_status(self):
        dispatcher, _ = self._dispatcher()
        created = await dispatcher.get_handlers()["workflow.create"]({
            "name": "orch2",
            "phases": [{"name": "p1", "pattern": "pipeline",
                        "agent_configs": [{"name": "stage", "system_prompt": "be brief"}]}],
        })
        # no gateway configured — _run_agent passthrough returns the input
        res = await dispatcher.get_handlers()["workflow.execute"]({
            "workflow_id": created["id"],
            "input": "hi",
        })
        assert str(res.get("id", "")).startswith("wf_run")

    async def test_cancel_unknown_run(self):
        dispatcher, _ = self._dispatcher()
        res = await dispatcher.get_handlers()["workflow.cancel"]({"run_id": "nope"})
        assert res["cancelled"] is False

    async def test_status_unknown_run_raises(self):
        dispatcher, _ = self._dispatcher()
        with pytest.raises(ValueError, match="not found"):
            await dispatcher.get_handlers()["workflow.status"]({"run_id": "nope"})

    async def test_list_runs_for_workflow(self):
        from agent_runtime.workflow_engine import WorkflowRun, WorkflowStatus

        dispatcher, engine = self._dispatcher()
        engine._runs["wf_run_x"] = WorkflowRun(
            id="wf_run_x", workflow_id="wf_1", status=WorkflowStatus.RUNNING
        )
        res = await dispatcher.get_handlers()["workflow.list"]({"workflow_id": "wf_1"})
        assert [r["id"] for r in res["runs"]] == ["wf_run_x"]


# ---------------------------------------------------------------------------
# Canvas / page source assertions — client-side features are TSX-only
# ---------------------------------------------------------------------------


class TestCanvasSourceParity:

    def test_canvas_has_schema_driven_inspector(self):
        source = (ROOT / "web" / "src" / "pages" / "studio" / "WorkflowCanvasPage.tsx").read_text()
        for marker in (
            "fieldsForNode",
            "tool.list",
            "fusion/node-type",
            "screenToFlowPosition",
            "onDrop",
            "validateGraphClient",
            "strict",
            "import.meta.glob",
            "only_node",
            "parentId",
            "fusionGroup",
            "exportGraph",
            "importGraph",
        ):
            assert marker in source, marker

    def test_canvas_has_edge_label_and_grouping(self):
        source = (ROOT / "web" / "src" / "pages" / "studio" / "WorkflowCanvasPage.tsx").read_text()
        assert "groupSelected" in source
        assert "Branch label" in source or "分支标签" in source
        assert "extent: 'parent' as const" in source

    def test_workflows_page_has_orchestrations(self):
        source = (ROOT / "web" / "src" / "pages" / "WorkflowsPage.tsx").read_text()
        for marker in (
            "workflow.create",
            "workflow.execute",
            "workflow.list",
            "workflow.cancel",
            "workflow.delete",
            "parallel_barrier",
            "judge_panel",
            "adversarial_verify",
        ):
            assert marker in source, marker

    def test_graphschema_has_field_specs_and_validator(self):
        source = (ROOT / "web" / "src" / "lib" / "graphSchema.ts").read_text()
        for marker in (
            "NODE_FIELDS",
            "fieldsForNode",
            "validateGraphClient",
            "groupNodes",
            "parent: n.parentId",
        ):
            assert marker in source, marker

    def test_examples_templates_exist(self):
        examples = sorted((ROOT / "examples").glob("*.json"))
        assert len(examples) >= 7
        for path in examples:
            data = json.loads(path.read_text())
            assert "nodes" in data

    def test_rpc_bridge_allows_canvas_methods(self):
        from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

        for method in (
            "graph.get",
            "graph.update",
            "graph.create",
            "tool.list",
            "workflow.list",
            "workflow.create",
            "workflow.execute",
            "workflow.cancel",
            "agent.list",
            "budget.status",
            "safety.get_pending_actions",
        ):
            assert method in WEB_RPC_ALLOWLIST, method
