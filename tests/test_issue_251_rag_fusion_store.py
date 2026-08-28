"""Tests for #251 — RAG vector search migration to fusion-store HNSW.

Verifies KnowledgeEngine routes vector ANN through fusion_store.Store.search_knn
when a backend is present (injected or env-gated), FTS5 stays in SQLite, and RRF
fusion merges both. Default path (sqlite-vec / FTS5-only) unchanged.

Runner: pytest tests/test_issue_251_rag_fusion_store.py
"""

from __future__ import annotations

import pytest

from agent_runtime.knowledge_engine import KnowledgeEngine


def _fusion_store_ready() -> bool:
    try:
        import fusion_store  # noqa: F401
        import numpy  # noqa: F401

        return True
    except Exception:
        return False


_HAS_FS = _fusion_store_ready()


class _MockBackend:
    # Minimal backend matching _FusionStoreBackend surface for offline injection.
    def __init__(self, dim: int):
        self._dim = dim
        self._vecs: dict[int, list[float]] = {}
        self._next = 0
        self._checkpoints = 0

    def dim(self) -> int:
        return self._dim

    def insert(self, vec: list[float]) -> int:
        fs_id = self._next
        self._vecs[fs_id] = list(vec)
        self._next += 1
        return fs_id

    def search_knn(self, query: list[float], top_k: int):
        scored = []
        for fs_id, vec in self._vecs.items():
            dot = sum(a * b for a, b in zip(query, vec))
            scored.append((fs_id, -dot))
        scored.sort(key=lambda x: x[1])
        return [(fs_id, dist) for fs_id, dist in scored[:top_k]]

    def checkpoint(self):
        self._checkpoints += 1

    def close(self):
        self.checkpoint()


@pytest.fixture
def db_paths(tmp_path):
    return str(tmp_path / "k251.db"), str(tmp_path / "k251.fs")


@pytest.mark.skipif(not _HAS_FS, reason="fusion_store + numpy not installed")
class TestRealFusionStoreBackend:
    @pytest.fixture(autouse=True)
    def _enable_fs_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_RAG_BACKEND", "fusion_store")

    def test_ingest_and_vector_search_routes_through_store(self, db_paths):
        db, store_dir = db_paths
        eng = KnowledgeEngine(db_path=db, store_dir=store_dir)
        assert eng._fs_backend is not None
        a = eng.ingest("python programming guide", scope="docs")
        eng.ingest("rust systems language", scope="docs")
        eng.ingest("cooking pasta recipe", scope="kitchen")
        results = eng.search("python", mode="vector", limit=3)
        assert any(r.id == a.id for r in results)
        eng.close()

    def test_vector_search_scope_filter(self, db_paths):
        db, store_dir = db_paths
        eng = KnowledgeEngine(db_path=db, store_dir=store_dir)
        eng.ingest("alpha doc in docs", scope="docs")
        eng.ingest("alpha doc in kitchen", scope="kitchen")
        results = eng.search("alpha", scope="docs", mode="vector", limit=5)
        assert results
        assert all(r.scope == "docs" for r in results)
        eng.close()

    def test_hybrid_fuses_fts_and_vector(self, db_paths):
        db, store_dir = db_paths
        eng = KnowledgeEngine(db_path=db, store_dir=store_dir)
        for i in range(6):
            eng.ingest(f"hybrid corpus entry number {i}", scope="corp")
        results = eng.search("hybrid", mode="hybrid", limit=4)
        assert len(results) >= 1
        eng.close()

    def test_delete_drops_map_keeps_vector_orphan(self, db_paths):
        db, store_dir = db_paths
        eng = KnowledgeEngine(db_path=db, store_dir=store_dir)
        entry = eng.ingest("delete me please", scope="tmp")
        assert eng.delete(entry.id)
        assert eng.get(entry.id) is None
        # vector orphan remains in store (no per-vec delete API); search returns nothing
        # for the dropped str_id.
        results = eng.search("delete", mode="vector", limit=5)
        assert all(r.id != entry.id for r in results)
        eng.close()

    def test_close_checkpoint_called(self, db_paths):
        db, store_dir = db_paths
        eng = KnowledgeEngine(db_path=db, store_dir=store_dir)
        eng.ingest("persist this", scope="p")
        eng.close()
        # reopen same store — vector_count should reflect prior insert
        eng2 = KnowledgeEngine(db_path=db, store_dir=store_dir)
        assert eng2._fs_backend is not None
        results = eng2.search("persist", mode="vector", limit=5)
        assert len(results) >= 1
        eng2.close()


class TestMockBackendInjection:
    def test_mock_backend_replaces_vector_path(self, db_paths):
        db, _ = db_paths
        mock = _MockBackend(dim=8)
        eng = KnowledgeEngine(db_path=db, embedding_dim=8, vector_backend=mock)
        assert eng._fs_backend is mock
        eng.ingest("one", scope="s")
        eng.ingest("two", scope="s")
        results = eng.search("one", mode="vector", limit=2)
        assert len(results) >= 1
        eng.close()
        assert mock._checkpoints >= 1

    def test_default_path_unchanged_when_no_backend(self, db_paths):
        db, _ = db_paths
        eng = KnowledgeEngine(db_path=db)
        assert eng._fs_backend is None
        eng.ingest("plain sqlite path", scope="d")
        results = eng.search("plain", mode="fts", limit=5)
        assert len(results) >= 1
        eng.close()
