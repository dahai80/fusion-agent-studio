"""Tests for Phase 7, 8, 9 modules."""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.knowledge_engine import (
    EMBEDDING_DIM,
    KnowledgeEngine,
    KnowledgeEntry,
    _stub_embedding,
)
from agent_runtime.llm_gateway import (
    LLMGateway,
    ModelConfig,
    ModelStats,
    _ModelCircuitBreaker,
)
from agent_runtime.swarm_router import (
    HandoffContext,
    SwarmAgent,
    SwarmRouter,
    TaskDelegation,
)


class TestKnowledgeEntry:
    def test_auto_id(self):
        e = KnowledgeEntry(content="test")
        assert e.id
        assert len(e.id) == 12

    def test_auto_timestamps(self):
        e = KnowledgeEntry(content="test")
        assert e.created_at > 0
        assert e.updated_at > 0

    def test_to_dict_from_dict_roundtrip(self):
        e = KnowledgeEntry(id="k1", content="hello", scope="test", metadata={"k": "v"})
        d = e.to_dict()
        e2 = KnowledgeEntry.from_dict(d)
        assert e2.id == "k1"
        assert e2.content == "hello"
        assert e2.scope == "test"
        assert e2.metadata == {"k": "v"}


class TestStubEmbedding:
    def test_dimension(self):
        vec = _stub_embedding("hello")
        assert len(vec) == EMBEDDING_DIM

    def test_normalized(self):
        vec = _stub_embedding("test")
        norm = sum(v * v for v in vec) ** 0.5
        assert abs(norm - 1.0) < 0.01

    def test_deterministic(self):
        v1 = _stub_embedding("same text")
        v2 = _stub_embedding("same text")
        assert v1 == v2

    def test_different_texts(self):
        v1 = _stub_embedding("text a")
        v2 = _stub_embedding("text b")
        assert v1 != v2


class TestKnowledgeEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        db = str(tmp_path / "test_kb.db")
        eng = KnowledgeEngine(db_path=db)
        yield eng
        eng.close()

    def test_ingest(self, engine):
        entry = engine.ingest("hello world", scope="test")
        assert entry.id
        assert entry.content == "hello world"
        assert entry.scope == "test"

    def test_ingest_with_metadata(self, engine):
        entry = engine.ingest("test", metadata={"source": "unit"})
        assert entry.metadata["source"] == "unit"

    def test_ingest_with_custom_embedding(self, engine):
        emb = [0.1] * EMBEDDING_DIM
        entry = engine.ingest("test", embedding=emb)
        assert len(entry.embedding) == EMBEDDING_DIM

    def test_get(self, engine):
        entry = engine.ingest("fetch me")
        fetched = engine.get(entry.id)
        assert fetched is not None
        assert fetched.content == "fetch me"

    def test_get_not_found(self, engine):
        assert engine.get("nonexistent") is None

    def test_delete(self, engine):
        entry = engine.ingest("delete me")
        assert engine.delete(entry.id)
        assert engine.get(entry.id) is None

    def test_delete_not_found(self, engine):
        assert not engine.delete("nonexistent")

    def test_search_fts(self, engine):
        engine.ingest("python programming language", scope="lang")
        engine.ingest("rust systems language", scope="lang")
        results = engine.search("python", mode="fts")
        assert len(results) >= 1
        assert any("python" in r.content for r in results)

    def test_search_fts_with_scope(self, engine):
        engine.ingest("python in scope a", scope="a")
        engine.ingest("python in scope b", scope="b")
        results = engine.search("python", scope="a", mode="fts")
        assert all(r.scope == "a" for r in results)

    def test_search_vector_fallback_fts(self, engine):
        engine.ingest("vector search test")
        results = engine.search("vector", mode="vector")
        assert len(results) >= 1

    def test_search_hybrid(self, engine):
        engine.ingest("hybrid search test content")
        results = engine.search("hybrid", mode="hybrid")
        assert len(results) >= 1

    def test_list_entries(self, engine):
        engine.ingest("entry 1")
        engine.ingest("entry 2")
        entries = engine.list_entries()
        assert len(entries) >= 2

    def test_list_entries_by_scope(self, engine):
        engine.ingest("scoped", scope="myscope")
        engine.ingest("other", scope="other")
        entries = engine.list_entries(scope="myscope")
        assert all(e.scope == "myscope" for e in entries)

    def test_count(self, engine):
        engine.ingest("count me 1")
        engine.ingest("count me 2")
        assert engine.count() >= 2

    def test_count_by_scope(self, engine):
        engine.ingest("scoped count", scope="s1")
        engine.ingest("other count", scope="s2")
        assert engine.count(scope="s1") >= 1

    def test_search_limit(self, engine):
        for i in range(5):
            engine.ingest(f"limit test item {i}")
        results = engine.search("limit", mode="fts", limit=2)
        assert len(results) <= 2


