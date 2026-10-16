"""N6 ToolSearch (#343): lazy tool schema loading via keyword search."""

from __future__ import annotations

import json
import logging
from typing import Callable

from .base import BaseTool

logger = logging.getLogger(__name__)


class ToolSearchTool(BaseTool):
    def __init__(self, registry_provider: Callable):
        self.name = "tool_search"
        self.description = (
            "Search available tools by keyword and return their full schemas. "
            "Use before calling a tool whose schema was not shown in context."
        )
        self.parameters = {
            "query": {"type": "string", "description": "keyword to search tool names/descriptions"},
            "limit": {"type": "integer", "description": "max results (default 8)"},
        }
        self.readonly = True
        self._registry_provider = registry_provider

    async def execute(self, **kwargs) -> str:
        query = str(kwargs.get("query", ""))
        try:
            limit = int(kwargs.get("limit", 8))
        except (TypeError, ValueError):
            limit = 8
        registry = self._registry_provider()
        if registry is None:
            return "Error: tool registry unavailable"
        results = registry.search_tools(query, limit=max(1, limit))
        logger.info("tool_search query=%r limit=%d results=%d", query, limit, len(results))
        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)
