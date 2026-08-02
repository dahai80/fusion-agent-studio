"""Tests for MemoryEngine — persistent memory with FTS5 search."""

import pytest
import tempfile
import time
import uuid
from pathlib import Path

from agent_runtime.memory_engine import MemoryEngine, MemoryEntry, MemoryTier


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def engine(tmp_dir):
    db_path = Path(tmp_dir) / "test_memory.db"
    eng = MemoryEngine(db_path=str(db_path))
    yield eng
    eng.close()


class TestMemoryEntry:
    def test_auto_id(self):
        entry = MemoryEntry(content="test")
        assert entry.id
        assert len(entry.id) == 16

    def test_auto_timestamp(self):
        entry = MemoryEntry(content="test")
        assert entry.created_at > 0

    def test_custom_values(self):
        entry = MemoryEntry(
            id="custom-id",
            content="test",
            scope="custom",
            tags="tag1,tag2",
            importance=8,
        )
        assert entry.id == "custom-id"
        assert entry.scope == "custom"
        assert entry.importance == 8


class TestMemoryEngineStore:
    def test_store_returns_id(self, engine):
        mid = engine.store("Test memory content")
        assert mid
        assert len(mid) == 16

    def test_store_with_scope(self, engine):
        mid = engine.store("Scoped memory", scope="user-profile")
        entry = engine.get(mid)
        assert entry is not None
        assert entry.scope == "user-profile"

    def test_store_with_tags(self, engine):
        mid = engine.store("Tagged memory", tags="important,urgent")
        entry = engine.get(mid)
        assert "important" in entry.tags

    def test_store_with_importance(self, engine):
        mid = engine.store("Important memory", importance=9)
        entry = engine.get(mid)
        assert entry.importance == 9

    def test_store_with_metadata(self, engine):
        mid = engine.store("Meta memory", metadata={"source": "test", "version": 1})
        entry = engine.get(mid)
        assert entry.metadata["source"] == "test"


class TestMemoryEngineRecall:
    def test_recall_by_keyword(self, engine):
        engine.store("Python is a programming language", scope="tech")
        engine.store("Rust is a systems language", scope="tech")
        engine.store("Today is sunny", scope="weather")

        results = engine.recall("Python")
        assert len(results) >= 1
        assert any("Python" in r.content for r in results)

    def test_recall_with_scope(self, engine):
        engine.store("Python is great", scope="tech")
        engine.store("Python the snake", scope="animals")

        results = engine.recall("Python", scope="tech")
        assert all(r.scope == "tech" for r in results)

    def test_recall_empty_query(self, engine):
        engine.store("Some memory")
        results = engine.recall("")
        assert len(results) >= 1

    def test_recall_no_results(self, engine):
        results = engine.recall("nonexistent_xyz_12345")
        assert len(results) == 0

    def test_recall_min_importance(self, engine):
        engine.store("Low importance", importance=2)
        engine.store("High importance", importance=9)

        results = engine.recall("importance", min_importance=5)
        assert all(r.importance >= 5 for r in results)


class TestMemoryEngineListRecent:
    def test_list_recent(self, engine):
        for i in range(5):
            engine.store(f"Memory {i}")
        results = engine.list_recent(limit=3)
        assert len(results) == 3

    def test_list_recent_by_scope(self, engine):
        engine.store("Tech memory", scope="tech")
        engine.store("Bio memory", scope="bio")
        results = engine.list_recent(scope="tech")
        assert all(r.scope == "tech" for r in results)


class TestMemoryEngineGet:
    def test_get_existing(self, engine):
        mid = engine.store("Gettable memory")
        entry = engine.get(mid)
        assert entry is not None
        assert entry.content == "Gettable memory"

    def test_get_nonexistent(self, engine):
        entry = engine.get("nonexistent_id")
        assert entry is None


class TestMemoryEngineDelete:
    def test_delete_existing(self, engine):
        mid = engine.store("Deletable memory")
        assert engine.delete(mid)
        assert engine.get(mid) is None

    def test_delete_nonexistent(self, engine):
        assert not engine.delete("nonexistent_id")

    def test_delete_scope(self, engine):
        engine.store("Memory 1", scope="temp")
        engine.store("Memory 2", scope="temp")
        engine.store("Keep this", scope="permanent")
        count = engine.delete_scope("temp")
        assert count == 2
        assert engine.count("temp") == 0
        assert engine.count("permanent") == 1


