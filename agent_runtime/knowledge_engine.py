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

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.fusion-agent-studio/knowledge.db")
EMBEDDING_DIM = 64
RRF_K = 60

# #251 RAG → fusion-store HNSW 迁移. 环境门控:
# FUSION_RAG_BACKEND=fusion_store 启用 fusion_store 向量后端 (HNSW),
# 默认空 → 沿用 sqlite-vec (现网行为不变). FTS5 全文检索始终留在 SQLite
# (fusion-store §1.3 不做全文). 向量结果与 FTS5 经 RRF 融合.
DEFAULT_STORE_DIR = os.path.expanduser("~/.fusion-agent-studio/knowledge.fs")


def _backend_from_env() -> str:
    # #251: __init__ 时读 env (非模块导入时), 使测试可临时 setenv 切后端.
    return os.environ.get("FUSION_RAG_BACKEND", "").strip().lower()


def _fusion_store_available() -> bool:
    try:
        import fusion_store  # noqa: F401

        return True
    except Exception:
        return False


# #251 fusion_store 用整数 id 且不支持单向量删除/更新 (DuplicateVector on reinsert).
# SQLite 仍是内容 + FTS5 + 字符串 id 的 SSOT. 本类维护 str_id -> int fs_id 反向映射,
# 向量侧 append-only. 重新 ingest 同 str_id 分配新 fs_id (旧向量孤儿, 仅记日志),
# delete 丢弃映射 (向量孤儿). 单向量删除是上游缺失 — 已提 issue, 此处降级.
class _FusionStoreBackend:
    def __init__(self, store_dir: str, embedding_dim: int):
        import fusion_store
        import numpy as np

        self._np = np
        self._fs = fusion_store
        Path(store_dir).mkdir(parents=True, exist_ok=True)
        # dim=Some → 新建/复用同维库. 单写者: 每进程一路径一 Store.
        # 已存在的 store 须 dim=None reopen (spec §3.2: Some→create, None→reopen).
        store_path = os.path.join(store_dir, "vectors.fs")
        if os.path.exists(store_path):
            self._store = fusion_store.Store.open(store_path, dim=None)
        else:
            self._store = fusion_store.Store.open(store_path, dim=embedding_dim)
        self._dim = self._store.vector_dim()
        # fs_id 单调计数器持久化在 store KV, 跨重启不回卷.
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
        # 释放 heed LMDB 环境句柄 (spec: reopen after del). 不 del 则同进程同路径
        # 重开抛 Heed(EnvAlreadyOpened).
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
        # 审计 A-6/3M-3: 跨线程共享单连接无锁 (仅 WAL 无锁仍竞态).
        # RLock 串行化所有连接操作 (reentrant: _search_hybrid 调 _search_fts/_search_vector).
        self._write_lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_db()
        self._init_schema()
        # #251: 向量后端选择. 注入优先 (测试 mock), 其次 env 门控 fusion_store.
        # 若 fusion_store 启用且可用, 覆盖 sqlite-vec 的向量检索路径; _vec_available 仍记
        # sqlite-vec 状态, 但检索经 _fs_backend 走.
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
            logger.warning(
                "sqlite-vec not available, falling back to FTS5-only: %s", exc
            )
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
            # #251: str_id -> fs_id 反向映射. gen 记第几次 ingest (同一 str_id 重 ingest 加 1).
            # 检索命中后取最大 gen 的 fs_id (最新向量), 旧 gen 孤儿向量忽略.
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
            # #251: fusion_store 后端插入. append-only 整数 fs_id + 反向映射.
            # 同 str_id 重 ingest → gen+1, 新 fs_id; 旧 fs_id 向量孤儿 (无删除 API).
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
        logger.info(
            "Ingested entry %s (scope=%s, %d chars)", entry.id, scope, len(content)
        )
        return entry

    def delete(self, entry_id: str) -> bool:
        with self._write_lock:
            try:
                self._conn.execute("DELETE FROM knowledge_fts WHERE id=?", (entry_id,))
            except sqlite3.OperationalError:
                pass
            # #251: 丢弃 str_id->fs_id 映射. fusion_store 无单向量删除, 向量孤儿 (记日志).
            if self._fs_backend is not None:
                self._conn.execute(
                    "DELETE FROM knowledge_vec_map WHERE str_id=?", (entry_id,)
                )
            cursor = self._conn.execute(
                "DELETE FROM knowledge_entries WHERE id=?", (entry_id,)
            )
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

    def _search_vector(
        self, query: str, scope: str, limit: int
    ) -> list[KnowledgeEntry]:
        if not self._vector_enabled():
            logger.warning("Vector search unavailable, falling back to FTS5")
            return self._search_fts(query, scope, limit)
        q_emb = self._get_embedding(query)
        # #251: fusion_store 后端路径. HNSW knn → fs_id → str_id → 内容条目 (scope 过滤).
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

    def _search_vector_fs(
        self, q_emb: list[float], scope: str, limit: int
    ) -> list[KnowledgeEntry]:
        # 拉比 limit 多的候选 (scope 过滤后裁剪), 因为 knn 不过滤 scope.
        raw_k = limit * 4 if scope else limit
        hits = self._fs_backend.search_knn(q_emb, top_k=raw_k)
        if not hits:
            return []
        fs_ids = [fs_id for fs_id, _ in hits]
        # fs_id -> str_id (取每 str_id 最新 gen 的 fs_id, 即若旧 gen 命中则跳过).
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
        # 按 knn 距离排序取 str_id, 去重 (同 str_id 多 gen 只留最新).
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
        # 保持 knn 排序 (hits 顺序), 仅保留命中条目.
        out = [id_to_entry[sid] for sid in ordered_str_ids if sid in id_to_entry]
        return out[:limit]

    def _search_hybrid(
        self, query: str, scope: str, limit: int
    ) -> list[KnowledgeEntry]:
        fts_results = self._search_fts(query, scope, limit * 2)
        vec_results = (
            self._search_vector(query, scope, limit * 2)
            if self._vector_enabled()
            else []
        )
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
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM knowledge_entries"
                ).fetchone()
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
        # #251: fusion_store 关闭前 checkpoint 落盘快照 (崩溃恢复).
        if self._fs_backend is not None:
            try:
                self._fs_backend.close()
            except Exception as e:
                logger.warning("fusion_store backend close failed: %s", e)
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("KnowledgeEngine closed")
