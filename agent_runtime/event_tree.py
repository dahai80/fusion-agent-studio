"""#497: event tree with parent_id + conversation fork.

Every run's structural events land in a SQLite event log with event_id /
parent_id linkage. fork_run copies the path-to-root prefix into a new run id
and registers a branch; branch listing + active-branch switching survive
daemon restart because everything lives in the store db.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

# Per-token frames are waterfall noise, not tree nodes — never fork on them.
_SKIPPED_EVENT_TYPES = frozenset({"token", "thinking_token"})


class EventTreeStore:
    """SQLite-backed event tree + branch registry. Reuses the AgentStore
    connection (WAL + RLock serialization) so writers never race."""

    def __init__(self, store):
        self.store = store
        self._ensure_tables()
        logger.info("EventTreeStore ready db=%s", getattr(store, "db_path", "?"))

    def _ensure_tables(self) -> None:
        with self.store._cursor() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_event_log (
                       run_id TEXT NOT NULL,
                       event_id TEXT NOT NULL,
                       parent_id TEXT NOT NULL DEFAULT '',
                       seq INTEGER NOT NULL,
                       event_json TEXT NOT NULL,
                       messages_json TEXT NOT NULL DEFAULT '[]',
                       created_at REAL NOT NULL,
                       PRIMARY KEY (run_id, event_id)
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_event_log_run ON run_event_log(run_id, seq)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_meta (
                       run_id TEXT PRIMARY KEY,
                       graph_id TEXT NOT NULL DEFAULT '',
                       session_id TEXT NOT NULL DEFAULT '',
                       task_id TEXT NOT NULL DEFAULT '',
                       team TEXT NOT NULL DEFAULT 'default',
                       root_run_id TEXT NOT NULL DEFAULT '',
                       parent_run_id TEXT NOT NULL DEFAULT '',
                       fork_event_id TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL DEFAULT 0
                   )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_meta_task ON run_meta(task_id)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_branches (
                       branch_id TEXT PRIMARY KEY,
                       run_id TEXT NOT NULL,
                       root_run_id TEXT NOT NULL,
                       parent_run_id TEXT NOT NULL DEFAULT '',
                       fork_event_id TEXT NOT NULL DEFAULT '',
                       title TEXT NOT NULL DEFAULT '',
                       active INTEGER NOT NULL DEFAULT 0,
                       created_at REAL NOT NULL DEFAULT 0
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_branches_root ON run_branches(root_run_id)"
            )

    # -- run registration -------------------------------------------------

    def register_run(
        self,
        run_id: str,
        graph_id: str = "",
        session_id: str = "",
        task_id: str = "",
        team: str = "default",
        parent_run_id: str = "",
        root_run_id: str = "",
        fork_event_id: str = "",
    ) -> None:
        if not root_run_id:
            if parent_run_id:
                meta = self.get_run_meta(parent_run_id)
                root_run_id = (meta or {}).get("root_run_id") or parent_run_id
            else:
                root_run_id = run_id
        with self.store._cursor() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO run_meta
                   (run_id, graph_id, session_id, task_id, team,
                    root_run_id, parent_run_id, fork_event_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    graph_id,
                    session_id,
                    task_id,
                    team,
                    root_run_id,
                    parent_run_id,
                    fork_event_id,
                    time.time(),
                ),
            )
        logger.info(
            "event tree run registered run=%s graph=%s task=%s parent=%s root=%s",
            run_id, graph_id, task_id, parent_run_id, root_run_id,
        )

    def get_run_meta(self, run_id: str) -> dict | None:
        with self.store._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM run_meta WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_run(self, run_id: str) -> dict | None:
        # run_id may be an execution id or the task id it was submitted with.
        meta = self.get_run_meta(run_id)
        if meta is not None:
            return meta
        with self.store._cursor() as conn:
            row = conn.execute(
                "SELECT * FROM run_meta WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    # -- event append / read ----------------------------------------------

    def append_event(
        self, run_id: str, event: dict, messages: list | None = None
    ) -> None:
        event_type = str(event.get("type") or "")
        if event_type in _SKIPPED_EVENT_TYPES:
            return
        event_id = str(event.get("event_id") or f"evt-{uuid.uuid4().hex[:12]}")
        parent_id = str(event.get("parent_id") or "")
        payload = dict(event)
        payload["event_id"] = event_id
        payload["parent_id"] = parent_id
        messages_json = json.dumps(messages or [], ensure_ascii=False)
        with self.store._cursor() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM run_event_log WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row[0]) if row is not None else 0
            conn.execute(
                """INSERT OR REPLACE INTO run_event_log
                   (run_id, event_id, parent_id, seq, event_json, messages_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    event_id,
                    parent_id,
                    seq,
                    json.dumps(payload, ensure_ascii=False),
                    messages_json,
                    time.time(),
                ),
            )
        logger.debug("event tree append run=%s seq=%d type=%s", run_id, seq, event_type)

    def list_events(self, run_id: str) -> list[dict]:
        with self.store._cursor() as conn:
            rows = conn.execute(
                """SELECT event_json, messages_json FROM run_event_log
                   WHERE run_id = ? ORDER BY seq""",
                (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            try:
                events.append(json.loads(row["event_json"]))
            except (ValueError, TypeError):
                logger.warning("event tree row undecodable run=%s", run_id)
        return events

    def path_to_root(self, run_id: str, event_id: str) -> list[dict] | None:
        # Walk the parent chain from event_id back to the run root, then
        # reverse. Unknown event id -> None (caller decides the error).
        events = self.list_events(run_id)
        by_id = {e.get("event_id"): e for e in events}
        if event_id not in by_id:
            return None
        chain = []
        current = event_id
        seen = set()
        while current and current in by_id and current not in seen:
            seen.add(current)
            chain.append(by_id[current])
            current = str(by_id[current].get("parent_id") or "")
        chain.reverse()
        return chain

    def messages_at(self, run_id: str, event_id: str = "") -> list | None:
        with self.store._cursor() as conn:
            if event_id:
                row = conn.execute(
                    "SELECT messages_json FROM run_event_log WHERE run_id = ? AND event_id = ?",
                    (run_id, event_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT messages_json FROM run_event_log WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["messages_json"])
        except (ValueError, TypeError):
            return []

    # -- fork / branches ---------------------------------------------------

    def fork_run(self, run_id: str, at_event_id: str = "", title: str = "") -> dict | None:
        meta = self.resolve_run(run_id)
        if meta is None:
            logger.warning("event tree fork: unknown run %s", run_id)
            return None
        source_run = meta["run_id"]
        events = self.list_events(source_run)
        if not events:
            logger.warning("event tree fork: run %s has no events", source_run)
            return None
        if at_event_id:
            path = self.path_to_root(source_run, at_event_id)
            if path is None:
                logger.warning("event tree fork: event %s not in run %s", at_event_id, source_run)
                return None
        else:
            path = events
            at_event_id = str(events[-1].get("event_id") or "")
        new_run = f"exec_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self.store._cursor() as conn:
            for seq, ev in enumerate(path):
                ev_id = str(ev.get("event_id") or f"evt-{uuid.uuid4().hex[:12]}")
                conn.execute(
                    """INSERT OR REPLACE INTO run_event_log
                       (run_id, event_id, parent_id, seq, event_json, messages_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_run,
                        ev_id,
                        str(ev.get("parent_id") or ""),
                        seq,
                        json.dumps(ev, ensure_ascii=False),
                        self._messages_json_for(source_run, ev_id),
                        now,
                    ),
                )
        root = meta.get("root_run_id") or source_run
        branch_title = title or f"fork of {source_run} at {at_event_id[:12]}"
        self.register_run(
            new_run,
            graph_id=meta.get("graph_id", ""),
            session_id=meta.get("session_id", ""),
            team=meta.get("team", "default"),
            parent_run_id=source_run,
            root_run_id=root,
            fork_event_id=at_event_id,
        )
        with self.store._cursor() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO run_branches
                   (branch_id, run_id, root_run_id, parent_run_id, fork_event_id,
                    title, active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (new_run, new_run, root, source_run, at_event_id, branch_title, now),
            )
        logger.info(
            "event tree forked run=%s -> %s at_event=%s events=%d",
            source_run, new_run, at_event_id, len(path),
        )
        return {
            "run_id": new_run,
            "branch_id": new_run,
            "parent_run_id": source_run,
            "root_run_id": root,
            "fork_event_id": at_event_id,
            "title": branch_title,
            "events_copied": len(path),
            "messages": self.messages_at(source_run, at_event_id) or [],
            "graph_id": meta.get("graph_id", ""),
        }

    def _messages_json_for(self, run_id: str, event_id: str) -> str:
        with self.store._cursor() as conn:
            row = conn.execute(
                "SELECT messages_json FROM run_event_log WHERE run_id = ? AND event_id = ?",
                (run_id, event_id),
            ).fetchone()
        return row["messages_json"] if row is not None else "[]"

    def list_branches(self, run_id: str) -> dict | None:
        meta = self.resolve_run(run_id)
        if meta is None:
            return None
        root = meta.get("root_run_id") or meta["run_id"]
        with self.store._cursor() as conn:
            rows = conn.execute(
                """SELECT branch_id, run_id, root_run_id, parent_run_id,
                          fork_event_id, title, active, created_at
                   FROM run_branches WHERE root_run_id = ? ORDER BY created_at""",
                (root,),
            ).fetchall()
        branches = [dict(r) for r in rows]
        active = next((b["branch_id"] for b in branches if b.get("active")), "")
        return {"root_run_id": root, "branches": branches, "active_branch_id": active}

    def activate_branch(self, run_id: str, branch_id: str) -> dict | None:
        listing = self.list_branches(run_id)
        if listing is None:
            return None
        root = listing["root_run_id"]
        if not any(b["branch_id"] == branch_id for b in listing["branches"]):
            logger.warning("event tree activate: branch %s not in family of %s", branch_id, root)
            return None
        with self.store._cursor() as conn:
            conn.execute("UPDATE run_branches SET active = 0 WHERE root_run_id = ?", (root,))
            conn.execute("UPDATE run_branches SET active = 1 WHERE branch_id = ?", (branch_id,))
        logger.info("event tree branch activated root=%s branch=%s", root, branch_id)
        return {"root_run_id": root, "activated_branch_id": branch_id}
