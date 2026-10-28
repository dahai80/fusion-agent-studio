"""Sub-dispatcher: SessionDispatcher — session REST surface (#425), file
checkpointing (#426), live input injection (#427), pluggable store (#429)."""

from __future__ import annotations

import logging
import os
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class SessionDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "sessions.list": self._handle_list,
            "sessions.get": self._handle_get,
            "sessions.messages": self._handle_messages,
            "sessions.rename": self._handle_rename,
            "sessions.resume": self._handle_resume,
            "sessions.fork": self._handle_fork,
            "sessions.inject_input": self._handle_inject_input,
            "sessions.store_info": self._handle_store_info,
            "sessions.lifecycle": self._handle_lifecycle,
            "file_checkpoint.snapshot": self._handle_fc_snapshot,
            "file_checkpoint.list": self._handle_fc_list,
            "file_checkpoint.rollback": self._handle_fc_rollback,
            "file_checkpoint.delete": self._handle_fc_delete,
        }

    def _store(self):
        from ..session_store import get_session_store

        return get_session_store(getattr(self._daemon, "store", None))

    def _checkpoints(self):
        from ..file_checkpoint import FileCheckpointStore

        store = getattr(self._daemon, "_file_checkpoints", None)
        if store is None:
            store = FileCheckpointStore()
            self._daemon._file_checkpoints = store
        return store

    async def _handle_list(self, params: dict) -> dict:
        limit = int(params.get("limit", 50) or 50)
        offset = int(params.get("offset", 0) or 0)
        tag = str(params.get("tag", "") or "")
        rows = self._store().list(limit=limit + offset)
        if tag:
            rows = [
                r
                for r in rows
                if tag in (r.get("metadata", {}) or {}).get("tags", [])
            ]
        rows = rows[offset: offset + limit]
        out = []
        for row in rows:
            out.append(
                {
                    "id": row.get("id", ""),
                    "title": row.get("title", ""),
                    "mode": row.get("mode", ""),
                    "graph_id": row.get("graph_id", ""),
                    "summary": (row.get("metadata", {}) or {}).get("summary", ""),
                    "tags": (row.get("metadata", {}) or {}).get("tags", []),
                    "cwd": (row.get("metadata", {}) or {}).get("cwd", ""),
                    "git_branch": (row.get("metadata", {}) or {}).get("git_branch", ""),
                    "tokens": (row.get("metadata", {}) or {}).get("tokens", 0),
                    "created_at": row.get("created_at", 0),
                    "updated_at": row.get("updated_at", 0),
                }
            )
        logger.info("sessions.list limit=%d offset=%d tag=%s count=%d", limit, offset, tag or "-", len(out))
        return {"status": "ok", "sessions": out, "total": len(out), "offset": offset}

    async def _handle_get(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        row = self._store().get(session_id)
        if row is None:
            return {"status": "error", "message": f"session not found: {session_id}"}
        row.pop("messages", None)
        return {"status": "ok", "session": row}

    async def _handle_messages(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        limit = int(params.get("limit", 200) or 200)
        row = self._store().get(session_id)
        if row is None:
            return {"status": "error", "message": f"session not found: {session_id}"}
        messages = [m for m in row.get("messages") or [] if isinstance(m, dict)]
        logger.info("sessions.messages id=%s count=%d", session_id, len(messages))
        return {"status": "ok", "session_id": session_id, "messages": messages[-limit:]}

    async def _handle_rename(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        row = self._store().get(session_id)
        if row is None:
            return {"status": "error", "message": f"session not found: {session_id}"}
        if params.get("title") is not None:
            row["title"] = str(params.get("title"))
        metadata = dict(row.get("metadata") or {})
        if params.get("tag") is not None:
            tag = str(params.get("tag"))
            tags = [t for t in metadata.get("tags", []) if t != tag]
            if tag:
                tags.append(tag)
            metadata["tags"] = tags
        if params.get("summary") is not None:
            metadata["summary"] = str(params.get("summary"))
        row["metadata"] = metadata
        updated = self._store().put(row)
        updated.pop("messages", None)
        logger.info("sessions.rename id=%s title=%r", session_id, row.get("title", ""))
        return {"status": "ok", "session": updated}

    async def _handle_resume(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        input_text = str(params.get("input", "") or params.get("text", "") or "")
        row = self._store().get(session_id)
        if row is None:
            return {"status": "error", "message": f"session not found: {session_id}"}
        manager = self._daemon._get_session_manager()
        bg = await manager.fork(session_id, input_text=input_text)
        logger.info("sessions.resume id=%s -> bg=%s", session_id, bg.id)
        return {
            "status": "ok",
            "session_id": session_id,
            "background_session_id": bg.id,
            "fork_mode": bg.metadata.get("fork_mode", ""),
            "state": bg.status.value,
        }

    async def _handle_fork(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        title = str(params.get("title", "") or "")
        row = self._store().fork(session_id, title=title)
        if row is None:
            return {"status": "error", "message": f"session not found: {session_id}"}
        logger.info("sessions.fork from=%s to=%s", session_id, row.get("id"))
        return {"status": "ok", "session": row, "forked_from": session_id}

    async def _handle_inject_input(self, params: dict) -> dict:
        session_id = str(params.get("session_id", "") or params.get("id", ""))
        text = str(params.get("text", "") or params.get("input", "") or "")
        manager = self._daemon._get_session_manager()
        result = manager.inject_input(session_id, text)
        if not result.get("ok"):
            return {"status": "error", "message": result.get("error", "inject failed")}
        logger.info("sessions.inject_input id=%s pending=%d", session_id, result.get("pending", 0))
        return {"status": "ok", **result}

    async def _handle_store_info(self, params: dict) -> dict:
        store = self._store()
        backend = "file" if type(store).__name__ == "FileSessionStore" else "sqlite"
        return {
            "status": "ok",
            "backend": backend,
            "env": os.environ.get("FUSION_SESSION_STORE", "sqlite"),
        }

    async def _handle_lifecycle(self, params: dict) -> dict:
        # #485: governance/canvas run-event surface reading from the
        # lifecycle bus feed instead of a parallel path. In-memory feed
        # first; empty (post-restart) feeds reconstruct from the bus JSONL.
        session_id = str(params.get("session_id", "") or "")
        limit = int(params.get("limit", 0) or 0)
        feed = self._daemon._get_lifecycle_feed()
        events = feed.events(session_id, limit=limit)
        restored = 0
        if not events:
            restored = feed.restore_from_jsonl(session_id)
            events = feed.events(session_id, limit=limit)
        summary = feed.summary(session_id)
        logger.info(
            "sessions.lifecycle session=%s events=%d restored=%d domains=%s",
            session_id, len(events), restored, summary.get("by_domain"),
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "restored_from_jsonl": restored,
            "events": events,
            "summary": summary,
        }

    async def _handle_fc_snapshot(self, params: dict) -> dict:
        root = str(params.get("root", "") or os.getcwd())
        result = self._checkpoints().snapshot(
            root=root,
            run_id=str(params.get("run_id", "")),
            label=str(params.get("label", "")),
            exclude=params.get("exclude") or None,
        )
        return {"status": "ok", **result}

    async def _handle_fc_list(self, params: dict) -> dict:
        rows = self._checkpoints().list(
            run_id=str(params.get("run_id", "")),
            limit=int(params.get("limit", 50) or 50),
        )
        return {"status": "ok", "snapshots": rows}

    async def _handle_fc_rollback(self, params: dict) -> dict:
        snapshot_id = str(params.get("snapshot_id", ""))
        result = self._checkpoints().rollback(snapshot_id, root=str(params.get("root", "")))
        if not result.get("ok") and result.get("error"):
            return {"status": "error", "message": result["error"]}
        logger.info(
            "file_checkpoint.rollback id=%s restored=%d removed=%d",
            snapshot_id,
            result.get("restored", 0),
            result.get("removed", 0),
        )
        return {"status": "ok", **result}

    async def _handle_fc_delete(self, params: dict) -> dict:
        ok = self._checkpoints().delete(str(params.get("snapshot_id", "")))
        return {"status": "ok", "deleted": ok}
