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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.fusion-agent-studio/knowledge.db")
EMBEDDING_DIM = 64
RRF_K = 60


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
    ):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.embedding_fn = embedding_fn
        self._vec_available = False
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_db()
        self._init_schema()
        logger.info(
            "KnowledgeEngine initialized: db=%s vec=%s embedding=%s",
            db_path, self._vec_available, "real" if embedding_fn else "stub",
        )

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
        self._conn.commit()

    def ingest(self, content: str, scope: str = "default", metadata: dict | None = None, embedding: list[float] | None = None) -> KnowledgeEntry:
        if embedding is None:
            embedding = self._get_embedding(content)
        entry = KnowledgeEntry(
            content=content,
            scope=scope,
            embedding=embedding,
            metadata=metadata or {},
        )
        emb_blob = self._encode_embedding(entry.embedding)
        self._conn.execute(
            "INSERT OR REPLACE INTO knowledge_entries (id, content, scope, embedding, metadata, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (entry.id, entry.content, entry.scope, emb_blob, json.dumps(entry.metadata), entry.created_at, entry.updated_at),
        )
        try:
            self._conn.execute("INSERT INTO knowledge_fts(rowid, id, content, scope) VALUES ((SELECT rowid FROM knowledge_entries WHERE id=?), ?, ?, ?)",
                               (entry.id, entry.id, entry.content, entry.scope))
        except sqlite3.OperationalError:
            logger.debug("FTS insert skipped for %s", entry.id)
        self._conn.commit()
        logger.info("Ingested entry %s (scope=%s, %d chars)", entry.id, scope, len(content))
        return entry

    def delete(self, entry_id: str) -> bool:
        try:
            self._conn.execute("DELETE FROM knowledge_fts WHERE id=?", (entry_id,))
        except sqlite3.OperationalError:
            pass
        cursor = self._conn.execute("DELETE FROM knowledge_entries WHERE id=?", (entry_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Deleted entry %s", entry_id)
        return deleted

    def search(self, query: str, scope: str = "", mode: str = "hybrid", limit: int = 10) -> list[KnowledgeEntry]:
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
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def _search_vector(self, query: str, scope: str, limit: int) -> list[KnowledgeEntry]:
        if not self._vec_available:
            logger.warning("Vector search unavailable, falling back to FTS5")
            return self._search_fts(query, scope, limit)
        q_emb = self._get_embedding(query)
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
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def _search_hybrid(self, query: str, scope: str, limit: int) -> list[KnowledgeEntry]:
        fts_results = self._search_fts(query, scope, limit * 2)
        vec_results = self._search_vector(query, scope, limit * 2) if self._vec_available else []
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
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def count(self, scope: str = "") -> int:
        if scope:
            row = self._conn.execute("SELECT COUNT(*) FROM knowledge_entries WHERE scope=?", (scope,)).fetchone()
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
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("KnowledgeEngine closed")
