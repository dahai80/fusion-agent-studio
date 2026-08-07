"""Tests for Phase 3 modules: graph_editor, metrics_engine, agent_marketplace."""

from __future__ import annotations

import time

import pytest

from agent_runtime.agent_marketplace import (
    AgentMarketplace,
    MarketEntry,
)
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.graph_editor import (
    GraphDocument,
    GraphEditor,
    NodePosition,
    ValidationIssue,
    ValidationResult,
    auto_layout,
    validate_graph,
)
from agent_runtime.metrics_engine import (
    InferenceMetrics,
    MetricsEngine,
    MetricsSummary,
    SessionRecord,
)


def _build_graph(nodes_spec, edges_spec):
    g = AgentGraph(name="test")
    for nid, ntype in nodes_spec:
        g.add_node(nid, NodeConfig(type=ntype))
    for src, tgt in edges_spec:
        g.add_edge(src, tgt)
    return g


class TestGraphValidation:
    def test_valid_simple_graph(self):
        g = _build_graph(
            [("start", "start"), ("llm", "llm"), ("end", "end")],
            [("start", "llm"), ("llm", "end")],
        )
        result = validate_graph(g)
        assert result.valid

    def test_no_start_node(self):
        g = _build_graph([("llm", "llm"), ("end", "end")], [("llm", "end")])
        result = validate_graph(g)
        assert not result.valid
        assert any("No start" in i.message for i in result.issues)

    def test_multiple_start_nodes(self):
        g = _build_graph(
            [("s1", "start"), ("s2", "start"), ("end", "end")],
            [("s1", "end"), ("s2", "end")],
        )
        result = validate_graph(g)
        assert not result.valid

    def test_cycle_detection(self):
        g = _build_graph(
            [("start", "start"), ("a", "llm"), ("b", "llm"), ("end", "end")],
            [("start", "a"), ("a", "b"), ("b", "a"), ("b", "end")],
        )
        result = validate_graph(g)
        assert not result.valid
        assert any("cycle" in i.message.lower() for i in result.issues)

    def test_unreachable_node(self):
        g = _build_graph(
            [("start", "start"), ("end", "end"), ("orphan", "llm")], [("start", "end")]
        )
        result = validate_graph(g)
        assert any("Unreachable" in i.message for i in result.issues)

    def test_condition_few_branches(self):
        g = _build_graph(
            [("start", "start"), ("cond", "condition"), ("end", "end")],
            [("start", "cond"), ("cond", "end")],
        )
        result = validate_graph(g)
        assert any("fewer than 2" in i.message for i in result.issues)

    def test_invalid_edge_source(self):
        g = _build_graph([("start", "start"), ("end", "end")], [])
        g.add_edge("nonexistent", "end")
        result = validate_graph(g)
        assert not result.valid

    def test_no_end_node_warning(self):
        g = _build_graph([("start", "start"), ("llm", "llm")], [("start", "llm")])
        result = validate_graph(g)
        assert any("No end" in i.message for i in result.issues)

    def test_empty_graph(self):
        g = AgentGraph(name="empty")
        result = validate_graph(g)
        assert not result.valid


class TestAutoLayout:
    def test_linear_graph(self):
        g = _build_graph(
            [("start", "start"), ("llm", "llm"), ("end", "end")],
            [("start", "llm"), ("llm", "end")],
        )
        positions = auto_layout(g)
        assert len(positions) == 3
        pos_map = {p.node_id: p for p in positions}
        assert pos_map["start"].y < pos_map["llm"].y
        assert pos_map["llm"].y < pos_map["end"].y

    def test_branching_graph(self):
        g = _build_graph(
            [("start", "start"), ("a", "llm"), ("b", "llm"), ("end", "end")],
            [("start", "a"), ("start", "b"), ("a", "end"), ("b", "end")],
        )
        positions = auto_layout(g)
        assert len(positions) == 4
        layer0 = [p for p in positions if p.y == min(p2.y for p2 in positions)]
        assert len(layer0) == 1
        assert layer0[0].node_id == "start"

    def test_empty_graph(self):
        g = AgentGraph(name="empty")
        positions = auto_layout(g)
        assert positions == []


class TestValidationResult:
    def test_to_dict(self):
        r = ValidationResult(valid=True, issues=[ValidationIssue("error", "n1", "bad")])
        d = r.to_dict()
        assert d["valid"] is True
        assert len(d["issues"]) == 1

    def test_empty_issues(self):
        r = ValidationResult(valid=True)
        assert r.to_dict()["issues"] == []


class TestNodePosition:
    def test_to_dict(self):
        p = NodePosition(node_id="x", x=10, y=20)
        d = p.to_dict()
        assert d["node_id"] == "x"
        assert d["x"] == 10
        assert d["width"] == 200.0


