"""Knowledge Engine — SQLite-vec + FTS5 hybrid search with RRF fusion.

Local-first knowledge base: vector search via sqlite-vec (with FTS5-only
fallback), full-text search via FTS5, and Reciprocal Rank Fusion to merge
results.  Embedding is stubbed (random vectors) until a real model is loaded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import studio_home

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(studio_home() / "knowledge.db")
EMBEDDING_DIM = 64
RRF_K = 60

# #251 RAG -> fusion-store HNSW migration. Env gating:
# FUSION_RAG_BACKEND=fusion_store enables the fusion_store vector backend (HNSW),
# default empty -> keep sqlite-vec (production behavior unchanged). FTS5 full-text
# search always stays in SQLite (fusion-store §1.3 has no full-text). Vector results
# and FTS5 are fused via RRF.
DEFAULT_STORE_DIR = str(studio_home() / "knowledge.fs")


def _backend_from_env() -> str:
    # #251: read env at __init__ (not at module import), so tests can temporarily
    # setenv to switch backends.
    return os.environ.get("FUSION_RAG_BACKEND", "").strip().lower()


def _fusion_store_available() -> bool:
    try:
        import fusion_store  # noqa: F401

        return True
    except Exception:
        return False


# #251 fusion_store uses integer ids and does not support single-vector
# delete/update (DuplicateVector on reinsert). SQLite remains the SSOT for
# content + FTS5 + string ids. This class maintains a str_id -> int fs_id reverse
# map; the vector side is append-only. Re-ingesting the same str_id allocates a
# new fs_id (old vectors become orphans, logged only); delete drops the mapping
# (orphan vectors). Single-vector deletion is an upstream gap — issue filed,
# degraded here.
class _FusionStoreBackend:
    def __init__(self, store_dir: str, embedding_dim: int):
        import fusion_store
        import numpy as np

        self._np = np
        self._fs = fusion_store
        Path(store_dir).mkdir(parents=True, exist_ok=True)
        # dim=Some -> create/reuse a store of the same dim. Single writer: one
        # Store per process per path.
        # An existing store must be reopened with dim=None (spec §3.2: Some->create, None->reopen).
        store_path = os.path.join(store_dir, "vectors.fs")
        if os.path.exists(store_path):
            self._store = fusion_store.Store.open(store_path, dim=None)
        else:
            self._store = fusion_store.Store.open(store_path, dim=embedding_dim)
        self._dim = self._store.vector_dim()
        # fs_id monotonic counter persisted in store KV, never rewinds across restarts.
        raw = self._store.get_kv(b"next_fs_id")
        self._next_id = int(raw) if raw else 0
        logger.info(
            "FusionStoreBackend open: dir=%s dim=%d next_id=%d",
            store_dir,
            self._dim,
            self._next_id,
        )

    def dim(self) -> int:
        return self._dim

    def insert(self, vec: list[float]) -> int:
        fs_id = self._next_id
        arr = self._np.asarray(vec, dtype=self._np.float32)
        self._store.insert_vector(fs_id, arr)
        self._next_id = fs_id + 1
        self._store.put_kv(b"next_fs_id", str(self._next_id).encode())
        return fs_id

    def search_knn(self, query: list[float], top_k: int) -> list[tuple[int, float]]:
        arr = self._np.asarray(query, dtype=self._np.float32)
        ids, dists = self._store.search_knn(arr, top_k=top_k, timeout_ms=200)
        return list(zip(ids, dists))

    def checkpoint(self):
        self._store.checkpoint()

    def close(self):
        try:
            self.checkpoint()
        except Exception as e:
            logger.warning("FusionStoreBackend checkpoint on close failed: %s", e)
        # Release the heed LMDB env handle (spec: reopen after del). Without del,
        # reopening the same path in the same process raises Heed(EnvAlreadyOpened).
        self._store = None
        import gc

        gc.collect()


@dataclass
class KnowledgeEntry:
    id: str = ""
    content: str = ""
    scope: str = "default"
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "scope": self.scope,
            "embedding": self.embedding,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeEntry:
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            scope=data.get("scope", "default"),
            embedding=data.get("embedding", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )


def _stub_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    h = hash(text) & 0xFFFFFFFF
    rng = h
    vec = []
    for i in range(dim):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        vec.append(math.sin(rng / 1e6))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class KnowledgeEngine:
    """Hybrid vector + FTS5 search with RRF fusion over SQLite."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        embedding_dim: int = EMBEDDING_DIM,
        embedding_fn: Any | None = None,
        vector_backend: Any | None = None,
        store_dir: str = DEFAULT_STORE_DIR,
    ):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.embedding_fn = embedding_fn
        self._vec_available = False
        self._fs_backend = None
        # Audit A-6/3M-3: a single connection shared across threads without a lock
        # is still racy (WAL alone does not remove the race).
        # RLock serializes all connection operations (reentrant: _search_hybrid calls _search_fts/_search_vector).
        self._write_lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_db()
        self._init_schema()
        # #251: vector backend selection. Injection wins (test mocks), then env-gated
        # fusion_store. When fusion_store is enabled and available, it overrides the
        # sqlite-vec vector search path; _vec_available still tracks sqlite-vec status,
        # but search goes through _fs_backend.
        if vector_backend is not None:
            self._fs_backend = vector_backend
            self.embedding_dim = self._fs_backend.dim()
        elif _backend_from_env() == "fusion_store" and _fusion_store_available():
            try:
                self._fs_backend = _FusionStoreBackend(store_dir, self.embedding_dim)
                self.embedding_dim = self._fs_backend.dim()
            except Exception as e:
                logger.warning(
                    "FUSION_RAG_BACKEND=fusion_store but open failed: %s — fall back to sqlite-vec",
                    e,
                )
        logger.info(
            "KnowledgeEngine initialized: db=%s vec=%s fs_backend=%s embedding=%s",
            db_path,
            self._vec_available,
            "fusion_store" if self._fs_backend else "none",
            "real" if embedding_fn else "stub",
        )

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.enable_load_extension(True)
            import sqlite_vec

            conn.load_extension(sqlite_vec.loadable_path())
            self._vec_available = True
            logger.info("sqlite-vec extension loaded")
        except Exception as exc:
            self._vec_available = False
            logger.warning("sqlite-vec not available, falling back to FTS5-only: %s", exc)
        return conn

    def _init_schema(self):
        with self._write_lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_entries (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'default',
                    embedding BLOB,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_entries_scope ON knowledge_entries(scope)
            """)
            try:
                self._conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                        id UNINDEXED, content, scope,
                        content='knowledge_entries',
                        content_rowid='rowid'
                    )
                """)
            except sqlite3.OperationalError:
                logger.debug("FTS5 table already exists")
            # #251: str_id -> fs_id reverse map. gen records the ingest round
            # (re-ingesting the same str_id increments it).
            # On retrieval, take the fs_id with the max gen (latest vector); ignore
            # orphan vectors from older gens.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_vec_map (
                    str_id TEXT NOT NULL,
                    fs_id INTEGER NOT NULL,
                    gen INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (str_id, gen)
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vec_map_str ON knowledge_vec_map(str_id)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vec_map_fs ON knowledge_vec_map(fs_id)
            """)
            self._conn.commit()

    def ingest(
        self,
        content: str,
        scope: str = "default",
        metadata: dict | None = None,
        embedding: list[float] | None = None,
    ) -> KnowledgeEntry:
        if embedding is None:
            embedding = self._get_embedding(content)
        entry = KnowledgeEntry(
            content=content,
            scope=scope,
            embedding=embedding,
            metadata=metadata or {},
        )
        emb_blob = self._encode_embedding(entry.embedding)
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO knowledge_entries (id, content, scope, embedding, metadata, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.content,
                    entry.scope,
                    emb_blob,
                    json.dumps(entry.metadata),
                    entry.created_at,
                    entry.updated_at,
                ),
            )
            try:
                self._conn.execute(
                    "INSERT INTO knowledge_fts(rowid, id, content, scope) VALUES ((SELECT rowid FROM knowledge_entries WHERE id=?), ?, ?, ?)",
                    (entry.id, entry.id, entry.content, entry.scope),
                )
            except sqlite3.OperationalError:
                logger.debug("FTS insert skipped for %s", entry.id)
            # #251: fusion_store backend insert. Append-only integer fs_id + reverse map.
            # Re-ingest of the same str_id -> gen+1, new fs_id; old fs_id vectors are
            # orphans (no delete API).
            if self._fs_backend is not None:
                try:
                    fs_id = self._fs_backend.insert(entry.embedding)
                    self._conn.execute(
                        "INSERT INTO knowledge_vec_map (str_id, fs_id, gen) VALUES (?, ?, ?)",
                        (
                            entry.id,
                            fs_id,
                            0,
                        ),
                    )
                except Exception as e:
                    logger.warning("fusion_store insert failed for %s: %s", entry.id, e)
            self._conn.commit()
        logger.info("Ingested entry %s (scope=%s, %d chars)", entry.id, scope, len(content))
        return entry

    def delete(self, entry_id: str) -> bool:
        with self._write_lock:
            try:
                self._conn.execute("DELETE FROM knowledge_fts WHERE id=?", (entry_id,))
            except sqlite3.OperationalError:
                pass
            # #251: drop the str_id->fs_id mapping. fusion_store has no single-vector
            # delete; vectors become orphans (logged).
            if self._fs_backend is not None:
                self._conn.execute("DELETE FROM knowledge_vec_map WHERE str_id=?", (entry_id,))
            cursor = self._conn.execute("DELETE FROM knowledge_entries WHERE id=?", (entry_id,))
            self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted entry %s", entry_id)
        return deleted

    def search(
        self, query: str, scope: str = "", mode: str = "hybrid", limit: int = 10
    ) -> list[KnowledgeEntry]:
        if mode == "vector":
            return self._search_vector(query, scope, limit)
        if mode == "fts":
            return self._search_fts(query, scope, limit)
        return self._search_hybrid(query, scope, limit)

    def _search_fts(self, query: str, scope: str, limit: int) -> list[KnowledgeEntry]:
        sql = """
            SELECT ke.id, ke.content, ke.scope, ke.metadata, ke.created_at, ke.updated_at
            FROM knowledge_fts ft
            JOIN knowledge_entries ke ON ke.id = ft.id
            WHERE knowledge_fts MATCH ?
        """
        params: list[Any] = [query]
        if scope:
            sql += " AND ft.scope = ?"
            params.append(scope)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def _vector_enabled(self) -> bool:
        return self._vec_available or self._fs_backend is not None

    def _search_vector(self, query: str, scope: str, limit: int) -> list[KnowledgeEntry]:
        if not self._vector_enabled():
            logger.warning("Vector search unavailable, falling back to FTS5")
            return self._search_fts(query, scope, limit)
        q_emb = self._get_embedding(query)
        # #251: fusion_store backend path. HNSW knn -> fs_id -> str_id -> content
        # entries (scope filtered).
        if self._fs_backend is not None:
            return self._search_vector_fs(q_emb, scope, limit)
        q_blob = self._encode_embedding(q_emb)
        sql = """
            SELECT ke.id, ke.content, ke.scope, ke.metadata, ke.created_at, ke.updated_at,
                   vec_distance_cosine(ke.embedding, ?) AS distance
            FROM knowledge_entries ke
            WHERE ke.embedding IS NOT NULL
        """
        params: list[Any] = [q_blob]
        if scope:
            sql += " AND ke.scope = ?"
            params.append(scope)
        sql += " ORDER BY distance ASC LIMIT ?"
        params.append(limit)
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def _search_vector_fs(self, q_emb: list[float], scope: str, limit: int) -> list[KnowledgeEntry]:
        # Fetch more candidates than limit (trimmed after scope filtering), because knn does not filter scope.
        raw_k = limit * 4 if scope else limit
        hits = self._fs_backend.search_knn(q_emb, top_k=raw_k)
        if not hits:
            return []
        fs_ids = [fs_id for fs_id, _ in hits]
        # fs_id -> str_id (take each str_id's latest-gen fs_id, i.e. skip old-gen hits).
        placeholders = ",".join("?" for _ in fs_ids)
        sql = f"""
            SELECT m.str_id, m.fs_id, m.gen,
                   t.max_gen
            FROM knowledge_vec_map m
            JOIN (
                SELECT str_id, MAX(gen) AS max_gen FROM knowledge_vec_map GROUP BY str_id
            ) t ON t.str_id = m.str_id AND t.max_gen = m.gen
            WHERE m.fs_id IN ({placeholders})
        """
        with self._write_lock:
            rows = self._conn.execute(sql, fs_ids).fetchall()
        fs_to_str = {row[1]: row[0] for row in rows}
        # Take str_ids in knn-distance order, deduped (keep only the latest gen per str_id).
        seen = set()
        ordered_str_ids = []
        for fs_id, _ in hits:
            sid = fs_to_str.get(fs_id)
            if sid and sid not in seen:
                seen.add(sid)
                ordered_str_ids.append(sid)
        if not ordered_str_ids:
            return []
        str_placeholders = ",".join("?" for _ in ordered_str_ids)
        fetch_sql = f"""
            SELECT id, content, scope, metadata, created_at, updated_at
            FROM knowledge_entries
            WHERE id IN ({str_placeholders})
        """
        fetch_params: list[Any] = list(ordered_str_ids)
        if scope:
            fetch_sql += " AND scope=?"
            fetch_params.append(scope)
        with self._write_lock:
            frows = self._conn.execute(fetch_sql, fetch_params).fetchall()
        id_to_entry = {r[0]: self._row_to_entry(r) for r in frows}
        # Preserve knn order (hits order), keep only entries that were found.
        out = [id_to_entry[sid] for sid in ordered_str_ids if sid in id_to_entry]
        return out[:limit]

    def _search_hybrid(self, query: str, scope: str, limit: int) -> list[KnowledgeEntry]:
        fts_results = self._search_fts(query, scope, limit * 2)
        vec_results = self._search_vector(query, scope, limit * 2) if self._vector_enabled() else []
        fts_ranks = {r.id: i + 1 for i, r in enumerate(fts_results)}
        vec_ranks = {r.id: i + 1 for i, r in enumerate(vec_results)}
        all_ids = set(fts_ranks.keys()) | set(vec_ranks.keys())
        scored = []
        for eid in all_ids:
            fts_score = 1.0 / (RRF_K + fts_ranks.get(eid, limit * 2 + 1))
            vec_score = 1.0 / (RRF_K + vec_ranks.get(eid, limit * 2 + 1))
            scored.append((eid, fts_score + vec_score))
        scored.sort(key=lambda x: x[1], reverse=True)
        top_ids = [s[0] for s in scored[:limit]]
        id_to_entry = {r.id: r for r in fts_results + vec_results}
        return [id_to_entry[eid] for eid in top_ids if eid in id_to_entry]

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        with self._write_lock:
            row = self._conn.execute(
                "SELECT id, content, scope, metadata, created_at, updated_at FROM knowledge_entries WHERE id=?",
                (entry_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_entry(row)

    def list_entries(self, scope: str = "", limit: int = 100) -> list[KnowledgeEntry]:
        sql = "SELECT id, content, scope, metadata, created_at, updated_at FROM knowledge_entries"
        params: list[Any] = []
        if scope:
            sql += " WHERE scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count(self, scope: str = "") -> int:
        with self._write_lock:
            if scope:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM knowledge_entries WHERE scope=?", (scope,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()
        return row[0] if row else 0

    def _row_to_entry(self, row: tuple) -> KnowledgeEntry:
        return KnowledgeEntry(
            id=row[0],
            content=row[1],
            scope=row[2],
            metadata=json.loads(row[3]) if isinstance(row[3], str) else {},
            created_at=row[4],
            updated_at=row[5],
        )

    def _get_embedding(self, text: str) -> list[float]:
        if self.embedding_fn:
            try:
                result = self.embedding_fn(text)
                if isinstance(result, list) and len(result) > 0:
                    self.embedding_dim = len(result)
                    return result
                logger.warning("embedding_fn returned empty, falling back to stub")
            except Exception as e:
                logger.warning("embedding_fn failed: %s, falling back to stub", e)
        return _stub_embedding(text, self.embedding_dim)

    def _encode_embedding(self, vec: list[float]) -> bytes:
        import struct

        return struct.pack(f"{len(vec)}f", *vec)

    def _decode_embedding(self, blob: bytes) -> list[float]:
        import struct

        count = len(blob) // 4
        return list(struct.unpack(f"{count}f", blob))

    def close(self):
        # #251: checkpoint the fusion_store snapshot to disk before closing (crash recovery).
        if self._fs_backend is not None:
            try:
                self._fs_backend.close()
            except Exception as e:
                logger.warning("fusion_store backend close failed: %s", e)
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("KnowledgeEngine closed")
