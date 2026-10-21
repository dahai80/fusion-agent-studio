from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB = os.path.join(
    os.path.expanduser("~"), ".fusion-agent-studio", "subagents.db"
)


def _db_path(path: str | None = None) -> str:
    return path or os.environ.get("FUSION_SUBAGENT_DB", DEFAULT_DB)


@contextmanager
def _connect(path: str | None = None):
    target = _db_path(path)
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


class SubAgentTranscriptStore:
    def __init__(self, path: str | None = None):
        self.path = _db_path(path)
        self._init()
        logger.info("subagent transcript store ready path=%s", self.path)

    def _init(self) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subagent_transcripts (
                    agent_id TEXT PRIMARY KEY,
                    parent_session TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    team TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    partial INTEGER NOT NULL DEFAULT 0,
                    turns INTEGER NOT NULL DEFAULT 0,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    report TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    spec TEXT NOT NULL DEFAULT '{}',
                    messages TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS subagent_parent "
                "ON subagent_transcripts(parent_session, created_at)"
            )

    def create(
        self,
        parent_session: str = "",
        name: str = "",
        team: str = "",
        model: str = "",
        task: str = "",
        spec: dict | None = None,
    ) -> str:
        agent_id = f"{name or 'sub'}-{uuid.uuid4().hex[:12]}"
        now = time.time()
        with _connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO subagent_transcripts
                    (agent_id, parent_session, name, team, model, task, status,
                     partial, turns, tokens_used, report, error, spec, messages,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'running', 0, 0, 0, '', '', ?, '[]', ?, ?)
                """,
                (
                    agent_id,
                    parent_session,
                    name,
                    team,
                    model,
                    task[:4000],
                    json.dumps(spec or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        logger.info(
            "subagent transcript created agent_id=%s parent=%s name=%s",
            agent_id,
            parent_session,
            name,
        )
        return agent_id

    def append(self, agent_id: str, messages: list[dict]) -> None:
        if not messages:
            return
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT messages FROM subagent_transcripts WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                logger.warning("subagent transcript append missing agent_id=%s", agent_id)
                return
            try:
                existing = json.loads(row["messages"] or "[]")
            except ValueError:
                existing = []
            existing.extend(m for m in messages if isinstance(m, dict))
            conn.execute(
                "UPDATE subagent_transcripts SET messages = ?, updated_at = ? "
                "WHERE agent_id = ?",
                (json.dumps(existing, ensure_ascii=False), time.time(), agent_id),
            )

    def finish(
        self,
        agent_id: str,
        ok: bool,
        partial: bool,
        turns: int,
        tokens_used: int,
        report: str = "",
        error: str = "",
    ) -> None:
        status = "completed" if ok else "failed"
        if partial:
            status = "partial"
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE subagent_transcripts SET status = ?, partial = ?, turns = ?, "
                "tokens_used = ?, report = ?, error = ?, updated_at = ? WHERE agent_id = ?",
                (status, 1 if partial else 0, int(turns), int(tokens_used),
                 report[:20000], error[:4000], time.time(), agent_id),
            )
        logger.info(
            "subagent transcript finished agent_id=%s status=%s partial=%s turns=%d",
            agent_id,
            status,
            partial,
            turns,
        )

    def get(self, agent_id: str, include_messages: bool = True) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM subagent_transcripts WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row(row, include_messages)

    def list(self, parent_session: str = "", limit: int = 100) -> list[dict]:
        query = "SELECT * FROM subagent_transcripts"
        args: list = []
        if parent_session:
            query += " WHERE parent_session = ?"
            args.append(parent_session)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with _connect(self.path) as conn:
            rows = conn.execute(query, args).fetchall()
        return [self._row(r, False) for r in rows]

    def delete(self, agent_id: str) -> bool:
        with _connect(self.path) as conn:
            cur = conn.execute(
                "DELETE FROM subagent_transcripts WHERE agent_id = ?", (agent_id,)
            )
        logger.info("subagent transcript delete agent_id=%s ok=%s", agent_id, cur.rowcount > 0)
        return cur.rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row, include_messages: bool) -> dict:
        data = dict(row)
        messages = data.pop("messages", "[]")
        raw_spec = data.pop("spec", "{}")
        try:
            data["spec"] = json.loads(raw_spec or "{}")
        except ValueError:
            data["spec"] = {}
        data["partial"] = bool(data.get("partial"))
        if include_messages:
            try:
                data["messages"] = json.loads(messages or "[]")
            except ValueError:
                data["messages"] = []
        else:
            try:
                data["message_count"] = len(json.loads(messages or "[]"))
            except ValueError:
                data["message_count"] = 0
        return data


class SubAgentTaskManager:
    def __init__(self, runner, store: SubAgentTranscriptStore | None = None):
        self.runner = runner
        self.store = store or SubAgentTranscriptStore()
        self._tasks: dict[str, asyncio.Task] = {}
        self._reports: dict[str, dict] = {}
        logger.info("subagent task manager init")

    def spawn(self, spec, parent_ctx=None) -> dict:
        agent_id = self.store.create(
            parent_session=getattr(parent_ctx, "session_id", "") if parent_ctx else "",
            name=spec.name,
            team=spec.team,
            model=spec.model,
            task=spec.task,
            spec=spec.to_dict(),
        )
        task = asyncio.create_task(self._run_task(agent_id, spec, parent_ctx))
        self._tasks[agent_id] = task
        logger.info(
            "subagent launched agent_id=%s name=%s background=%s",
            agent_id,
            spec.name,
            spec.background,
        )
        return {
            "agent_id": agent_id,
            "name": spec.name,
            "status": "launched",
            "background": bool(spec.background),
        }

    async def _run_task(self, agent_id: str, spec, parent_ctx) -> None:
        try:
            report = await self.runner.run(spec, parent_ctx, agent_id=agent_id)
            self._reports[agent_id] = report.to_dict()
        except asyncio.CancelledError:
            self.store.finish(agent_id, ok=False, partial=False, turns=0,
                              tokens_used=0, error="cancelled")
            raise
        except Exception as e:
            logger.error("subagent task failed agent_id=%s err=%s", agent_id, e)
            self.store.finish(agent_id, ok=False, partial=False, turns=0,
                              tokens_used=0, error=str(e))
            self._reports[agent_id] = {"name": spec.name, "ok": False, "error": str(e)}

    async def wait(self, agent_id: str, timeout: float = 0) -> dict | None:
        task = self._tasks.get(agent_id)
        if task is None:
            record = self.store.get(agent_id)
            if record is None:
                return None
            return self._reports.get(agent_id) or {
                "name": record.get("name", ""),
                "ok": record.get("status") == "completed",
                "report": record.get("report", ""),
                "turns": record.get("turns", 0),
                "partial": record.get("partial", False),
            }
        try:
            if timeout > 0:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            else:
                await task
        except asyncio.TimeoutError:
            logger.warning("subagent wait timeout agent_id=%s timeout=%s", agent_id, timeout)
            return None
        return self._reports.get(agent_id)

    def cancel(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("subagent cancel requested agent_id=%s", agent_id)
        return True

    def tree(self, parent_session: str = "", limit: int = 100) -> dict:
        rows = self.store.list(parent_session=parent_session, limit=limit)
        nodes = []
        for row in rows:
            nodes.append(
                {
                    "agent_id": row.get("agent_id"),
                    "name": row.get("name"),
                    "parent_session": row.get("parent_session"),
                    "status": row.get("status"),
                    "partial": row.get("partial"),
                    "turns": row.get("turns"),
                    "tokens_used": row.get("tokens_used"),
                    "message_count": row.get("message_count", 0),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        running = sum(1 for n in nodes if n["status"] == "running")
        logger.info("subagent tree parent=%s nodes=%d running=%d", parent_session or "-", len(nodes), running)
        return {"parent_session": parent_session, "nodes": nodes, "running": running}