class TestModelConfig:
    def test_default_init(self):
        mc = ModelConfig(name="test-model")
        assert mc.provider == "local"
        assert mc.context_length == 4096
        assert "chat" in mc.capabilities

    def test_to_dict_from_dict_roundtrip(self):
        mc = ModelConfig(
            name="m1", provider="cloud", priority=5, capabilities=["chat", "tool_use"]
        )
        d = mc.to_dict()
        mc2 = ModelConfig.from_dict(d)
        assert mc2.name == "m1"
        assert mc2.provider == "cloud"
        assert mc2.priority == 5
        assert "tool_use" in mc2.capabilities


class TestModelStats:
    def test_avg_latency(self):
        s = ModelStats(model_name="m1", successes=2, total_latency=1.0)
        assert s.avg_latency == 0.5

    def test_error_rate(self):
        s = ModelStats(model_name="m1", requests=10, failures=2)
        assert s.error_rate == 0.2

    def test_zero_division(self):
        s = ModelStats(model_name="m1")
        assert s.avg_latency == 0.0
        assert s.error_rate == 0.0

    def test_to_dict(self):
        s = ModelStats(model_name="m1", requests=5, successes=3, failures=2)
        d = s.to_dict()
        assert d["model_name"] == "m1"
        assert d["requests"] == 5


class TestModelCircuitBreaker:
    def test_initial_closed(self):
        cb = _ModelCircuitBreaker()
        assert not cb.is_open("model1")

    def test_trips_after_threshold(self):
        cb = _ModelCircuitBreaker(threshold=2, reset_time=60.0)
        cb.record_failure("model1")
        cb.record_failure("model1")
        assert cb.is_open("model1")

    def test_success_resets(self):
        cb = _ModelCircuitBreaker(threshold=2, reset_time=60.0)
        cb.record_failure("model1")
        cb.record_failure("model1")
        assert cb.is_open("model1")
        cb.record_success("model1")
        assert not cb.is_open("model1")


class TestLLMGateway:
    def test_register_model(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="local-llm", priority=10))
        assert gw.get_model("local-llm") is not None

    def test_unregister_model(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="local-llm"))
        assert gw.unregister_model("local-llm")
        assert not gw.unregister_model("nonexistent")

    def test_route_by_priority(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="low", priority=1))
        gw.register_model(ModelConfig(name="high", priority=10))
        selected = gw.route()
        assert selected.name == "high"

    def test_route_by_capability(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="chat-only", capabilities=["chat"]))
        gw.register_model(
            ModelConfig(name="multimodal", capabilities=["chat", "vision"])
        )
        selected = gw.route(capability="vision")
        assert selected.name == "multimodal"

    def test_route_no_match(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="chat-only", capabilities=["chat"]))
        assert gw.route(capability="embedding") is None

    def test_route_min_context(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="small", context_length=2048))
        gw.register_model(ModelConfig(name="large", context_length=32768))
        selected = gw.route(min_context=8192)
        assert selected.name == "large"

    def test_route_exclude(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="m1", priority=10))
        gw.register_model(ModelConfig(name="m2", priority=5))
        selected = gw.route(exclude={"m1"})
        assert selected.name == "m2"

    def test_fallback_chain(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="primary", priority=10))
        gw.register_model(ModelConfig(name="secondary", priority=5))
        gw.register_model(ModelConfig(name="tertiary", priority=1))
        chain = gw.get_fallback_chain()
        assert len(chain) == 3
        assert chain[0].name == "primary"

    def test_execute_no_model(self):
        gw = LLMGateway()
        result = gw.execute([{"role": "user", "content": "hi"}])
        assert "error" in result

    def test_execute_stub(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="stub-model"))
        result = gw.execute([{"role": "user", "content": "hi"}], model="stub-model")
        assert result.get("model") == "stub-model"

    def test_embed_stub(self):
        gw = LLMGateway()
        vec = gw.embed("test text")
        assert len(vec) == 64

    def test_embed_with_model(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="emb", capabilities=["embedding"]))
        vec = gw.embed("test text", model="emb")
        assert len(vec) == 64

    def test_get_stats(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="m1"))
        stats = gw.get_stats()
        assert "m1" in stats

    def test_get_stats_by_model(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="m1"))
        stats = gw.get_stats("m1")
        assert stats["model_name"] == "m1"

    def test_list_models(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="a"))
        gw.register_model(ModelConfig(name="b"))
        result = asyncio.run(gw.list_models())
        assert len(result) == 2

    def test_circuit_breaker_blocks_route(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="m1", priority=10))
        gw.register_model(ModelConfig(name="m2", priority=1))
        for _ in range(3):
            gw._cb.record_failure("m1")
        selected = gw.route()
        assert selected.name == "m2"