class TestGraphDocument:
    def test_to_dict_roundtrip(self):
        doc = GraphDocument(
            id="abc", name="test", positions=[NodePosition("n1", 0, 0)], tags=["demo"]
        )
        d = doc.to_dict()
        restored = GraphDocument.from_dict(d)
        assert restored.id == "abc"
        assert restored.name == "test"
        assert len(restored.positions) == 1
        assert restored.tags == ["demo"]


class TestGraphEditor:
    def _make_valid_graph_data(self):
        g = _build_graph([("start", "start"), ("end", "end")], [("start", "end")])
        return g.to_dict()

    def test_create_and_get(self):
        editor = GraphEditor()
        doc = editor.create("my-graph", "desc", self._make_valid_graph_data())
        assert doc.name == "my-graph"
        got = editor.get(doc.id)
        assert got is not None
        assert got.name == "my-graph"

    def test_list_all(self):
        editor = GraphEditor()
        editor.create("g1")
        editor.create("g2")
        assert len(editor.list_all()) == 2

    def test_update(self):
        editor = GraphEditor()
        doc = editor.create("old")
        updated = editor.update(doc.id, name="new")
        assert updated.name == "new"
        assert updated.version == 2

    def test_update_not_found(self):
        editor = GraphEditor()
        assert editor.update("nonexistent", name="x") is None

    def test_delete(self):
        editor = GraphEditor()
        doc = editor.create("del-me")
        assert editor.delete(doc.id) is True
        assert editor.get(doc.id) is None

    def test_delete_not_found(self):
        editor = GraphEditor()
        assert editor.delete("nonexistent") is False

    def test_validate_document(self):
        editor = GraphEditor()
        doc = editor.create("v", graph_data=self._make_valid_graph_data())
        result = editor.validate(doc.id)
        assert isinstance(result, ValidationResult)

    def test_validate_not_found(self):
        editor = GraphEditor()
        result = editor.validate("nonexistent")
        assert not result.valid

    def test_compute_layout(self):
        editor = GraphEditor()
        doc = editor.create("lay", graph_data=self._make_valid_graph_data())
        positions = editor.compute_layout(doc.id)
        assert len(positions) == 2

    def test_compute_layout_not_found(self):
        editor = GraphEditor()
        assert editor.compute_layout("nonexistent") == []

    def test_duplicate(self):
        editor = GraphEditor()
        doc = editor.create("orig")
        editor.update(doc.id, tags=["t1"])
        dup = editor.duplicate(doc.id, "orig-copy")
        assert dup is not None
        assert dup.name == "orig-copy"
        assert dup.id != doc.id

    def test_duplicate_not_found(self):
        editor = GraphEditor()
        assert editor.duplicate("nonexistent") is None


# ── Metrics Engine ───────────────────────────────────────


class TestMetricsEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        db = tmp_path / "test_metrics.db"
        eng = MetricsEngine(db_path=db)
        yield eng
        eng.close()

    def test_record_inference(self, engine):
        m = InferenceMetrics(model="qwen3-0.6b", latency_ms=120.5, tokens_out=50)
        row_id = engine.record_inference(m)
        assert row_id > 0

    def test_record_session(self, engine):
        s = SessionRecord(
            session_id="s1", graph_id="g1", status="completed", duration_ms=500
        )
        row_id = engine.record_session(s)
        assert row_id > 0

    def test_query_inferences(self, engine):
        engine.record_inference(InferenceMetrics(model="qwen3-0.6b", latency_ms=100))
        engine.record_inference(InferenceMetrics(model="llama3-8b", latency_ms=200))
        results = engine.query_inferences()
        assert len(results) == 2

    def test_query_inferences_by_model(self, engine):
        engine.record_inference(InferenceMetrics(model="qwen3-0.6b", latency_ms=100))
        engine.record_inference(InferenceMetrics(model="llama3-8b", latency_ms=200))
        results = engine.query_inferences(model="qwen3-0.6b")
        assert len(results) == 1
        assert results[0].model == "qwen3-0.6b"

    def test_query_sessions(self, engine):
        engine.record_session(SessionRecord(session_id="s1", status="completed"))
        engine.record_session(SessionRecord(session_id="s2", status="error"))
        results = engine.query_sessions(status="completed")
        assert len(results) == 1

    def test_get_summary(self, engine):
        engine.record_inference(
            InferenceMetrics(
                model="qwen3-0.6b",
                latency_ms=100,
                tokens_in=10,
                tokens_out=20,
                vram_mb=1024,
            )
        )
        engine.record_session(SessionRecord(session_id="s1", status="completed"))
        summary = engine.get_summary()
        assert summary.total_inferences == 1
        assert summary.total_sessions == 1
        assert summary.avg_latency_ms == 100.0
        assert summary.peak_vram_mb == 1024.0
        assert summary.success_rate == 1.0

    def test_get_summary_empty(self, engine):
        summary = engine.get_summary()
        assert summary.total_inferences == 0
        assert summary.success_rate == 0.0

    def test_auto_timestamp(self, engine):
        before = time.time()
        m = InferenceMetrics(model="test")
        engine.record_inference(m)
        results = engine.query_inferences()
        assert results[0].timestamp >= before

    def test_query_since(self, engine):
        old_time = time.time() - 3600
        m = InferenceMetrics(model="old", timestamp=old_time)
        engine.record_inference(m)
        engine.record_inference(InferenceMetrics(model="new"))
        recent = engine.query_inferences(since=time.time() - 60)
        assert len(recent) == 1
        assert recent[0].model == "new"


