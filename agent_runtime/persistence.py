"""Agent state persistence — save and restore agent execution state using SQLite."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import AgentContext
from .graph import AgentGraph


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
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

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
        """)
        conn.commit()

    # ── Graph CRUD ──

    def save_graph(self, graph: AgentGraph) -> None:
        """Save or update an agent graph."""
        conn = self._get_conn()
        now = time.time()
        conn.execute(
            """INSERT INTO graphs (id, name, description, data, version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   description=excluded.description,
                   data=excluded.data,
                   version=excluded.version,
                   updated_at=excluded.updated_at""",
            (graph.id, graph.name, graph.description, graph.to_json(),
             graph.version, now, now),
        )
        conn.commit()

    def load_graph(self, graph_id: str) -> AgentGraph | None:
        """Load an agent graph by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT data FROM graphs WHERE id = ?", (graph_id,)).fetchone()
        if row is None:
            return None
        return AgentGraph.from_json(row["data"])

    def list_graphs(self) -> list[dict[str, Any]]:
        """List all saved graphs with metadata."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, name, description, version, created_at, updated_at FROM graphs ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_graph(self, graph_id: str) -> bool:
        """Delete a graph by ID. Returns True if deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))
        conn.commit()
        return cursor.rowcount > 0

    # ── Session Management ──

    def create_session(self, session_id: str, graph_id: str, name: str = "") -> None:
        """Create a new execution session."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO sessions (session_id, graph_id, name, status, created_at) VALUES (?, ?, ?, 'created', ?)",
            (session_id, graph_id, name, time.time()),
        )
        conn.commit()

    def update_session_status(self, session_id: str, status: str) -> None:
        """Update session status ('running', 'completed', 'failed')."""
        conn = self._get_conn()
        now = time.time()
        finished_at = now if status in ("completed", "failed") else None
        conn.execute(
            "UPDATE sessions SET status = ?, finished_at = ? WHERE session_id = ?",
            (status, finished_at, session_id),
        )
        conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Get session metadata."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent sessions."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Checkpoint Management ──

    def save_checkpoint(self, session_id: str, context: AgentContext,
                        current_node_id: str) -> int:
        """Save an execution checkpoint. Returns checkpoint ID."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO checkpoints (session_id, context_json, current_node_id, iteration_count, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, json.dumps(context.to_dict()), current_node_id,
             context.iteration_count, time.time()),
        )
        conn.commit()
        return cursor.lastrowid or 0

    def load_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for a session."""
        conn = self._get_conn()
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
            graph_id="",
            context_json=row["context_json"],
            current_node_id=row["current_node_id"],
            iteration_count=row["iteration_count"],
            created_at=row["created_at"],
        )

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """List all checkpoints for a session."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM checkpoints
               WHERE session_id = ?
               ORDER BY created_at DESC""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None