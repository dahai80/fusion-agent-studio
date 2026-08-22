"""Sub-dispatcher: MemoryDispatcher."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class MemoryDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "memory.store": self._handle_memory_store,
            "memory.recall": self._handle_memory_recall,
            "memory.list_recent": self._handle_memory_list_recent,
            "memory.get": self._handle_memory_get,
            "memory.delete": self._handle_memory_delete,
            "memory.delete_scope": self._handle_memory_delete_scope,
            "memory.count": self._handle_memory_count,
            "memory.recall_relevant": self._handle_memory_recall_relevant,
            "memory.auto_forget": self._handle_memory_auto_forget,
        }

    async def _handle_memory_store(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"status": "error", "message": "content parameter required"}
        mem = self._daemon._get_memory()
        entry_id = mem.store(
            content=content,
            scope=params.get("scope", "default"),
            tags=params.get("tags", ""),
            importance=params.get("importance", 5),
            metadata=params.get("metadata"),
            tier=params.get("tier", ""),
            memory_type=params.get("memory_type", ""),
        )
        logger.info(
            "memory.store: entry_id=%s scope=%s",
            entry_id,
            params.get("scope", "default"),
        )
        return {"entry_id": entry_id}

    async def _handle_memory_recall(self, params: dict) -> dict:
        query = params.get("query", "")
        mem = self._daemon._get_memory()
        entries = mem.recall(
            query=query,
            scope=params.get("scope", ""),
            limit=params.get("limit", 10),
            min_importance=params.get("min_importance", 0),
            tier=params.get("tier", ""),
            memory_type=params.get("memory_type", ""),
        )
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_list_recent(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        entries = mem.list_recent(
            scope=params.get("scope", ""),
            limit=params.get("limit", 20),
            min_importance=params.get("min_importance", 0),
            tier=params.get("tier", ""),
            memory_type=params.get("memory_type", ""),
        )
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._daemon._get_memory()
        entry = mem.get(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_memory_delete(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._daemon._get_memory()
        deleted = mem.delete(entry_id)
        return {"deleted": deleted}

    async def _handle_memory_delete_scope(self, params: dict) -> dict:
        scope = params.get("scope", "")
        if not scope:
            return {"status": "error", "message": "scope parameter required"}
        mem = self._daemon._get_memory()
        count = mem.delete_scope(scope)
        return {"deleted_count": count}

    async def _handle_memory_count(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        count = mem.count(
            scope=params.get("scope", ""),
            tier=params.get("tier", ""),
            memory_type=params.get("memory_type", ""),
        )
        return {"count": count}

    async def _handle_memory_recall_relevant(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        query = params.get("query", "")
        limit = params.get("limit", 5)
        scope = params.get("scope", "")
        memory_type = params.get("memory_type", "")
        result = mem.recall_relevant(
            query=query, limit=limit, scope=scope, memory_type=memory_type
        )
        return {"context": result}

    async def _handle_memory_auto_forget(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        max_entries = params.get("max_entries", 1000)
        min_importance = params.get("min_importance", 3)
        removed = mem.auto_forget(
            max_entries=max_entries, min_importance=min_importance
        )
        return {"removed": removed}

    # ── Safety handlers ──