class TestMemoryEngineCount:
    def test_count_all(self, engine):
        engine.store("A")
        engine.store("B")
        engine.store("C")
        assert engine.count() == 3

    def test_count_by_scope(self, engine):
        engine.store("X", scope="scope1")
        engine.store("Y", scope="scope2")
        assert engine.count("scope1") == 1


class TestMemoryEngineSummary:
    def test_store_summary(self, engine):
        sid = engine.store_summary("Summary of 10 memories", "test-scope", 10)
        entry = engine.get(sid)
        assert entry is not None
        assert "auto-summary" in entry.tags
        assert entry.metadata.get("type") == "summary"
        assert entry.metadata.get("original_count") == 10


class TestMemoryEngineAutoSummarize:
    def test_auto_summarize_triggers(self, tmp_dir):
        db_path = Path(tmp_dir) / "auto_summarize.db"
        eng = MemoryEngine(db_path=str(db_path), max_entries=10, summary_batch=5)
        for i in range(15):
            eng.store(
                f"Memory entry {i} with some content to make it longer",
                scope="auto-test",
                importance=3,
            )
        count = eng.count("auto-test")
        assert count < 15
        eng.close()


class TestMemoryTier:
    def test_to_dict(self):
        tier = MemoryTier(
            name="test", max_entries=50, max_age_hours=12.0, importance_threshold=5
        )
        d = tier.to_dict()
        assert d["name"] == "test"
        assert d["max_entries"] == 50
        assert d["max_age_hours"] == 12.0
        assert d["importance_threshold"] == 5

    def test_from_dict(self):
        data = {
            "name": "custom",
            "max_entries": 99,
            "max_age_hours": 48.0,
            "importance_threshold": 3,
        }
        tier = MemoryTier.from_dict(data)
        assert tier.name == "custom"
        assert tier.max_entries == 99
        assert tier.max_age_hours == 48.0
        assert tier.importance_threshold == 3


class TestMemoryEntryTier:
    def test_entry_default_tier(self):
        entry = MemoryEntry(content="test")
        assert entry.tier == "short_term"

    def test_entry_custom_tier(self):
        entry = MemoryEntry(content="test", tier="archive")
        assert entry.tier == "archive"

    def test_entry_to_dict_includes_tier(self):
        entry = MemoryEntry(content="test", tier="long_term")
        d = entry.to_dict()
        assert d["tier"] == "long_term"

    def test_entry_from_dict_with_tier(self):
        data = {"content": "test", "tier": "archive"}
        entry = MemoryEntry.from_dict(data)
        assert entry.tier == "archive"


class TestTierAssignment:
    def test_high_importance_gets_short_term(self, engine):
        mid = engine.store("Important", importance=9)
        entry = engine.get(mid)
        assert entry.tier == "short_term"

    def test_medium_importance_gets_long_term(self, engine):
        mid = engine.store("Medium", importance=5)
        entry = engine.get(mid)
        assert entry.tier == "long_term"

    def test_low_importance_gets_archive(self, engine):
        mid = engine.store("Low", importance=1)
        entry = engine.get(mid)
        assert entry.tier == "archive"

    def test_explicit_tier_override(self, engine):
        mid = engine.store("High but archive", importance=9, tier="archive")
        entry = engine.get(mid)
        assert entry.tier == "archive"


class TestTierStats:
    def test_get_tier_stats_empty(self, engine):
        stats = engine.get_tier_stats()
        assert stats["total"] == 0
        assert stats["original_count"] == 0
        assert stats["summary_count"] == 0
        assert stats["compression_ratio"] == 0.0
        assert "short_term" in stats["tiers"]
        assert "long_term" in stats["tiers"]
        assert "archive" in stats["tiers"]

    def test_get_tier_stats_with_data(self, engine):
        engine.store("High", importance=9)
        engine.store("Medium", importance=5)
        engine.store("Low", importance=1)
        stats = engine.get_tier_stats()
        assert stats["total"] == 3
        assert stats["original_count"] == 3
        assert stats["tiers"]["short_term"]["count"] == 1
        assert stats["tiers"]["long_term"]["count"] == 1
        assert stats["tiers"]["archive"]["count"] == 1


