"""Tests for persistence layer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_runtime.persistence import AgentStore, Checkpoint
from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph, NodeConfig


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        s = AgentStore(str(db_path))
        yield s
        s.close()


@pytest.fixture
def sample_graph():
    graph = AgentGraph(name="Test Graph", description="A test graph")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    return graph


class TestAgentStore:
    def test_save_and_load_graph(self, store, sample_graph):
        store.save_graph(sample_graph)
        loaded = store.load_graph(sample_graph.id)
        assert loaded is not None
        assert loaded.name == "Test Graph"
        assert loaded.description == "A test graph"
        assert len(loaded.nodes) == 3

    def test_save_updates_existing_graph(self, store, sample_graph):
        store.save_graph(sample_graph)
        sample_graph.name = "Updated Name"
        store.save_graph(sample_graph)
        loaded = store.load_graph(sample_graph.id)
        assert loaded.name == "Updated Name"

    def test_load_nonexistent_graph(self, store):
        loaded = store.load_graph("nonexistent")
        assert loaded is None

    def test_delete_graph(self, store, sample_graph):
        store.save_graph(sample_graph)
        deleted = store.delete_graph(sample_graph.id)
        assert deleted is True
        assert store.load_graph(sample_graph.id) is None

    def test_delete_nonexistent_graph(self, store):
        deleted = store.delete_graph("nonexistent")
        assert deleted is False

    def test_list_graphs(self, store, sample_graph):
        store.save_graph(sample_graph)
        graphs = store.list_graphs()
        assert len(graphs) >= 1
        assert graphs[0]["name"] == "Test Graph"

    def test_list_graphs_empty(self, store):
        graphs = store.list_graphs()
        assert len(graphs) == 0

    def test_create_session(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("session-1", sample_graph.id, "Test Run")
        session = store.get_session("session-1")
        assert session is not None
        assert session["session_id"] == "session-1"
        assert session["status"] == "created"

    def test_get_nonexistent_session(self, store):
        session = store.get_session("nonexistent")
        assert session is None

    def test_update_session_status(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("s1", sample_graph.id, "Test")
        store.update_session_status("s1", "running")
        session = store.get_session("s1")
        assert session["status"] == "running"

        store.update_session_status("s1", "completed")
        session = store.get_session("s1")
        assert session["status"] == "completed"
        assert session["finished_at"] is not None

    def test_update_session_failed(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("s1", sample_graph.id)
        store.update_session_status("s1", "failed")
        session = store.get_session("s1")
        assert session["status"] == "failed"

    def test_list_sessions(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("s1", sample_graph.id, "Run 1")
        store.create_session("s2", sample_graph.id, "Run 2")
        sessions = store.list_sessions()
        assert len(sessions) == 2

    def test_save_and_load_checkpoint(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("s1", sample_graph.id)

        ctx = AgentContext(session_id="s1")
        ctx.add_message("user", "Hello")
        ctx.iteration_count = 3

        ckpt_id = store.save_checkpoint("s1", ctx, "llm_node")
        assert ckpt_id > 0

        loaded = store.load_latest_checkpoint("s1")
        assert loaded is not None
        assert loaded.session_id == "s1"
        assert loaded.current_node_id == "llm_node"
        assert loaded.iteration_count == 3

        # Verify context can be restored
        restored_ctx = AgentContext.from_dict(json.loads(loaded.context_json))
        assert restored_ctx.session_id == "s1"
        assert len(restored_ctx.messages) == 1

    def test_load_checkpoint_no_sessions(self, store):
        loaded = store.load_latest_checkpoint("nonexistent")
        assert loaded is None

    def test_list_checkpoints(self, store, sample_graph):
        store.save_graph(sample_graph)
        store.create_session("s1", sample_graph.id)
        ctx = AgentContext(session_id="s1")
        store.save_checkpoint("s1", ctx, "node1")
        store.save_checkpoint("s1", ctx, "node2")

        checkpoints = store.list_checkpoints("s1")
        assert len(checkpoints) >= 2

    def test_default_db_path(self):
        store = AgentStore()
        assert store.db_path == Path.home() / ".fusion-agent-studio" / "store.db"
        store.close()

    def test_checkpoint_defaults(self):
        ckpt = Checkpoint(
            session_id="s1",
            graph_id="g1",
            context_json="{}",
            current_node_id="n1",
            iteration_count=0,
        )
        assert ckpt.created_at > 0

    def test_checkpoint_to_dict(self):
        ckpt = Checkpoint(
            session_id="s1",
            graph_id="g1",
            context_json='{"msg": "hi"}',
            current_node_id="n1",
            iteration_count=5,
            created_at=100.0,
        )
        d = ckpt.to_dict()
        assert d["session_id"] == "s1"
        assert d["iteration_count"] == 5
        assert d["created_at"] == 100.0
