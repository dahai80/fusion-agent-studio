"""Tests for C16 P1-10: soul.md unified loading (chat/workflow/session paths) +
memory semantic type classification (user/feedback/project/reference).
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from agent_runtime.agent_package import (
    AgentPackage,
    resolve_soul_prompt,
)
from agent_runtime.chat_engine import ChatEngine, ChatSession
from agent_runtime.memory_engine import (
    DEFAULT_MEMORY_TYPE,
    MemoryEngine,
    MemoryEntry,
    classify_memory_type,
)


@pytest.fixture
def tmp_agents_root(monkeypatch, tmp_path):
    fake_root = tmp_path / "agents"
    fake_root.mkdir()
    monkeypatch.setattr("agent_runtime.agent_package.AGENTS_ROOT", fake_root)
    return fake_root


@pytest.fixture
def memory_engine(tmp_path):
    eng = MemoryEngine(db_path=str(tmp_path / "c16_mem.db"))
    yield eng
    eng.close()


# === memory_type classification ===


class TestClassifyMemoryType:
    def test_feedback_keywords(self):
        assert classify_memory_type("always use 4-space indent") == "feedback"
        assert classify_memory_type("don't push to main") == "feedback"
        assert classify_memory_type("rule: no docstrings") == "feedback"

    def test_user_keywords(self):
        assert classify_memory_type("i am a backend engineer") == "user"
        assert classify_memory_type("my role is devops") == "user"
        assert classify_memory_type("i prefer rust") == "user"

    def test_reference_keywords(self):
        assert classify_memory_type("see https://example.com/docs") == "reference"
        assert classify_memory_type("ticket JIRA-123 broken") == "reference"
        assert classify_memory_type("dashboard at grafana") == "reference"

    def test_default_project(self):
        assert classify_memory_type("ran graph.execute on workflow") == "project"
        assert classify_memory_type("") == "project"
        assert classify_memory_type("random execution output") == "project"


# === MemoryEntry memory_type field ===


class TestMemoryEntryType:
    def test_default_type(self):
        assert MemoryEntry(content="x").memory_type == DEFAULT_MEMORY_TYPE

    def test_invalid_type_defaults(self):
        entry = MemoryEntry(content="x", memory_type="bogus")
        assert entry.memory_type == DEFAULT_MEMORY_TYPE

    def test_valid_type_preserved(self):
        entry = MemoryEntry(content="x", memory_type="user")
        assert entry.memory_type == "user"

    def test_roundtrip_dict(self):
        entry = MemoryEntry(content="x", memory_type="feedback")
        d = entry.to_dict()
        assert d["memory_type"] == "feedback"
        assert MemoryEntry.from_dict(d).memory_type == "feedback"


# === store / recall / list / count with memory_type ===


class TestMemoryEngineTypeFilter:
    def test_store_with_type(self, memory_engine):
        mid = memory_engine.store("user identity", memory_type="user")
        entry = memory_engine.get(mid)
        assert entry.memory_type == "user"

    def test_store_invalid_type_defaults(self, memory_engine):
        mid = memory_engine.store("data", memory_type="nope")
        assert memory_engine.get(mid).memory_type == "project"

    def test_recall_filter_by_type(self, memory_engine):
        memory_engine.store("i am a coder", memory_type="user")
        memory_engine.store("some project note", memory_type="project")
        hits = memory_engine.recall("coder", memory_type="user")
        assert len(hits) == 1
        assert hits[0].memory_type == "user"
        miss = memory_engine.recall("coder", memory_type="feedback")
        assert miss == []

    def test_list_recent_filter_by_type(self, memory_engine):
        memory_engine.store("a", memory_type="user")
        memory_engine.store("b", memory_type="feedback")
        only_user = memory_engine.list_recent(memory_type="user")
        assert all(e.memory_type == "user" for e in only_user)
        assert len(only_user) == 1

    def test_count_filter_by_type(self, memory_engine):
        memory_engine.store("a", memory_type="user")
        memory_engine.store("b", memory_type="user")
        memory_engine.store("c", memory_type="feedback")
        assert memory_engine.count(memory_type="user") == 2
        assert memory_engine.count(memory_type="feedback") == 1
        assert memory_engine.count() == 3

    def test_recall_relevant_filter_by_type(self, memory_engine):
        memory_engine.store("see the docs portal", memory_type="reference")
        memory_engine.store("project log entry", memory_type="project")
        ctx = memory_engine.recall_relevant("docs", memory_type="reference")
        assert "docs portal" in ctx
        ctx_empty = memory_engine.recall_relevant("docs", memory_type="user")
        assert ctx_empty == ""


# === schema migration: old db without memory_type column ===


class TestMemoryTypeMigration:
    def test_old_db_gets_column(self, tmp_path):
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, scope TEXT, "
            "tags TEXT, importance INTEGER, created_at REAL, metadata TEXT, "
            "is_summary INTEGER, tier TEXT)"
        )
        conn.execute(
            "INSERT INTO memories VALUES ('old1', 'legacy', 'default', '', 5, "
            "1700000000.0, '{}', 0, 'short_term')"
        )
        conn.commit()
        conn.close()

        eng = MemoryEngine(db_path=str(db_path))
        try:
            cur = eng.conn.cursor()
            cur.execute("SELECT memory_type FROM memories WHERE id='old1'")
            assert cur.fetchone()[0] == "project"
        finally:
            eng.close()


# === resolve_soul_prompt ===


class TestResolveSoulPrompt:
    def test_empty_agent_id_returns_fallback(self, tmp_agents_root):
        assert resolve_soul_prompt("", fallback="def") == "def"

    def test_missing_agent_returns_fallback(self, tmp_agents_root):
        assert resolve_soul_prompt("ghost", fallback="def") == "def"

    def test_loads_soul_md(self, tmp_agents_root):
        agent_dir = tmp_agents_root / "agentA"
        pkg = AgentPackage(agent_dir)
        pkg.init(soul="You are agent A.")
        assert resolve_soul_prompt("agentA") == "You are agent A."

    def test_soul_overrides_manifest(self, tmp_agents_root):
        agent_dir = tmp_agents_root / "agentB"
        pkg = AgentPackage(agent_dir)
        from agent_runtime.agent_package import AgentManifest

        pkg.init(
            manifest=AgentManifest(system_prompt="manifest prompt"),
            soul="soul wins",
        )
        assert resolve_soul_prompt("agentB") == "soul wins"

    def test_falls_back_to_manifest_when_no_soul(self, tmp_agents_root):
        agent_dir = tmp_agents_root / "agentC"
        pkg = AgentPackage(agent_dir)
        pkg.init()
        pkg.save_soul("")
        from agent_runtime.agent_package import AgentManifest

        pkg.save_manifest(AgentManifest(system_prompt="manifest only"))
        assert resolve_soul_prompt("agentC") == "manifest only"


# === chat_engine _inject_soul / _resolve_session_agent_id ===


class TestChatEngineSoul:
    @pytest.fixture
    def chat_engine(self, tmp_path):
        store = MagicMock()
        store.load_graph = MagicMock(return_value=None)
        return ChatEngine(store=store)

    def test_resolve_agent_id_from_metadata(self, chat_engine):
        session = ChatSession(metadata={"agent_id": "agentX"})
        assert chat_engine._resolve_session_agent_id(session) == "agentX"

    def test_resolve_agent_id_empty_no_metadata(self, chat_engine):
        session = ChatSession()
        assert chat_engine._resolve_session_agent_id(session) == ""

    def test_resolve_agent_id_from_graph_store(self, tmp_agents_root, tmp_path):
        store = MagicMock()
        store.load_graph = MagicMock(return_value={"agent_id": "agentY"})
        runtime = MagicMock()
        runtime.store = store
        engine = ChatEngine(runtime=runtime, store=store)
        session = ChatSession(graph_id="graph-1")
        assert engine._resolve_session_agent_id(session) == "agentY"

    def test_inject_soul_prepends_system(self, tmp_agents_root):
        agent_dir = tmp_agents_root / "agentS"
        AgentPackage(agent_dir).init(soul="SOUL PROMPT")
        store = MagicMock()
        store.load_graph = MagicMock(return_value=None)
        engine = ChatEngine(store=store)
        session = ChatSession(metadata={"agent_id": "agentS"})
        history = [{"role": "user", "content": "hi"}]
        out = engine._inject_soul(session, history)
        assert out[0]["role"] == "system"
        assert out[0]["content"] == "SOUL PROMPT"
        assert out[1]["role"] == "user"

    def test_inject_soul_merges_existing_system(self, tmp_agents_root):
        agent_dir = tmp_agents_root / "agentM"
        AgentPackage(agent_dir).init(soul="SOUL")
        store = MagicMock()
        store.load_graph = MagicMock(return_value=None)
        engine = ChatEngine(store=store)
        session = ChatSession(metadata={"agent_id": "agentM"})
        history = [{"role": "system", "content": "EXISTING"}]
        out = engine._inject_soul(session, history)
        assert out[0]["role"] == "system"
        assert "SOUL" in out[0]["content"]
        assert "EXISTING" in out[0]["content"]
        assert len(out) == 1

    def test_inject_soul_no_agent_id_passthrough(self):
        store = MagicMock()
        engine = ChatEngine(store=store)
        session = ChatSession()
        history = [{"role": "user", "content": "hi"}]
        out = engine._inject_soul(session, history)
        assert out == history


# === RPC dispatcher passthrough ===


class TestMemoryDispatcherTypePassthrough:
    @pytest.fixture
    def dispatcher(self, memory_engine):
        from agent_runtime.dispatchers.memory import MemoryDispatcher

        disp = MemoryDispatcher.__new__(MemoryDispatcher)
        disp._daemon = MagicMock()
        disp._daemon._get_memory = MagicMock(return_value=memory_engine)
        return disp

    @pytest.mark.asyncio
    async def test_store_passes_type(self, dispatcher, memory_engine):
        res = await dispatcher._handle_memory_store(
            {"content": "i am a dev", "memory_type": "user"}
        )
        mid = res["entry_id"]
        assert memory_engine.get(mid).memory_type == "user"

    @pytest.mark.asyncio
    async def test_recall_passes_type(self, dispatcher, memory_engine):
        memory_engine.store("i am a dev", memory_type="user")
        memory_engine.store("proj note", memory_type="project")
        res = await dispatcher._handle_memory_recall(
            {"query": "dev", "memory_type": "user"}
        )
        assert all(e["memory_type"] == "user" for e in res["entries"])

    @pytest.mark.asyncio
    async def test_count_passes_type(self, dispatcher, memory_engine):
        memory_engine.store("a", memory_type="feedback")
        res = await dispatcher._handle_memory_count({"memory_type": "feedback"})
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_recall_relevant_passes_type(self, dispatcher, memory_engine):
        memory_engine.store("see the docs portal", memory_type="reference")
        res = await dispatcher._handle_memory_recall_relevant(
            {"query": "docs", "memory_type": "reference"}
        )
        assert "docs portal" in res["context"]