class TestSwarmAgent:
    def test_auto_id(self):
        a = SwarmAgent(name="test")
        assert a.id
        assert len(a.id) == 8

    def test_to_dict_from_dict_roundtrip(self):
        a = SwarmAgent(
            id="a1",
            name="coder",
            capabilities=["code"],
            handoff_targets=["reviewer"],
            max_hops=2,
        )
        d = a.to_dict()
        a2 = SwarmAgent.from_dict(d)
        assert a2.id == "a1"
        assert a2.name == "coder"
        assert a2.capabilities == ["code"]
        assert a2.max_hops == 2


class TestTaskDelegation:
    def test_auto_id(self):
        t = TaskDelegation(task="review", delegator="a1", delegatee="a2")
        assert t.id
        assert t.status == "pending"

    def test_to_dict_from_dict_roundtrip(self):
        t = TaskDelegation(
            id="t1", task="review", delegator="a1", delegatee="a2", hop_count=1
        )
        d = t.to_dict()
        t2 = TaskDelegation.from_dict(d)
        assert t2.id == "t1"
        assert t2.hop_count == 1


class TestHandoffContext:
    def test_to_dict_from_dict_roundtrip(self):
        ctx = HandoffContext(
            conversation=[{"role": "user", "content": "hi"}], hop_count=1, task_id="t1"
        )
        d = ctx.to_dict()
        ctx2 = HandoffContext.from_dict(d)
        assert ctx2.hop_count == 1
        assert len(ctx2.conversation) == 1


