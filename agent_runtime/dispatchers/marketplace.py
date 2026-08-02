import logging
from typing import Any, Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class MarketplaceDispatcher(SubDispatcher):
    def __init__(self, daemon: Any):
        super().__init__(daemon)
        self._marketplace = None

    def get_handlers(self) -> dict[str, Callable]:
        return {
            "marketplace.search": self._handle_search,
            "marketplace.get": self._handle_get,
            "marketplace.publish": self._handle_publish,
            "marketplace.unpublish": self._handle_unpublish,
            "marketplace.list_categories": self._handle_list_categories,
            "marketplace.install": self._handle_install,
            "marketplace.uninstall": self._handle_uninstall,
        }

    def _get_marketplace(self):
        if self._marketplace is None:
            from ..agent_marketplace import AgentMarketplace
            self._marketplace = AgentMarketplace()
            logger.info("AgentMarketplace created at %s", self._marketplace.store_dir)
        return self._marketplace

    async def _handle_search(self, params: dict) -> dict:
        mp = self._get_marketplace()
        results = mp.search(
            query=params.get("query", ""),
            category=params.get("category", ""),
            tags=params.get("tags"),
            sort_by=params.get("sort_by", "name"),
            limit=params.get("limit", 50),
        )
        return {"entries": [e.to_dict() for e in results]}

    async def _handle_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        entry = mp.get(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_publish(self, params: dict) -> dict:
        from ..agent_marketplace import MarketEntry
        mp = self._get_marketplace()
        entry = MarketEntry(
            name=params.get("name", ""),
            author=params.get("author", ""),
            description=params.get("description", ""),
            category=params.get("category", ""),
            tags=params.get("tags", []),
            version=params.get("version", "1.0.0"),
            graph_data=params.get("graph_data", {}),
        )
        entry_id = mp.publish(entry)
        logger.info("marketplace.publish: id=%s name=%s", entry_id, entry.name)
        return {"entry_id": entry_id}

    async def _handle_unpublish(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        ok = mp.unpublish(entry_id)
        return {"unpublished": ok}

    async def _handle_list_categories(self, params: dict) -> dict:
        mp = self._get_marketplace()
        return {"categories": mp.list_categories()}

    async def _handle_install(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        result = mp.install(entry_id, target_dir=params.get("target_dir"))
        if result is None:
            return {"status": "error", "message": f"Install failed for: {entry_id}"}
        logger.info("marketplace.install: id=%s path=%s", entry_id, result)
        return {"installed": True, "path": str(result)}

    async def _handle_uninstall(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._get_marketplace()
        entry = mp.get(entry_id)
        if not entry:
            return {"success": False, "message": f"Entry not found: {entry_id}"}
        ok = mp.unpublish(entry_id)
        logger.info("marketplace.uninstall: id=%s success=%s", entry_id, ok)
        return {"success": ok}
