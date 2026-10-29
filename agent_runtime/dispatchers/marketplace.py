"""Sub-dispatcher: MarketplaceDispatcher."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class MarketplaceDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "marketplace.search": self._handle_marketplace_search,
            "marketplace.get": self._handle_marketplace_get,
            "marketplace.publish": self._handle_marketplace_publish,
            "marketplace.unpublish": self._handle_marketplace_unpublish,
            "marketplace.list_categories": self._handle_marketplace_list_categories,
            "marketplace.install": self._handle_marketplace_install,
            "marketplace.uninstall": self._handle_marketplace_uninstall,
            "marketplace.audit": self._handle_marketplace_audit,
            # #503: persona-pack distribution (signed publish/install)
            "marketplace.persona_pack_publish": self._handle_persona_pack_publish,
            "marketplace.persona_pack_install": self._handle_persona_pack_install,
            "marketplace.persona_pack_list": self._handle_persona_pack_list,
        }

    @staticmethod
    def _tenant(params: dict) -> str:
        # #481: tenant scoping — explicit param wins; auth context (when
        # FUSION_IDENTITY_ENABLED resolves it) is consumed by the daemon
        # layer and passed down the same way.
        return str(params.get("tenant", "default") or "default")

    async def _handle_marketplace_search(self, params: dict) -> dict:
        mp = self._daemon._get_marketplace()
        results = mp.search(
            query=params.get("query", ""),
            category=params.get("category", ""),
            tags=params.get("tags"),
            sort_by=params.get("sort_by", "name"),
            limit=params.get("limit", 50),
            tenant=self._tenant(params),
        )
        return {"entries": [e.to_dict() for e in results]}

    async def _handle_marketplace_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._daemon._get_marketplace()
        entry = mp.get(entry_id, tenant=self._tenant(params))
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_marketplace_publish(self, params: dict) -> dict:
        from ..agent_marketplace import MarketEntry

        mp = self._daemon._get_marketplace()
        entry = MarketEntry(
            name=params.get("name", ""),
            author=params.get("author", ""),
            description=params.get("description", ""),
            category=params.get("category", ""),
            tags=params.get("tags", []),
            version=params.get("version", "1.0.0"),
            graph_data=params.get("graph_data", {}),
            visibility=str(params.get("visibility", "public") or "public"),
        )
        entry_id = mp.publish(entry, tenant=self._tenant(params))
        logger.info("marketplace.publish: id=%s name=%s", entry_id, entry.name)
        return {"entry_id": entry_id}

    async def _handle_marketplace_unpublish(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._daemon._get_marketplace()
        ok = mp.unpublish(entry_id, tenant=self._tenant(params))
        return {"unpublished": ok}

    async def _handle_marketplace_list_categories(self, params: dict) -> dict:
        mp = self._daemon._get_marketplace()
        return {"categories": mp.list_categories()}

    async def _handle_marketplace_install(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._daemon._get_marketplace()
        result = mp.install(
            entry_id, target_dir=params.get("target_dir"), tenant=self._tenant(params)
        )
        if result is None:
            return {"status": "error", "message": f"Install failed for: {entry_id}"}
        logger.info("marketplace.install: id=%s path=%s", entry_id, result)
        return {"installed": True, "path": str(result)}

    async def _handle_marketplace_uninstall(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"status": "error", "message": "entry_id parameter required"}
        mp = self._daemon._get_marketplace()
        entry = mp.get(entry_id, tenant=self._tenant(params))
        if not entry:
            return {"success": False, "message": f"Entry not found: {entry_id}"}
        ok = mp.unpublish(entry_id, tenant=self._tenant(params))
        logger.info("marketplace.uninstall: id=%s success=%s", entry_id, ok)
        return {"success": ok}

    async def _handle_persona_pack_publish(self, params: dict) -> dict:
        from ..persona_pack import build_manifest

        pack_dir = str(params.get("pack_dir", "") or "")
        if not pack_dir:
            return {"status": "error", "message": "pack_dir parameter required"}
        try:
            build_manifest(
                pack_dir,
                layer=str(params.get("layer", "") or ""),
                name=str(params.get("name", "") or ""),
                version=str(params.get("version", "1.0.0") or "1.0.0"),
                visibility=str(params.get("visibility", "public") or "public"),
            )
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        ppm = self._daemon._get_persona_pack_manager()
        pack_id = ppm.publish(pack_dir, tenant=self._tenant(params))
        logger.info("marketplace.persona_pack_publish: id=%s dir=%s", pack_id, pack_dir)
        return {"pack_id": pack_id}

    async def _handle_persona_pack_install(self, params: dict) -> dict:
        pack_id = str(params.get("pack_id", "") or "")
        if not pack_id:
            return {"status": "error", "message": "pack_id parameter required"}
        ppm = self._daemon._get_persona_pack_manager()
        result = ppm.install(
            pack_id,
            tenant=self._tenant(params),
            layer_name=str(params.get("layer_name", "") or ""),
        )
        if result.get("status") == "error":
            logger.warning("marketplace.persona_pack_install refused: %s", result)
            return result
        logger.info("marketplace.persona_pack_install: id=%s path=%s",
                    pack_id, result.get("path"))
        return result

    async def _handle_persona_pack_list(self, params: dict) -> dict:
        ppm = self._daemon._get_persona_pack_manager()
        return {"packs": ppm.list(tenant=self._tenant(params))}

    async def _handle_marketplace_audit(self, params: dict) -> dict:
        mp = self._daemon._get_marketplace()
        tenant = params.get("tenant")
        records = mp.audit_records(
            tenant=str(tenant) if tenant is not None else None,
            limit=int(params.get("limit", 100) or 100),
        )
        return {"records": records}

    # ── Chat Session Handlers ──
