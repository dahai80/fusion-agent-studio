"""Agent state persistence — save and restore agent execution state using SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chat_engine import ChatSession
from .context import AgentContext
from .graph import AgentGraph

logger = logging.getLogger(__name__)



@dataclass
class Checkpoint:
    """A checkpoint of agent execution state for resume capability."""

    session_id: str
    graph_id: str
    context_json: str
    current_node_id: str
    iteration_count: int
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "graph_id": self.graph_id,
            "context_json": self.context_json,
            "current_node_id": self.current_node_id,
            "iteration_count": self.iteration_count,
            "created_at": self.created_at,
        }


class AgentStore:
    """SQLite-backed persistence for agent graphs, sessions, and checkpoints."""

    def __init__(self, db_path: str | Path = ""):
        if not db_path:
            db_path = Path.home() / ".fusion-agent-studio" / "store.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        # 审计 A-6/R-7/3M-3: store/trigger/task 写经 asyncio.to_thread 跨线程共享单连接,
        # 无锁无 WAL 致写竞态腐. RLock 串行化所有连接操作, WAL 让读不堵写, busy_timeout
        # 等锁而非立即 SQLITE_BUSY 失败.
        self._write_lock = threading.RLock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    @contextmanager
    def _cursor(self):
        """Context manager for safe SQLite operations with auto-commit. Lock-serialized."""
        conn = self._get_conn()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS graphs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                data TEXT NOT NULL,
                version TEXT DEFAULT '1.0',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                graph_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                status TEXT DEFAULT 'created',
                created_at REAL NOT NULL,
                finished_at REAL,
                FOREIGN KEY (graph_id) REFERENCES graphs(id)
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                context_json TEXT NOT NULL,
                current_node_id TEXT NOT NULL,
                iteration_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON checkpoints(session_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                mode TEXT DEFAULT 'simple',
                messages_json TEXT DEFAULT '[]',
                active_branch TEXT DEFAULT '',
                graph_id TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                data TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_workflow_runs_wf
                ON workflow_runs(workflow_id, created_at DESC);
        """)
        conn.commit()
        # 审计 E-20: checkpoints 缺 graph_id/state_json 列, save/load 签名不匹配致
        # 写静默失败 + resume 报 "No checkpoint found". 补列 (IF NOT EXISTS 模式不
        # 改老表, 需 ALTER). 用 PRAGMA table_info 探列存在再 ALTER, 避重复报错.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")}
        if "graph_id" not in cols:
            conn.execute("ALTER TABLE checkpoints ADD COLUMN graph_id TEXT DEFAULT ''")
        if "state_json" not in cols:
            conn.execute("ALTER TABLE checkpoints ADD COLUMN state_json TEXT DEFAULT '{}'")
        conn.commit()

    # ── Graph CRUD ──

    def save_graph(self, graph: AgentGraph) -> None:
        """Save or update an agent graph."""
        now = time.time()
        with self._cursor() as conn:
            conn.execute(
                """INSERT INTO graphs (id, name, description, data, version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       description=excluded.description,
                       data=excluded.data,
                       version=excluded.version,
                       updated_at=excluded.updated_at""",
                (
                    graph.id,
                    graph.name,
                    graph.description,
                    graph.to_json(),
                    graph.version,
                    now,
                    now,
                ),
            )

    def load_graph(self, graph_id: str) -> AgentGraph | None:
        """Load an agent graph by ID."""
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT data FROM graphs WHERE id = ?", (graph_id,)
            ).fetchone()
        if row is None:
            return None
        return AgentGraph.from_json(row["data"])

    def list_graphs(self) -> list[dict[str, Any]]:
        """List all saved graphs with metadata + node_count."""
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, name, description, data, version, created_at, updated_at FROM graphs ORDER BY updated_at DESC"
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                graph_data = json.loads(d.pop("data", "{}"))
                d["node_count"] = len(graph_data.get("nodes", {}))
                d["edge_count"] = len(graph_data.get("edges", []))
            except (json.JSONDecodeError, TypeError):
                d["node_count"] = 0
                d["edge_count"] = 0
            results.append(d)
        return results

    def delete_graph(self, graph_id: str) -> bool:
        """Delete a graph by ID. Returns True if deleted."""
        with self._cursor() as conn:
            cursor = conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))
        return cursor.rowcount > 0

    def delete_graphs_by_names(self, names: list[str]) -> int:
        # 批量删除指定名称的 graph（用于清理测试残留）。
        # names: 要删除的 graph 名称列表（精确匹配）; 返回实际删除的条数。
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        with self._cursor() as conn:
            cursor = conn.execute(
                f"DELETE FROM graphs WHERE name IN ({placeholders})",
                names,
            )
        deleted = cursor.rowcount
        logger.info("delete_graphs_by_names: deleted %d graphs (names=%s)", deleted, names)
        return deleted

    def delete_graphs_by_name_prefix(self, prefix: str) -> int:
        # 删除名称以指定前缀开头的 graph（如 'e2e-' 前缀清理 e2e 测试残留）。
        # prefix: 名称前缀; 返回实际删除的条数。
        if not prefix:
            return 0
        pattern = f"{prefix}%"
        with self._cursor() as conn:
            cursor = conn.execute(
                "DELETE FROM graphs WHERE name LIKE ?",
                (pattern,),
            )
        deleted = cursor.rowcount
        logger.info("delete_graphs_by_name_prefix: deleted %d graphs (prefix=%s)", deleted, prefix)
        return deleted

    # ── Session Management ──

    def create_session(self, session_id: str, graph_id: str, name: str = "") -> None:
        """Create a new execution session."""
        with self._cursor() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, graph_id, name, status, created_at) VALUES (?, ?, ?, 'created', ?)",
                (session_id, graph_id, name, time.time()),
            )

    def update_session_status(self, session_id: str, status: str) -> None:
        """Update session status ('running', 'completed', 'failed')."""
        now = time.time()
        finished_at = now if status in ("completed", "failed") else None
        with self._cursor() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, finished_at = ? WHERE session_id = ?",
                (status, finished_at, session_id),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get session metadata."""
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent sessions."""
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Checkpoint Management ──

    def save_checkpoint(
        self,
        graph_id: str = "",
        session_id: str = "",
        node_id: str = "",
        state: dict | None = None,
    ) -> int:
        # 审计 E-20: 签名对齐 runtime._save_checkpoint 调用 (graph_id/session_id/
        # node_id/state). state 含 messages/iteration_count/variables/工具链计数.
        # context_json 保留兼容老读路径, 写 state_json 为 resume 真真源.
        # 兼容老签名 save_checkpoint(session_id, context, current_node_id):
        # ctx 落到 session_id 形参槽, 检测后回填. state 取 ctx.to_dict()
        # 保 from_dict 完整往返 (session_id/agent_id/metadata 等).
        if isinstance(session_id, AgentContext):
            ctx = session_id
            session_id = ctx.session_id
            state = ctx.to_dict()
            node_id = node_id or ctx.current_node_id or ""
            graph_id = graph_id or ""
        state = state or {}
        iteration = int(state.get("iteration_count", 0))
        with self._cursor() as conn:
            cursor = conn.execute(
                """INSERT INTO checkpoints
                   (session_id, graph_id, context_json, state_json,
                    current_node_id, iteration_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    graph_id,
                    json.dumps(state),
                    json.dumps(state),
                    node_id,
                    iteration,
                    time.time(),
                ),
            )
        return cursor.lastrowid or 0

    def load_latest_checkpoint(
        self, session_id: str = "", graph_id: str = ""
    ) -> Checkpoint | None:
        # 审计 E-20: 支持 graph_id 过滤; 老表无 graph_id 列时退化为 session-only.
        with self._cursor() as conn:
            if graph_id:
                row = conn.execute(
                    """SELECT * FROM checkpoints
                       WHERE session_id = ? AND graph_id = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (session_id, graph_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM checkpoints
                       WHERE session_id = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            session_id=row["session_id"],
            graph_id=row["graph_id"] if "graph_id" in row.keys() else "",
            context_json=row["context_json"],
            current_node_id=row["current_node_id"],
            iteration_count=row["iteration_count"],
            created_at=row["created_at"],
        )

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """List all checkpoints for a session."""
        with self._cursor() as conn:
            rows = conn.execute(
                """SELECT * FROM checkpoints
                   WHERE session_id = ?
                   ORDER BY created_at DESC""",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Chat Session CRUD ──

    def save_chat_session(self, session: ChatSession) -> None:
        now = time.time()
        session.updated_at = now
        with self._cursor() as conn:
            conn.execute(
                """INSERT INTO chat_sessions (id, title, mode, messages_json, active_branch, graph_id, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title,
                       mode=excluded.mode,
                       messages_json=excluded.messages_json,
                       active_branch=excluded.active_branch,
                       graph_id=excluded.graph_id,
                       metadata_json=excluded.metadata_json,
                       updated_at=excluded.updated_at""",
                (
                    session.id,
                    session.title,
                    session.mode,
                    json.dumps([m.to_dict() for m in session.messages]),
                    session.active_branch,
                    session.graph_id,
                    json.dumps(session.metadata),
                    session.created_at,
                    session.updated_at,
                ),
            )

    def load_chat_session(self, session_id: str) -> ChatSession | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        data = {
            "id": row["id"],
            "title": row["title"],
            "mode": row["mode"],
            "messages": json.loads(row["messages_json"]),
            "active_branch": row["active_branch"],
            "graph_id": row["graph_id"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return ChatSession.from_dict(data)

    def list_chat_sessions(self, limit: int = 50) -> list[ChatSession]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, title, mode, active_branch, graph_id, "
                "metadata_json, created_at, updated_at "
                "FROM chat_sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        sessions = []
        for row in rows:
            data = {
                "id": row["id"],
                "title": row["title"],
                "mode": row["mode"],
                "messages": [],
                "active_branch": row["active_branch"],
                "graph_id": row["graph_id"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            sessions.append(ChatSession.from_dict(data))
        return sessions

    def delete_chat_session(self, session_id: str) -> bool:
        with self._cursor() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
            )
        return cursor.rowcount > 0

    # ── Workflow CRUD (C5: 工作流持久化) ──

    def save_workflow(self, workflow_id: str, name: str, data: dict) -> None:
        now = time.time()
        with self._cursor() as conn:
            conn.execute(
                """INSERT INTO workflows (id, name, data, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       data=excluded.data""",
                (workflow_id, name, json.dumps(data, ensure_ascii=False), now),
            )

    def load_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT data FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["data"])

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM workflows ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._cursor() as conn:
            cursor = conn.execute(
                "DELETE FROM workflows WHERE id = ?", (workflow_id,)
            )
        return cursor.rowcount > 0

    def save_workflow_run(self, run_id: str, workflow_id: str, data: dict) -> None:
        now = time.time()
        status = data.get("status", "pending")
        with self._cursor() as conn:
            conn.execute(
                """INSERT INTO workflow_runs (id, workflow_id, data, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       data=excluded.data,
                       status=excluded.status,
                       updated_at=excluded.updated_at""",
                (
                    run_id,
                    workflow_id,
                    json.dumps(data, ensure_ascii=False),
                    status,
                    now,
                    now,
                ),
            )

    def load_workflow_run(self, run_id: str) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT data FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["data"])

    def list_workflow_runs(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            if workflow_id:
                rows = conn.execute(
                    "SELECT id, workflow_id, status, created_at, updated_at "
                    "FROM workflow_runs WHERE workflow_id = ? ORDER BY created_at DESC",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, workflow_id, status, created_at, updated_at "
                    "FROM workflow_runs ORDER BY created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_workflow_run(self, run_id: str) -> bool:
        with self._cursor() as conn:
            cursor = conn.execute(
                "DELETE FROM workflow_runs WHERE id = ?", (run_id,)
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