class TestSwarmRouter:
    def test_register_agent(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        assert sr.get_agent("a1") is not None

    def test_unregister_agent(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        assert sr.unregister_agent("a1")
        assert not sr.unregister_agent("nonexistent")

    def test_list_agents(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        sr.register_agent(SwarmAgent(id="a2", name="reviewer"))
        assert len(sr.list_agents()) == 2

    def test_find_by_capability(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder", capabilities=["code"]))
        sr.register_agent(SwarmAgent(id="a2", name="writer", capabilities=["write"]))
        found = sr.find_agent_by_capability("code")
        assert found.id == "a1"

    def test_find_by_capability_exclude(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder", capabilities=["code"]))
        sr.register_agent(SwarmAgent(id="a2", name="coder2", capabilities=["code"]))
        found = sr.find_agent_by_capability("code", exclude={"a1"})
        assert found.id == "a2"

    def test_find_no_match(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", capabilities=["code"]))
        assert sr.find_agent_by_capability("vision") is None

    def test_delegate_by_capability(self):
        sr = SwarmRouter()
        sr.register_agent(
            SwarmAgent(id="a1", name="supervisor", capabilities=["manage"])
        )
        sr.register_agent(SwarmAgent(id="a2", name="coder", capabilities=["code"]))
        delegation = sr.delegate("a1", "write code", capability="code")
        assert delegation is not None
        assert delegation.delegatee == "a2"
        assert delegation.hop_count == 1

    def test_delegate_by_handoff_target(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder", handoff_targets=["a2"]))
        sr.register_agent(SwarmAgent(id="a2", name="reviewer", capabilities=["review"]))
        delegation = sr.delegate("a1", "review this")
        assert delegation is not None
        assert delegation.delegatee == "a2"

    def test_delegate_no_delegator(self):
        sr = SwarmRouter()
        assert sr.delegate("nonexistent", "task") is None

    def test_delegate_no_delegatee(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", capabilities=["code"]))
        assert sr.delegate("a1", "vision task", capability="vision") is None

    def test_handoff(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        sr.register_agent(SwarmAgent(id="a2", name="reviewer"))
        ctx = HandoffContext(hop_count=0)
        new_ctx = sr.handoff("a1", "a2", ctx)
        assert new_ctx is not None
        assert new_ctx.hop_count == 1
        assert new_ctx.metadata["handed_off_from"] == "a1"

    def test_handoff_blocked_by_max_hops(self):
        sr = SwarmRouter(max_hops=2)
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        sr.register_agent(SwarmAgent(id="a2", name="reviewer"))
        ctx = HandoffContext(hop_count=2)
        result = sr.handoff("a1", "a2", ctx)
        assert result is None

    def test_handoff_agent_not_found(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        ctx = HandoffContext()
        assert sr.handoff("a1", "nonexistent", ctx) is None

    def test_handoff_preserves_conversation(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2"))
        ctx = HandoffContext(
            conversation=[{"role": "user", "content": "hello"}], hop_count=0
        )
        new_ctx = sr.handoff("a1", "a2", ctx)
        assert len(new_ctx.conversation) == 1

    def test_evaluate(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"]))
        delegation = sr.delegate("a1", "task", capability="code")
        result = sr.evaluate(delegation.id, {"status": "done"})
        assert result.status == "completed"
        assert result.result["status"] == "done"

    def test_evaluate_not_found(self):
        sr = SwarmRouter()
        assert sr.evaluate("nonexistent", {}) is None

    def test_escalate(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"]))
        delegation = sr.delegate("a1", "task", capability="code")
        result = sr.escalate(delegation.id, reason="stuck")
        assert result.status == "escalated"
        assert result.result["reason"] == "stuck"

    def test_auto_escalate_max_hops(self):
        sr = SwarmRouter(max_hops=1)
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"], max_hops=1))
        delegation = sr.delegate("a1", "task", capability="code")
        delegation.hop_count = 1
        result = sr.auto_escalate_if_needed(delegation.id)
        assert result is not None
        assert result.status == "escalated"

    def test_auto_escalate_offline_agent(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"], status="online"))
        delegation = sr.delegate("a1", "task", capability="code")
        assert delegation is not None
        sr._agents["a2"].status = "offline"
        result = sr.auto_escalate_if_needed(delegation.id)
        assert result is not None
        assert result.status == "escalated"

    def test_auto_escalate_not_needed(self):
        sr = SwarmRouter(max_hops=3)
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"], max_hops=3))
        delegation = sr.delegate("a1", "task", capability="code")
        assert sr.auto_escalate_if_needed(delegation.id) is None

    def test_list_delegations(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"]))
        sr.delegate("a1", "task1", capability="code")
        sr.delegate("a1", "task2", capability="code")
        assert len(sr.list_delegations()) == 2

    def test_list_delegations_by_status(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"]))
        d = sr.delegate("a1", "task", capability="code")
        sr.evaluate(d.id, {"ok": True})
        completed = sr.list_delegations(status="completed")
        assert len(completed) == 1

    def test_handoff_log(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2"))
        sr.handoff("a1", "a2", HandoffContext())
        log = sr.get_handoff_log()
        assert len(log) == 1
        assert log[0]["from"] == "a1"

    def test_stats(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1"))
        sr.register_agent(SwarmAgent(id="a2", capabilities=["code"]))
        sr.delegate("a1", "task", capability="code")
        stats = sr.get_stats()
        assert stats["agents"] == 2
        assert stats["total_delegations"] == 1


class TestSwarmRouterComposition:
    # SwarmRouter composes FMProtocol + SafetyGateway (Phase composition landing).

    def test_delegate_sends_fmp_message(self):
        sr = SwarmRouter()
        sr.register_agent(
            SwarmAgent(id="a1", name="supervisor", capabilities=["manage"])
        )
        sr.register_agent(SwarmAgent(id="a2", name="coder", capabilities=["code"]))
        sr.delegate("a1", "write code", capability="code")
        assert sr.fmp._stats["sent"] >= 1
        types = [m.message_type for m in sr.fmp._message_log]
        assert "delegation" in types

    def test_handoff_sends_fmp_message(self):
        sr = SwarmRouter()
        sr.register_agent(SwarmAgent(id="a1", name="coder"))
        sr.register_agent(SwarmAgent(id="a2", name="reviewer"))
        ctx = HandoffContext(
            conversation=[{"role": "a1", "content": "done"}], hop_count=0, task_id="t1"
        )
        sr.handoff("a1", "a2", ctx)
        assert sr.fmp._stats["sent"] >= 1
        types = [m.message_type for m in sr.fmp._message_log]
        assert "handoff" in types

    def test_escalate_routes_through_safety(self):
        sr = SwarmRouter()
        sr.register_agent(
            SwarmAgent(id="a1", name="supervisor", capabilities=["manage"])
        )
        sr.register_agent(SwarmAgent(id="a2", name="coder", capabilities=["code"]))
        delegation = sr.delegate("a1", "run shell", capability="code")
        assert delegation is not None
        result = sr.escalate(delegation.id, reason="stuck")
        assert result is not None
        assert result.result["escalated"] is True
        assert result.result["reason"] == "stuck"
        assert "safety_action" in result.result
        assert "requires_approval" in result.result
        assert "action_id" in result.result
