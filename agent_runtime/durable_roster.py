"""Durable subagent roster + mailbox (#389, DSH §9.3).

Both survive daemon restarts: SQLite with WAL, one file per installation.
Roster tracks agent identity/capabilities/status; mailbox is a durable
point-to-point message queue with ack-based delivery (at-least-once).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB = os.path.join(
    os.path.expanduser("~"), ".fusion-agent-studio", "team.db"
)


@contextmanager
def _connect(path: str | None = None):
    target = path or os.environ.get("FUSION_TEAM_DB", DEFAULT_DB)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


class DurableRoster:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("FUSION_TEAM_DB", DEFAULT_DB)
        self._init()
        logger.info("durable roster ready path=%s", self.path)

    def _init(self) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS roster (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    team TEXT NOT NULL DEFAULT '',
                    capabilities TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idle',
                    graph_id TEXT NOT NULL DEFAULT '',
                    autonomous_inbox INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            # #457: migrate pre-existing DBs (ALTER TABLE lacks IF NOT EXISTS).
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(roster)").fetchall()
            }
            if "graph_id" not in cols:
                conn.execute(
                    "ALTER TABLE roster ADD COLUMN graph_id TEXT NOT NULL DEFAULT ''"
                )
            if "autonomous_inbox" not in cols:
                conn.execute(
                    "ALTER TABLE roster ADD COLUMN autonomous_inbox INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mailbox (
                    message_id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    team TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'message',
                    body TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    acked_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS mailbox_recipient ON mailbox(recipient, acked_at)"
            )

    def register(
        self,
        agent_id: str,
        name: str = "",
        team: str = "",
        capabilities: list[str] | None = None,
        status: str = "idle",
        graph_id: str = "",
        autonomous_inbox: bool = False,
    ) -> dict:
        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id required")
        now = time.time()
        caps = ",".join(capabilities or [])
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO roster
                    (agent_id, name, team, capabilities, status, graph_id,
                     autonomous_inbox, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name, team=excluded.team,
                    capabilities=excluded.capabilities, status=excluded.status,
                    graph_id=excluded.graph_id,
                    autonomous_inbox=excluded.autonomous_inbox,
                    updated_at=excluded.updated_at
                """,
                (
                    agent_id, name or agent_id, team, caps, status,
                    graph_id, int(bool(autonomous_inbox)), now,
                ),
            )
        logger.info(
            "roster register agent=%s team=%s status=%s graph=%s autonomous_inbox=%s",
            agent_id, team, status, graph_id, autonomous_inbox,
        )
        return self.get(agent_id) or {}

    def get(self, agent_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM roster WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list(self, team: str = "") -> list[dict]:
        with _connect(self.path) as conn:
            if team:
                rows = conn.execute(
                    "SELECT * FROM roster WHERE team = ? ORDER BY updated_at DESC", (team,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM roster ORDER BY updated_at DESC"
                ).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, agent_id: str, status: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute(
                "UPDATE roster SET status = ?, updated_at = ? WHERE agent_id = ?",
                (status, time.time(), agent_id),
            )
        logger.info("roster status agent=%s status=%s ok=%s", agent_id, status, cur.rowcount > 0)
        return cur.rowcount > 0

    def remove(self, agent_id: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute("DELETE FROM roster WHERE agent_id = ?", (agent_id,))
        return cur.rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["capabilities"] = [c for c in (data.get("capabilities") or "").split(",") if c]
        return data


class Mailbox:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("FUSION_TEAM_DB", DEFAULT_DB)
        self._init()
        logger.info("durable mailbox ready path=%s", self.path)

    def _init(self) -> None:
        DurableRoster(self.path)

    def send(
        self,
        sender: str,
        recipient: str,
        body: str = "",
        kind: str = "message",
        team: str = "",
    ) -> dict:
        if not sender or not recipient:
            raise ValueError("sender and recipient required")
        message_id = uuid.uuid4().hex[:16]
        now = time.time()
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO mailbox
                    (message_id, sender, recipient, team, kind, body, created_at, acked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (message_id, sender, recipient, team, kind, str(body)[:20000], now),
            )
        logger.info("mailbox send id=%s sender=%s recipient=%s", message_id, sender, recipient)
        return {
            "message_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "team": team,
            "kind": kind,
            "body": body,
            "created_at": now,
            "acked_at": 0,
        }

    def broadcast(
        self, sender: str, team: str, body: str = "", kind: str = "broadcast"
    ) -> list[dict]:
        targets = DurableRoster(self.path).list(team=team)
        return [
            self.send(sender, row["agent_id"], body, kind=kind, team=team)
            for row in targets
            if row["agent_id"] != sender
        ]

    def inbox(self, recipient: str, unread_only: bool = True, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM mailbox WHERE recipient = ?"
        if unread_only:
            query += " AND acked_at = 0"
        query += " ORDER BY created_at ASC LIMIT ?"
        with _connect(self.path) as conn:
            rows = conn.execute(query, (recipient, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def ack(self, recipient: str, message_id: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute(
                "UPDATE mailbox SET acked_at = ? WHERE message_id = ? AND recipient = ?",
                (time.time(), message_id, recipient),
            )
        logger.info("mailbox ack id=%s recipient=%s ok=%s", message_id, recipient, cur.rowcount > 0)
        return cur.rowcount > 0

    def stats(self) -> dict:
        with _connect(self.path) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM mailbox WHERE acked_at = 0"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM mailbox").fetchone()[0]
        return {"total": total, "pending": pending}
