"""Memory engine — persistent agent memory with FTS5 search and AutoGPT-style
hierarchical compression (ar2.md §2.5).

Uses SQLite + FTS5 for full-text search over agent memories.
Supports:
- Store: save a memory entry with metadata
- Recall: FTS5 search with relevance ranking
- Auto-summarize: compress old memories when threshold is reached
- Scoped memory: per-agent or shared namespace
- Tiered memory: short_term / long_term / archive with auto-promotion/demotion
- LLM-powered summarization via optional LLMGateway integration
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm_gateway import LLMGateway

logger = logging.getLogger(__name__)

_DEFAULT_DB = "memory.db"
_MAX_ENTRIES_BEFORE_SUMMARY = 200
_SUMMARY_BATCH_SIZE = 50
_TIME_WINDOW_SECONDS = 3600  # 1-hour chunks for compression grouping

# C16: 记忆语义类型分类 (对齐 Claude 记忆分类法).
# user=身份偏好, feedback=用户纠正/确认的工作方式, project=进行中工作目标约束,
# reference=外部资源指针. 默认 project (auto-store 多为执行上下文).
DEFAULT_MEMORY_TYPE = "project"
VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}

# C16: 自动分类启发式关键词 (lowercased 子串匹配). 命中即归类, 无命中 -> project.
# 顺序: user 优先于 feedback — "i prefer X" 是身份偏好(user)而非工作方式纠正(feedback),
# 避免裸 "prefer" 抢先命中 feedback. reference (URL/票据) 与前两者正交故置末.
_TYPE_KEYWORDS = (
    ("user", ("i am", "i'm", "my role", "i use", "i work", "i prefer", "expertise")),
    ("feedback", ("prefer", "don't", "always use", "never use", "should", "rule:", "why:", "how to apply")),
    ("reference", ("http://", "https://", "url:", "dashboard", "ticket", "doc at", "see ")),
)


def classify_memory_type(content: str) -> str:
    # C16: 按内容关键词启发式归类. 无命中默认 project.
    text = (content or "").lower()
    for mem_type, keywords in _TYPE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return mem_type
    return DEFAULT_MEMORY_TYPE


@dataclass
class MemoryTier:
    name: str = ""
    max_entries: int = 100
    max_age_hours: float = 0.0
    importance_threshold: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_entries": self.max_entries,
            "max_age_hours": self.max_age_hours,
            "importance_threshold": self.importance_threshold,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryTier:
        return cls(
            name=data.get("name", ""),
            max_entries=data.get("max_entries", 100),
            max_age_hours=data.get("max_age_hours", 0.0),
            importance_threshold=data.get("importance_threshold", 0),
        )


DEFAULT_TIERS: dict[str, MemoryTier] = {
    "short_term": MemoryTier(
        name="short_term",
        max_entries=50,
        max_age_hours=24.0,
        importance_threshold=7,
    ),
    "long_term": MemoryTier(
        name="long_term",
        max_entries=200,
        max_age_hours=168.0,  # 7 days
        importance_threshold=4,
    ),
    "archive": MemoryTier(
        name="archive",
        max_entries=1000,
        max_age_hours=0.0,  # no age limit
        importance_threshold=0,
    ),
}


@dataclass
class MemoryEntry:
    id: str = ""
    content: str = ""
    scope: str = "default"
    tags: str = ""
    importance: int = 5
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "short_term"
    # C16: 语义记忆类型 (user/feedback/project/reference).
    memory_type: str = DEFAULT_MEMORY_TYPE

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = time.time()
        if not self.memory_type or self.memory_type not in VALID_MEMORY_TYPES:
            self.memory_type = DEFAULT_MEMORY_TYPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "scope": self.scope,
            "tags": self.tags,
            "importance": self.importance,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "tier": self.tier,
            "memory_type": self.memory_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            scope=data.get("scope", "default"),
            tags=data.get("tags", ""),
            importance=data.get("importance", 5),
            created_at=data.get("created_at", 0.0),
            metadata=data.get("metadata", {}),
            tier=data.get("tier", "short_term"),
            memory_type=data.get("memory_type", DEFAULT_MEMORY_TYPE),
        )


class MemoryEngine:
    def __init__(
        self,
        db_path: str | Path | None = None,
        max_entries: int = _MAX_ENTRIES_BEFORE_SUMMARY,
        summary_batch: int = _SUMMARY_BATCH_SIZE,
        gateway: LLMGateway | None = None,
        tiers: dict[str, MemoryTier] | None = None,
    ):
        if db_path is None:
            db_path = _DEFAULT_DB
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.summary_batch = summary_batch
        self.gateway = gateway
        self.tiers = tiers if tiers is not None else dict(DEFAULT_TIERS)
        self._conn: sqlite3.Connection | None = None
        self._summarizing = False
        # Thread-safe writes: store/recall run via asyncio.to_thread, so the
        # connection is touched from worker threads. RLock serializes writes
        # (store -> _maybe_summarize re-entry) and the connection allows
        # cross-thread use; WAL lets reads stay concurrent.
        self._write_lock = threading.RLock()
        self._init_db()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'default',
                tags TEXT DEFAULT '',
                importance INTEGER DEFAULT 5,
                created_at REAL NOT NULL,
                metadata TEXT DEFAULT '{}',
                is_summary INTEGER DEFAULT 0,
                tier TEXT NOT NULL DEFAULT 'short_term',
                memory_type TEXT NOT NULL DEFAULT 'project'
            )
        """)
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content,
                tags,
                scope,
                content='memories',
                content_rowid='rowid'
            )
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags, scope)
                VALUES (new.rowid, new.content, new.tags, new.scope);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags, scope)
                VALUES ('delete', old.rowid, old.content, old.tags, old.scope);
            END
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier)
        """)
        self.conn.commit()
        self._migrate_add_tier_column(c)
        self._migrate_add_memory_type_column(c)
        self.conn.commit()
        # C16: memory_type 索引须在迁移加列之后建 — 老库无此列时先建索引会 OperationalError.
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type)
        """)
        self.conn.commit()
        logger.info("Memory engine initialized at %s", self.db_path)

    def _migrate_add_tier_column(self, c: sqlite3.Cursor) -> None:
        try:
            c.execute("SELECT tier FROM memories LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Migrating memories table: adding tier column")
            c.execute(
                "ALTER TABLE memories ADD COLUMN tier TEXT NOT NULL DEFAULT 'short_term'"
            )
            c.execute("""
                UPDATE memories SET tier = 'long_term'
                WHERE is_summary = 1 OR importance < 7
            """)

    def _migrate_add_memory_type_column(self, c: sqlite3.Cursor) -> None:
        # C16: 老库无 memory_type 列 -> 加列, 旧条目归 project 默认.
        try:
            c.execute("SELECT memory_type FROM memories LIMIT 1")
        except sqlite3.OperationalError:
            logger.info("Migrating memories table: adding memory_type column")
            c.execute(
                "ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'project'"
            )

    def _assign_tier(self, importance: int, is_summary: bool = False) -> str:
        if is_summary:
            if importance < 4:
                return "archive"
            return "long_term"
        if importance >= 7:
            return "short_term"
        if importance >= 4:
            return "long_term"
        return "archive"

    def store(
        self,
        content: str,
        scope: str = "default",
        tags: str = "",
        importance: int = 5,
        metadata: dict[str, Any] | None = None,
        tier: str = "",
        is_summary: bool = False,
        memory_type: str = "",
    ) -> str:
        tier = tier or self._assign_tier(importance, is_summary=is_summary)
        if not memory_type or memory_type not in VALID_MEMORY_TYPES:
            memory_type = DEFAULT_MEMORY_TYPE
        entry = MemoryEntry(
            content=content,
            scope=scope,
            tags=tags,
            importance=importance,
            metadata=metadata or {},
            tier=tier,
            memory_type=memory_type,
        )
        summary_flag = 1 if is_summary else 0
        with self._write_lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT INTO memories (id, content, scope, tags, importance, created_at, metadata, tier, is_summary, memory_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.content,
                    entry.scope,
                    entry.tags,
                    entry.importance,
                    entry.created_at,
                    json.dumps(entry.metadata, ensure_ascii=False),
                    entry.tier,
                    summary_flag,
                    entry.memory_type,
                ),
            )
            self.conn.commit()
            logger.debug(
                "Stored memory %s in scope '%s' tier '%s' type '%s' summary=%s",
                entry.id,
                scope,
                tier,
                entry.memory_type,
                is_summary,
            )

            self._maybe_summarize()
        return entry.id

    def recall(
        self,
        query: str,
        scope: str = "",
        limit: int = 10,
        min_importance: int = 0,
        tier: str = "",
        memory_type: str = "",
    ) -> list[MemoryEntry]:
        if not query.strip():
            return self.list_recent(
                scope=scope, limit=limit, tier=tier, memory_type=memory_type
            )

        c = self.conn.cursor()

        fts_query = query.replace('"', '""')

        conditions = []
        params: list[Any] = []

        conditions.append("memories_fts MATCH ?")
        params.append(fts_query)

        if scope:
            conditions.append("m.scope = ?")
            params.append(scope)

        conditions.append("m.importance >= ?")
        params.append(min_importance)

        if tier:
            conditions.append("m.tier = ?")
            params.append(tier)

        if memory_type:
            conditions.append("m.memory_type = ?")
            params.append(memory_type)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT m.* FROM memories m "
            f"JOIN memories_fts fts ON m.rowid = fts.rowid "
            f"WHERE {where} "
            f"ORDER BY rank LIMIT ?"
        )
        params.append(limit)
        c.execute(sql, params)

        return [self._row_to_entry(row) for row in c.fetchall()]

    def list_recent(
        self,
        scope: str = "",
        limit: int = 20,
        min_importance: int = 0,
        tier: str = "",
        memory_type: str = "",
    ) -> list[MemoryEntry]:
        c = self.conn.cursor()
        conditions = []
        params: list[Any] = []

        if scope:
            conditions.append("scope = ?")
            params.append(scope)

        conditions.append("importance >= ?")
        params.append(min_importance)

        if tier:
            conditions.append("tier = ?")
            params.append(tier)

        if memory_type:
            conditions.append("memory_type = ?")
            params.append(memory_type)

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        c.execute(sql, params)
        return [self._row_to_entry(row) for row in c.fetchall()]

    def get(self, entry_id: str) -> MemoryEntry | None:
        c = self.conn.cursor()
        c.execute("SELECT * FROM memories WHERE id = ?", (entry_id,))
        row = c.fetchone()
        return self._row_to_entry(row) if row else None

    def delete(self, entry_id: str) -> bool:
        c = self.conn.cursor()
        c.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
        self.conn.commit()
        deleted = c.rowcount > 0
        if deleted:
            logger.debug("Deleted memory %s", entry_id)
        return deleted

    def delete_scope(self, scope: str) -> int:
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,))
        count = c.fetchone()[0]
        c.execute("DELETE FROM memories WHERE scope = ?", (scope,))
        self.conn.commit()
        logger.info("Deleted %d memories in scope '%s'", count, scope)
        return count

    def count(self, scope: str = "", tier: str = "", memory_type: str = "") -> int:
        c = self.conn.cursor()
        conditions = []
        params: list[Any] = []
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if tier:
            conditions.append("tier = ?")
            params.append(tier)
        if memory_type:
            conditions.append("memory_type = ?")
            params.append(memory_type)
        if conditions:
            where = " AND ".join(conditions)
            sql = f"SELECT COUNT(*) FROM memories WHERE {where}"
            c.execute(sql, params)
        else:
            c.execute("SELECT COUNT(*) FROM memories")
        return c.fetchone()[0]

    def store_summary(self, summary: str, scope: str, original_count: int) -> str:
        return self.store(
            content=summary,
            scope=scope,
            tags="auto-summary",
            importance=3,
            metadata={"original_count": original_count, "type": "summary"},
            is_summary=True,
        )

    def compress_scope(self, scope: str, tier: str = "long_term") -> int:
        tier_config = self.tiers.get(tier)
        if not tier_config:
            logger.warning("Unknown tier '%s', skipping compress_scope", tier)
            return 0

        max_age_seconds = (
            tier_config.max_age_hours * 3600 if tier_config.max_age_hours > 0 else 0
        )
        if max_age_seconds <= 0:
            logger.debug("Tier '%s' has no age limit, nothing to compress", tier)
            return 0

        c = self.conn.cursor()
        age_cutoff = time.time() - max_age_seconds
        _importance_threshold = tier_config.importance_threshold

        c.execute(
            "SELECT id, content, importance, created_at, metadata FROM memories "
            "WHERE scope = ? AND tier = ? AND created_at < ? AND is_summary = 0 "
            "ORDER BY created_at ASC",
            (scope, tier, age_cutoff),
        )
        rows = c.fetchall()
        if len(rows) < 3:
            logger.debug(
                "Only %d candidates in scope '%s' tier '%s', too few to compress",
                len(rows),
                scope,
                tier,
            )
            return 0

        chunks = self._group_by_time_window(rows, _TIME_WINDOW_SECONDS)
        total_compressed = 0

        target_tier = "archive"
        if tier == "short_term":
            target_tier = "long_term"
        elif tier == "long_term":
            target_tier = "archive"

        for chunk_start, chunk_rows in chunks:
            contents = [row["content"] for row in chunk_rows]
            ids_to_delete = [row["id"] for row in chunk_rows]
            summary_text = self._generate_summary(contents, scope)

            self.store(
                content=summary_text,
                scope=scope,
                tags="auto-summary",
                importance=min(row["importance"] for row in chunk_rows),
                metadata={
                    "original_count": len(chunk_rows),
                    "type": "summary",
                    "time_window_start": chunk_start,
                    "source_tier": tier,
                },
                tier=target_tier,
                is_summary=True,
            )

            placeholders = ",".join("?" * len(ids_to_delete))
            c.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            total_compressed += len(ids_to_delete)
            logger.info(
                "Compressed %d memories in scope '%s' from tier '%s' to '%s'",
                len(ids_to_delete),
                scope,
                tier,
                target_tier,
            )

        self.conn.commit()
        return total_compressed

    def _group_by_time_window(
        self, rows: list[sqlite3.Row], window_seconds: float
    ) -> list[tuple[float, list[sqlite3.Row]]]:
        if not rows:
            return []

        chunks: list[tuple[float, list[sqlite3.Row]]] = []
        current_start = rows[0]["created_at"]
        current_chunk: list[sqlite3.Row] = [rows[0]]

        for row in rows[1:]:
            if row["created_at"] - current_start >= window_seconds:
                chunks.append((current_start, current_chunk))
                current_start = row["created_at"]
                current_chunk = [row]
            else:
                current_chunk.append(row)

        if current_chunk:
            chunks.append((current_start, current_chunk))

        return chunks

    def get_tier_stats(self) -> dict[str, Any]:
        c = self.conn.cursor()
        c.execute("SELECT tier, COUNT(*) as cnt FROM memories GROUP BY tier")
        tier_counts = {row["tier"]: row["cnt"] for row in c.fetchall()}

        c.execute("SELECT COUNT(*) FROM memories WHERE is_summary = 1")
        summary_count = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM memories")
        total = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM memories WHERE is_summary = 0")
        original_count = c.fetchone()[0]

        compression_ratio = 0.0
        if summary_count > 0 and original_count > 0:
            compression_ratio = summary_count / original_count

        stats: dict[str, Any] = {
            "total": total,
            "original_count": original_count,
            "summary_count": summary_count,
            "compression_ratio": round(compression_ratio, 3),
            "tiers": {},
        }
        for tier_name, tier_config in self.tiers.items():
            count = tier_counts.get(tier_name, 0)
            stats["tiers"][tier_name] = {
                "count": count,
                "max_entries": tier_config.max_entries,
                "max_age_hours": tier_config.max_age_hours,
                "importance_threshold": tier_config.importance_threshold,
            }

        return stats

    def _demote_old_entries(self) -> None:
        c = self.conn.cursor()
        now = time.time()

        for tier_name, tier_config in self.tiers.items():
            if tier_config.max_age_hours <= 0:
                continue
            max_age_seconds = tier_config.max_age_hours * 3600
            age_cutoff = now - max_age_seconds

            target_tier = ""
            if tier_name == "short_term":
                target_tier = "long_term"
            elif tier_name == "long_term":
                target_tier = "archive"
            else:
                continue

            c.execute(
                "SELECT id, importance FROM memories "
                "WHERE tier = ? AND created_at < ? AND is_summary = 0",
                (tier_name, age_cutoff),
            )
            rows = c.fetchall()
            if not rows:
                continue

            demoted = 0
            for row in rows:
                if row["importance"] < self.tiers[target_tier].importance_threshold:
                    c.execute(
                        "UPDATE memories SET tier = ? WHERE id = ?",
                        (target_tier, row["id"]),
                    )
                    demoted += 1

            if demoted > 0:
                self.conn.commit()
                logger.info(
                    "Demoted %d memories from tier '%s' to '%s'",
                    demoted,
                    tier_name,
                    target_tier,
                )

    def _maybe_summarize(self) -> None:
        if self._summarizing:
            return
        total = self.count()
        if total < self.max_entries:
            return

        self._summarizing = True
        try:
            self._demote_old_entries()

            c = self.conn.cursor()

            for tier_name in ("short_term", "long_term"):
                tier_config = self.tiers.get(tier_name)
                if not tier_config:
                    continue

                c.execute(
                    "SELECT COUNT(*) FROM memories WHERE tier = ? AND is_summary = 0",
                    (tier_name,),
                )
                tier_count = c.fetchone()[0]

                if tier_count <= tier_config.max_entries:
                    continue

                c.execute(
                    "SELECT scope FROM memories "
                    "WHERE tier = ? AND is_summary = 0 "
                    "GROUP BY scope HAVING COUNT(*) > ?",
                    (tier_name, self.summary_batch),
                )
                scopes_to_compress = [row[0] for row in c.fetchall()]

                for scope in scopes_to_compress:
                    c.execute(
                        "SELECT id, content, importance, created_at FROM memories "
                        "WHERE scope = ? AND tier = ? AND is_summary = 0 "
                        "AND importance < ? "
                        "ORDER BY created_at ASC LIMIT ?",
                        (
                            scope,
                            tier_name,
                            tier_config.importance_threshold,
                            self.summary_batch,
                        ),
                    )
                    rows = c.fetchall()
                    if len(rows) < 3:
                        continue

                    contents = [row[1] for row in rows]
                    ids_to_delete = [row[0] for row in rows]
                    summary_text = self._generate_summary(contents, scope)

                    target_tier = "archive" if tier_name == "long_term" else "long_term"
                    self.store(
                        content=summary_text,
                        scope=scope,
                        tags="auto-summary",
                        importance=min(row[2] for row in rows),
                        metadata={
                            "original_count": len(rows),
                            "type": "summary",
                            "source_tier": tier_name,
                        },
                        tier=target_tier,
                        is_summary=True,
                    )

                    placeholders = ",".join("?" * len(ids_to_delete))
                    c.execute(
                        f"DELETE FROM memories WHERE id IN ({placeholders})",
                        ids_to_delete,
                    )
                    self.conn.commit()
                    logger.info(
                        "Auto-summarized %d memories in scope '%s' from tier '%s'",
                        len(ids_to_delete),
                        scope,
                        tier_name,
                    )

            remaining = self.count()
            if remaining >= self.max_entries:
                self._emergency_compress()

        finally:
            self._summarizing = False

    def _emergency_compress(self) -> None:
        c = self.conn.cursor()
        c.execute(
            "SELECT scope FROM memories WHERE is_summary = 0 AND importance <= 3 "
            "GROUP BY scope HAVING COUNT(*) > 3"
        )
        scopes = [row[0] for row in c.fetchall()]

        for scope in scopes:
            c.execute(
                "SELECT id, content, importance, created_at FROM memories "
                "WHERE scope = ? AND is_summary = 0 AND importance <= 3 "
                "ORDER BY created_at ASC LIMIT ?",
                (scope, self.summary_batch),
            )
            rows = c.fetchall()
            if len(rows) < 3:
                continue

            contents = [row[1] for row in rows]
            ids_to_delete = [row[0] for row in rows]
            summary_text = self._generate_summary(contents, scope)

            self.store(
                content=summary_text,
                scope=scope,
                tags="auto-summary,emergency",
                importance=1,
                metadata={
                    "original_count": len(rows),
                    "type": "summary",
                    "emergency": True,
                },
                tier="archive",
                is_summary=True,
            )

            placeholders = ",".join("?" * len(ids_to_delete))
            c.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            self.conn.commit()
            logger.warning(
                "Emergency compressed %d low-importance memories in scope '%s'",
                len(ids_to_delete),
                scope,
            )

    def _generate_summary(self, contents: list[str], scope: str) -> str:
        if self.gateway is not None:
            return self._llm_summarize(contents, scope)

        combined = "\n---\n".join(contents)
        if len(combined) > 2000:
            combined = combined[:2000] + "..."
        return f"[Auto-summary of {len(contents)} memories in '{scope}']\n{combined}"

    def _llm_summarize(self, contents: list[str], scope: str) -> str:
        combined = "\n---\n".join(contents)
        if len(combined) > 6000:
            combined = combined[:6000] + "..."

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a memory compression assistant. Summarize the following "
                    "memory entries into a concise summary that preserves key facts, "
                    "decisions, and context. Do not add information that is not present. "
                    "Output only the summary, no meta-commentary."
                ),
            },
            {
                "role": "user",
                "content": f"Summarize these {len(contents)} memory entries from scope '{scope}':\n\n{combined}",
            },
        ]

        try:
            result = self.gateway.execute(
                messages=messages,
                capability="chat",
                max_tokens=512,
                temperature=0.3,
            )
            summary = result.get("content", "")
            if summary:
                logger.info(
                    "LLM summarized %d memories in scope '%s' (%d chars)",
                    len(contents),
                    scope,
                    len(summary),
                )
                return summary
        except Exception as exc:
            logger.warning(
                "LLM summarization failed for scope '%s': %s, falling back to stub",
                scope,
                exc,
            )

        combined = "\n---\n".join(contents)
        if len(combined) > 2000:
            combined = combined[:2000] + "..."
        return f"[Auto-summary of {len(contents)} memories in '{scope}']\n{combined}"

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        metadata = {}
        raw_meta = row["metadata"]
        if raw_meta:
            try:
                metadata = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        tier = row["tier"] if "tier" in row.keys() else "short_term"
        keys = row.keys()
        memory_type = row["memory_type"] if "memory_type" in keys else DEFAULT_MEMORY_TYPE
        return MemoryEntry(
            id=row["id"],
            content=row["content"],
            scope=row["scope"],
            tags=row["tags"],
            importance=row["importance"],
            created_at=row["created_at"],
            metadata=metadata,
            tier=tier,
            memory_type=memory_type,
        )

    def recall_relevant(
        self, query: str, limit: int = 5, scope: str = "", memory_type: str = ""
    ) -> str:
        entries = self.recall(
            query=query,
            scope=scope,
            limit=limit,
            min_importance=5,
            memory_type=memory_type,
        )
        if not entries:
            return ""
        parts = []
        for e in entries:
            ts = time.strftime("%Y-%m-%d", time.localtime(e.created_at))
            parts.append(f"[{ts}] {e.content}")
        return "\n".join(parts)

    def auto_forget(self, max_entries: int = 1000, min_importance: int = 3) -> int:
        total = self.count()
        if total <= max_entries:
            return 0
        c = self.conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM memories WHERE importance < ?",
            (min_importance,),
        )
        low_count = c.fetchone()[0]
        if low_count == 0:
            return 0
        c.execute(
            "DELETE FROM memories WHERE importance < ? ORDER BY created_at ASC LIMIT ?",
            (min_importance, low_count),
        )
        self.conn.commit()
        logger.info(
            "auto_forget: removed %d low-importance memories (was %d total)",
            c.rowcount,
            total,
        )
        return c.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
