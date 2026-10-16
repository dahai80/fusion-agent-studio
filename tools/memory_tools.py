"""N15 memory tool group (#354): read/write/search/forget via ToolFunnel.

Write/forget are NOT readonly — the permission engine gates them; read/search
are readonly (plan-mode safe).
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from .base import BaseTool

logger = logging.getLogger(__name__)


class MemoryReadTool(BaseTool):
    def __init__(self, engine_provider: Callable):
        self.name = "memory_read"
        self.description = "Read a memory entry by id, or list recent entries in a scope."
        self.parameters = {
            "entry_id": {"type": "string", "description": "memory entry id (optional)"},
            "scope": {"type": "string", "description": "scope to list recent from (optional)"},
            "limit": {"type": "integer", "description": "list limit (default 10)"},
        }
        self.readonly = True
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        engine = self._engine_provider()
        if engine is None:
            return "Error: memory engine unavailable"
        entry_id = str(kwargs.get("entry_id", "") or "")
        try:
            if entry_id:
                entry = engine.get(entry_id)
                if entry is None:
                    return f"Error: memory not found: {entry_id}"
                return json.dumps(entry.to_dict(), ensure_ascii=False)
            scope = str(kwargs.get("scope", "") or "")
            try:
                limit = int(kwargs.get("limit", 10))
            except (TypeError, ValueError):
                limit = 10
            rows = engine.list_recent(scope=scope, limit=max(1, limit))
            return json.dumps([r.to_dict() if hasattr(r, "to_dict") else r for r in rows], ensure_ascii=False)
        except Exception as e:
            logger.warning("memory_read failed: %s", e)
            return f"Error: memory_read failed: {e}"


class MemoryWriteTool(BaseTool):
    def __init__(self, engine_provider: Callable):
        self.name = "memory_write"
        self.description = "Store a memory fact (classified by four-type frontmatter semantics)."
        self.parameters = {
            "content": {"type": "string", "description": "the fact to remember"},
            "scope": {"type": "string", "description": "scope key (e.g. session/team id)"},
            "memory_type": {"type": "string", "description": "user|feedback|project|reference"},
            "importance": {"type": "integer", "description": "1-10 importance"},
        }
        self.readonly = False
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        engine = self._engine_provider()
        if engine is None:
            return "Error: memory engine unavailable"
        content = str(kwargs.get("content", "") or "").strip()
        if not content:
            return "Error: content required"
        scope = str(kwargs.get("scope", "") or "default")
        memory_type = str(kwargs.get("memory_type", "") or "")
        try:
            importance = int(kwargs.get("importance", 5))
        except (TypeError, ValueError):
            importance = 5
        try:
            if memory_type:
                entry_id = engine.store(
                    content=content, scope=scope, importance=importance,
                    memory_type=memory_type,
                )
            else:
                entry_id = engine.store(content=content, scope=scope, importance=importance)
            logger.info(
                "memory.write id=%s scope=%s type=%s importance=%d",
                entry_id, scope, memory_type or "auto", importance,
            )
            return json.dumps({"id": entry_id, "status": "stored"}, ensure_ascii=False)
        except Exception as e:
            logger.warning("memory_write failed: %s", e)
            return f"Error: memory_write failed: {e}"


class MemorySearchTool(BaseTool):
    def __init__(self, engine_provider: Callable):
        self.name = "memory_search"
        self.description = "Full-text search over stored memories."
        self.parameters = {
            "query": {"type": "string", "description": "search query"},
            "limit": {"type": "integer", "description": "max results (default 5)"},
        }
        self.readonly = True
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        engine = self._engine_provider()
        if engine is None:
            return "Error: memory engine unavailable"
        query = str(kwargs.get("query", "") or "")
        if not query:
            return "Error: query required"
        try:
            limit = int(kwargs.get("limit", 5))
        except (TypeError, ValueError):
            limit = 5
        try:
            rows = engine.recall(query, limit=max(1, limit))
            return json.dumps(
                [r.to_dict() if hasattr(r, "to_dict") else r for r in rows],
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning("memory_search failed: %s", e)
            return f"Error: memory_search failed: {e}"


class MemoryForgetTool(BaseTool):
    def __init__(self, engine_provider: Callable):
        self.name = "memory_forget"
        self.description = "Delete a memory entry by id."
        self.parameters = {
            "entry_id": {"type": "string", "description": "memory entry id"},
        }
        self.readonly = False
        self._engine_provider = engine_provider

    async def execute(self, **kwargs) -> str:
        engine = self._engine_provider()
        if engine is None:
            return "Error: memory engine unavailable"
        entry_id = str(kwargs.get("entry_id", "") or "")
        if not entry_id:
            return "Error: entry_id required"
        try:
            ok = engine.delete(entry_id)
            logger.info("memory.forget id=%s ok=%s", entry_id, ok)
            if not ok:
                return f"Error: memory not found: {entry_id}"
            return json.dumps({"id": entry_id, "status": "deleted"}, ensure_ascii=False)
        except Exception as e:
            logger.warning("memory_forget failed: %s", e)
            return f"Error: memory_forget failed: {e}"


def create_memory_tools(engine_provider: Callable) -> list[BaseTool]:
    return [
        MemoryReadTool(engine_provider),
        MemoryWriteTool(engine_provider),
        MemorySearchTool(engine_provider),
        MemoryForgetTool(engine_provider),
    ]