class TestInferenceMetrics:
    def test_to_dict(self):
        m = InferenceMetrics(model="test", latency_ms=50)
        d = m.to_dict()
        assert d["model"] == "test"
        assert d["latency_ms"] == 50


class TestSessionRecord:
    def test_to_dict(self):
        s = SessionRecord(session_id="s1", status="completed")
        d = s.to_dict()
        assert d["session_id"] == "s1"
        assert d["status"] == "completed"


class TestMetricsSummary:
    def test_to_dict(self):
        s = MetricsSummary(total_inferences=10)
        d = s.to_dict()
        assert d["total_inferences"] == 10


# ── Agent Marketplace ────────────────────────────────────


class TestAgentMarketplace:
    @pytest.fixture
    def marketplace(self, tmp_path):
        return AgentMarketplace(store_dir=tmp_path / "market")

    def test_publish(self, marketplace):
        entry = MarketEntry(name="Test Agent", category="basic")
        entry_id = marketplace.publish(entry)
        assert entry_id
        assert marketplace.get(entry_id) is not None

    def test_unpublish(self, marketplace):
        entry = MarketEntry(name="Remove Me")
        entry_id = marketplace.publish(entry)
        assert marketplace.unpublish(entry_id) is True
        assert marketplace.get(entry_id) is None

    def test_unpublish_not_found(self, marketplace):
        assert marketplace.unpublish("nonexistent") is False

    def test_search_by_name(self, marketplace):
        marketplace.publish(MarketEntry(name="Code Reviewer", category="dev"))
        marketplace.publish(MarketEntry(name="Data Analyst", category="data"))
        results = marketplace.search(query="code")
        assert len(results) == 1
        assert results[0].name == "Code Reviewer"

    def test_search_by_category(self, marketplace):
        marketplace.publish(MarketEntry(name="A", category="dev"))
        marketplace.publish(MarketEntry(name="B", category="data"))
        results = marketplace.search(category="dev")
        assert len(results) == 1

    def test_search_by_tags(self, marketplace):
        marketplace.publish(MarketEntry(name="A", tags=["python", "code"]))
        marketplace.publish(MarketEntry(name="B", tags=["data", "sql"]))
        results = marketplace.search(tags=["python"])
        assert len(results) == 1

    def test_search_sort_by_rating(self, marketplace):
        marketplace.publish(MarketEntry(name="Low", rating=2.0))
        marketplace.publish(MarketEntry(name="High", rating=5.0))
        results = marketplace.search(sort_by="rating")
        assert results[0].name == "High"

    def test_list_categories(self, marketplace):
        marketplace.publish(MarketEntry(name="A", category="dev"))
        marketplace.publish(MarketEntry(name="B", category="data"))
        cats = marketplace.list_categories()
        assert "dev" in cats
        assert "data" in cats

    def test_export_import(self, marketplace, tmp_path):
        entry = MarketEntry(
            name="Export Test",
            author="test",
            category="dev",
            tags=["test"],
            graph_data={"nodes": {}, "edges": []},
        )
        entry_id = marketplace.publish(entry)
        export_dir = tmp_path / "exports"
        exported_path = marketplace.export_agent(entry_id, export_dir)
        assert exported_path is not None
        assert (exported_path / "manifest.json").exists()
        assert (exported_path / "graph.json").exists()

        marketplace2 = AgentMarketplace(store_dir=tmp_path / "market2")
        imported = marketplace2.import_agent(exported_path)
        assert imported is not None
        assert imported.name == "Export Test"

    def test_export_not_found(self, marketplace, tmp_path):
        result = marketplace.export_agent("nonexistent", tmp_path)
        assert result is None

    def test_import_no_manifest(self, marketplace, tmp_path):
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        result = marketplace.import_agent(bad_dir)
        assert result is None

    def test_install(self, marketplace, tmp_path):
        entry = MarketEntry(name="Install Test", graph_data={"nodes": {}})
        entry_id = marketplace.publish(entry)
        result = marketplace.install(entry_id, tmp_path / "installed")
        assert result is not None
        got = marketplace.get(entry_id)
        assert got.downloads == 1

    def test_market_entry_to_dict(self):
        e = MarketEntry(id="x", name="test", tags=["a"])
        d = e.to_dict()
        assert d["id"] == "x"
        assert d["tags"] == ["a"]

    def test_market_entry_from_dict(self):
        d = {"id": "x", "name": "test", "tags": ["a"], "version": "2.0.0"}
        e = MarketEntry.from_dict(d)
        assert e.id == "x"
        assert e.version == "2.0.0"
