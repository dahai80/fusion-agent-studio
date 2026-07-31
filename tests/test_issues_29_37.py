"""Tests for issues #29-#37: agent_definition, agent_api, cowork_manager,
langgraph_engine, artifact_tools.

Importers: pytest test runner.
Affected API: AgentDefinition, AgentStatusTracker, CoworkManager, LangGraphEngine, ArtifactManager.
Data schemas: all dataclasses from the 5 modules under test.
User instruction: "后续功能也要马上启动落地实施".
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from agent_runtime.agent_definition import (
    AgentDefinition,
    AgentKnowledgeConfig,
    AgentMetadataConfig,
    AgentModelConfig,
    AgentOrchestrationConfig,
    AgentToolConfig,
    ArtifactPolicyConfig,
    ContextInjectionConfig,
    SCHEMA_URI,
    SCHEMA_VERSION,
)
from agent_runtime.agent_api import AgentStatusTracker, RunHistoryEntry
from agent_runtime.cowork_manager import CoworkManager
from agent_runtime.langgraph_engine import (
    LangGraphEngine,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowEdge,
    VALID_NODE_TYPES,
)
from agent_runtime.artifact_tools import ArtifactManager, ArtifactRecord, VALID_ARTIFACT_TYPES


class TestAgentDefinition:
    def test_default_definition(self):
        d = AgentDefinition()
        assert d.schema_ref == SCHEMA_URI
        assert d.schema_version == SCHEMA_VERSION
        assert d.status == "draft"
        assert d.agent_id == ""

    def test_to_dict_from_dict_roundtrip(self):
        d = AgentDefinition(
            agent_id="agent-1",
            name="Test Agent",
            version="1.0.0",
            description="A test agent",
            system_prompt="You are helpful",
            tools=[AgentToolConfig(name="search")],
            model=AgentModelConfig(model_name="llama-3.2", temperature=0.5),
        )
        data = d.to_dict()
        assert data["$schema"] == SCHEMA_URI
        assert data["agent_id"] == "agent-1"
        assert len(data["tools"]) == 1

        d2 = AgentDefinition.from_dict(data)
        assert d2.agent_id == "agent-1"
        assert d2.name == "Test Agent"
        assert d2.model.model_name == "llama-3.2"
        assert len(d2.tools) == 1
        assert d2.tools[0].name == "search"

    def test_sub_configs_roundtrip(self):
        model = AgentModelConfig(model_name="test-model", temperature=0.7, max_tokens=2048)
        m2 = AgentModelConfig.from_dict(model.to_dict())
        assert m2.model_name == "test-model"
        assert m2.temperature == 0.7

        tool = AgentToolConfig(name="calculator", type="function", description="calc")
        t2 = AgentToolConfig.from_dict(tool.to_dict())
        assert t2.name == "calculator"

        knowledge = AgentKnowledgeConfig(enable_rag=True, kb_id="kb-1", top_k=5, strategy="vector")
        k2 = AgentKnowledgeConfig.from_dict(knowledge.to_dict())
        assert k2.enable_rag is True
        assert k2.kb_id == "kb-1"

        orchestration = AgentOrchestrationConfig(chain_next="agent-2", parallel_group="grp-1", timeout_seconds=30)
        o2 = AgentOrchestrationConfig.from_dict(orchestration.to_dict())
        assert o2.chain_next == "agent-2"
        assert o2.timeout_seconds == 30

        meta = AgentMetadataConfig(author="test", tags=["test"])
        m2 = AgentMetadataConfig.from_dict(meta.to_dict())
        assert m2.author == "test"

        ctx = ContextInjectionConfig(mode="full", recent_n=5, enable_rag=False)
        c2 = ContextInjectionConfig.from_dict(ctx.to_dict())
        assert c2.mode == "full"
        assert c2.recent_n == 5

        policy = ArtifactPolicyConfig(can_create=True, can_update=True, trigger_strategy="on_complete")
        p2 = ArtifactPolicyConfig.from_dict(policy.to_dict())
        assert p2.can_create is True
        assert p2.trigger_strategy == "on_complete"

    def test_json_serialization(self):
        d = AgentDefinition(agent_id="json-test", name="JSON Agent")
        json_str = json.dumps(d.to_dict())
        data = json.loads(json_str)
        d2 = AgentDefinition.from_dict(data)
        assert d2.agent_id == "json-test"


class TestAgentStatusTracker:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tracker = AgentStatusTracker(data_dir=self.tmpdir)

    def test_initial_status(self):
        status = self.tracker.get_status("agent-1")
        assert status.status in ("unknown", "idle")

    def test_set_running_and_idle(self):
        self.tracker.set_running("agent-1", task="test task")
        status = self.tracker.get_status("agent-1")
        assert status.status == "running"

        self.tracker.set_idle("agent-1")
        status = self.tracker.get_status("agent-1")
        assert status.status == "idle"

    def test_record_and_get_history(self):
        entry = RunHistoryEntry(
            run_id="r1",
            agent_id="agent-1",
            trigger="manual",
            input_summary="test input",
            output_summary="test output",
            tokens_used=100,
            duration_ms=50,
            status="success",
            started_at=time.time(),
            completed_at=time.time() + 0.05,
        )
        self.tracker.record_run(entry)
        history = self.tracker.get_history("agent-1")
        assert len(history) >= 1
        assert history[0].run_id == "r1"

    def test_list_published(self):
        index = {"agent-1": {"name": "Test", "status": "published"}}
        result = self.tracker.list_published(agents_index=index)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "Test"


class TestCoworkManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = CoworkManager(data_dir=self.tmpdir)

    def test_add_and_list_agents(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        agents = self.mgr.list_agents("space-1")
        assert len(agents) == 1
        assert agents[0].agent_id == "agent-1"

    def test_remove_agent(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        self.mgr.remove_agent("space-1", "agent-1")
        agents = self.mgr.list_agents("space-1")
        assert len(agents) == 0

    def test_check_permission(self):
        self.mgr.add_agent("space-1", "agent-1", "viewer")
        ok = self.mgr.check_permission("space-1", "agent-1", "read")
        assert ok is True
        ok = self.mgr.check_permission("space-1", "agent-1", "write")
        assert ok is False

    def test_get_agent_status(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        status = self.mgr.get_agent_status("space-1", "agent-1")
        assert status["agent_id"] == "agent-1"

    def test_inject_context_full(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        result = self.mgr.inject_context("space-1", "agent-1", strategy="full")
        assert result["status"] == "ok"

    def test_inject_context_recent_n(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        result = self.mgr.inject_context("space-1", "agent-1", strategy="recent_n", n=3)
        assert result["status"] == "ok"

    def test_inject_context_rag(self):
        self.mgr.add_agent("space-1", "agent-1", "editor")
        result = self.mgr.inject_context("space-1", "agent-1", strategy="rag", query="test")
        assert result["status"] == "ok"


class TestLangGraphEngine:
    def setup_method(self):
        self.engine = LangGraphEngine()

    def _make_simple_wf(self, wf_id="wf-1", name="Test"):
        nodes = [
            WorkflowNode(node_id="start-1", node_type="START_NODE"),
            WorkflowNode(node_id="end-1", node_type="END_NODE"),
        ]
        edges = [WorkflowEdge(source_id="start-1", target_id="end-1")]
        return WorkflowDefinition(
            wf_id=wf_id,
            name=name,
            slash_command=f"/{wf_id}",
            nodes=nodes,
            edges=edges,
            entry_node_id="start-1",
        )

    def test_create_and_get_workflow(self):
        wf = self._make_simple_wf("wf-1", "Test Workflow")
        self.engine.create_workflow(wf)
        result = self.engine.get_workflow("wf-1")
        assert result["status"] == "ok"
        assert result["workflow"]["name"] == "Test Workflow"

    def test_list_workflows(self):
        self.engine.create_workflow(self._make_simple_wf("wf-a", "A"))
        self.engine.create_workflow(self._make_simple_wf("wf-b", "B"))
        result = self.engine.list_workflows()
        assert len(result) == 2

    def test_delete_workflow(self):
        self.engine.create_workflow(self._make_simple_wf("wf-del", "Del"))
        result = self.engine.delete_workflow("wf-del")
        assert result["status"] == "ok"
        result = self.engine.get_workflow("wf-del")
        assert result["status"] == "error"

    def test_validate_workflow_missing_start(self):
        nodes = [WorkflowNode(node_id="e1", node_type="END_NODE")]
        edges = []
        wf = WorkflowDefinition(
            wf_id="wf-bad", name="Bad", slash_command="/bad",
            nodes=nodes, edges=edges, entry_node_id="e1",
        )
        errors = self.engine.validate_workflow(wf)
        assert isinstance(errors, list)

    def test_valid_node_types(self):
        assert "START_NODE" in VALID_NODE_TYPES
        assert "END_NODE" in VALID_NODE_TYPES
        assert "CONNECTOR_NODE" in VALID_NODE_TYPES
        assert "SKILL_NODE" in VALID_NODE_TYPES
        assert "CONDITION_NODE" in VALID_NODE_TYPES
        assert "APPROVAL_GATE_NODE" in VALID_NODE_TYPES
        assert "OUTPUT_NODE" in VALID_NODE_TYPES


class TestArtifactManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.mgr = ArtifactManager(artifacts_dir=self.tmpdir)

    def test_create_artifact(self):
        result = self.mgr.create_artifact("agent-1", "test-doc", "document", "hello world")
        assert result["status"] == "ok"
        assert result["record"]["name"] == "test-doc"
        assert result["record"]["owner_agent_id"] == "agent-1"

    def test_create_invalid_type(self):
        result = self.mgr.create_artifact("agent-1", "bad", "invalid_type")
        assert result["status"] == "error"

    def test_update_artifact(self):
        create = self.mgr.create_artifact("agent-1", "doc", "document", "v1")
        aid = create["artifact_id"]
        result = self.mgr.update_artifact(aid, "agent-1", content="v2")
        assert result["status"] == "ok"
        assert result["record"]["content"] == "v2"
        assert result["record"]["version"] == 2

    def test_get_artifact(self):
        create = self.mgr.create_artifact("agent-1", "doc", "document", "content")
        aid = create["artifact_id"]
        result = self.mgr.get_artifact(aid)
        assert result["status"] == "ok"
        assert result["record"]["content"] == "content"

    def test_search_artifacts(self):
        self.mgr.create_artifact("agent-1", "report-q1", "report", "Q1 data")
        self.mgr.create_artifact("agent-1", "code-main", "code", "print('hi')")
        results = self.mgr.search_artifacts(query="report")
        assert len(results) == 1
        assert results[0]["name"] == "report-q1"

    def test_search_by_type(self):
        self.mgr.create_artifact("agent-1", "doc1", "document", "text")
        self.mgr.create_artifact("agent-1", "code1", "code", "x=1")
        results = self.mgr.search_artifacts(artifact_type="code")
        assert len(results) == 1

    def test_list_artifacts(self):
        self.mgr.create_artifact("agent-1", "a1", "document", "c1")
        self.mgr.create_artifact("agent-2", "a2", "document", "c2")
        all_artifacts = self.mgr.list_artifacts()
        assert len(all_artifacts) == 2
        agent1 = self.mgr.list_artifacts(agent_id="agent-1")
        assert len(agent1) == 1

    def test_delete_artifact(self):
        create = self.mgr.create_artifact("agent-1", "del-me", "document", "bye")
        aid = create["artifact_id"]
        result = self.mgr.delete_artifact(aid, "agent-1")
        assert result["status"] == "ok"
        result = self.mgr.get_artifact(aid)
        assert result["status"] == "error"

    def test_delete_non_owner_fails(self):
        create = self.mgr.create_artifact("agent-1", "owned", "document", "mine")
        aid = create["artifact_id"]
        result = self.mgr.delete_artifact(aid, "agent-2")
        assert result["status"] == "error"

    def test_export_artifact(self):
        create = self.mgr.create_artifact("agent-1", "export-me", "document", "data")
        aid = create["artifact_id"]
        result = self.mgr.export_artifact(aid)
        assert result["status"] == "ok"
        assert os.path.exists(result["path"])

    def test_get_active_artifacts_context(self):
        self.mgr.create_artifact("agent-1", "ctx1", "document", "hello")
        self.mgr.create_artifact("agent-1", "ctx2", "code", "x=1")
        ctx = self.mgr.get_active_artifacts_context("agent-1")
        assert "ctx1" in ctx
        assert "ctx2" in ctx

    def test_get_active_artifacts_context_empty(self):
        ctx = self.mgr.get_active_artifacts_context("no-such-agent")
        assert ctx == ""

    def test_artifact_record_roundtrip(self):
        rec = ArtifactRecord(
            artifact_id="rec-1",
            name="test",
            artifact_type="document",
            owner_agent_id="agent-1",
            content="hello",
            metadata={"key": "val"},
        )
        data = rec.to_dict()
        rec2 = ArtifactRecord.from_dict(data)
        assert rec2.artifact_id == "rec-1"
        assert rec2.metadata["key"] == "val"

    def test_valid_artifact_types(self):
        assert "document" in VALID_ARTIFACT_TYPES
        assert "code" in VALID_ARTIFACT_TYPES
        assert "data" in VALID_ARTIFACT_TYPES

    def test_persistence_across_instances(self):
        self.mgr.create_artifact("agent-1", "persist-test", "document", "saved")
        mgr2 = ArtifactManager(artifacts_dir=self.tmpdir)
        all_a = mgr2.list_artifacts()
        assert len(all_a) == 1
        assert all_a[0]["name"] == "persist-test"