class TestCompressScope:
    def test_compress_scope_no_old_entries(self, tmp_dir):
        db_path = Path(tmp_dir) / "compress.db"
        tiers = {
            "short_term": MemoryTier(
                name="short_term",
                max_entries=50,
                max_age_hours=0.001,
                importance_threshold=7,
            ),
            "long_term": MemoryTier(
                name="long_term",
                max_entries=200,
                max_age_hours=168.0,
                importance_threshold=4,
            ),
            "archive": MemoryTier(
                name="archive",
                max_entries=1000,
                max_age_hours=0.0,
                importance_threshold=0,
            ),
        }
        eng = MemoryEngine(db_path=str(db_path), tiers=tiers)
        for i in range(10):
            eng.store(f"Recent memory {i}", scope="test", importance=8)
        compressed = eng.compress_scope("test", tier="short_term")
        assert compressed == 0
        eng.close()

    def test_compress_scope_with_old_entries(self, tmp_dir):
        db_path = Path(tmp_dir) / "compress_old.db"
        tiers = {
            "short_term": MemoryTier(
                name="short_term",
                max_entries=50,
                max_age_hours=0.00001,
                importance_threshold=7,
            ),
            "long_term": MemoryTier(
                name="long_term",
                max_entries=200,
                max_age_hours=168.0,
                importance_threshold=4,
            ),
            "archive": MemoryTier(
                name="archive",
                max_entries=1000,
                max_age_hours=0.0,
                importance_threshold=0,
            ),
        }
        eng = MemoryEngine(db_path=str(db_path), tiers=tiers)
        c = eng.conn.cursor()
        old_time = time.time() - 3600
        for i in range(10):
            entry_id = uuid.uuid4().hex[:16]
            c.execute(
                "INSERT INTO memories (id, content, scope, tags, importance, created_at, metadata, tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    f"Old memory {i}",
                    "test",
                    "",
                    8,
                    old_time + i,
                    "{}",
                    "short_term",
                ),
            )
        eng.conn.commit()

        time.sleep(0.1)

        compressed = eng.compress_scope("test", tier="short_term")
        assert compressed > 0

        stats = eng.get_tier_stats()
        assert stats["summary_count"] >= 1
        eng.close()

    def test_compress_scope_unknown_tier(self, engine):
        result = engine.compress_scope("default", tier="nonexistent")
        assert result == 0


class TestCountByTier:
    def test_count_with_tier_filter(self, engine):
        engine.store("High", importance=9)
        engine.store("Medium", importance=5)
        engine.store("Low", importance=1)

        assert engine.count(tier="short_term") == 1
        assert engine.count(tier="long_term") == 1
        assert engine.count(tier="archive") == 1


class TestRecallWithTier:
    def test_recall_with_tier_filter(self, engine):
        engine.store("Important stuff about Python", importance=9, scope="tech")
        engine.store("Medium Python note", importance=5, scope="tech")

        results = engine.recall("Python", tier="short_term")
        assert all(r.tier == "short_term" for r in results)

    def test_list_recent_with_tier(self, engine):
        engine.store("High importance", importance=9)
        engine.store("Low importance", importance=1)

        results = engine.list_recent(tier="short_term")
        assert all(r.tier == "short_term" for r in results)


class TestLLMGatewayIntegration:
    def test_gateway_none_uses_stub(self, engine):
        _mid = engine.store("Test memory for summary", importance=3)
        result = engine._generate_summary(["entry1", "entry2", "entry3"], "test")
        assert "[Auto-summary of 3 memories in 'test']" in result

    def test_gateway_present_but_fails_gracefully(self, tmp_dir):
        from unittest.mock import MagicMock

        db_path = Path(tmp_dir) / "gateway_fail.db"
        gateway = MagicMock()
        gateway.execute.side_effect = RuntimeError("Model unavailable")

        eng = MemoryEngine(db_path=str(db_path), gateway=gateway)
        result = eng._generate_summary(["entry1", "entry2"], "test")
        assert "[Auto-summary of 2 memories in 'test']" in result
        eng.close()

    def test_gateway_present_and_succeeds(self, tmp_dir):
        from unittest.mock import MagicMock

        db_path = Path(tmp_dir) / "gateway_ok.db"
        gateway = MagicMock()
        gateway.execute.return_value = {
            "content": "Summarized: entry1 and entry2 were processed.",
            "model": "test-model",
        }

        eng = MemoryEngine(db_path=str(db_path), gateway=gateway)
        result = eng._generate_summary(["entry1", "entry2"], "test")
        assert "Summarized" in result
        assert "[Auto-summary" not in result
        eng.close()
