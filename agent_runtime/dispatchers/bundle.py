"""Sub-dispatcher: BundleDispatcher — full-bundle packages (#431)."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class BundleDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "bundle.install": self._handle_install,
            "bundle.uninstall": self._handle_uninstall,
            "bundle.list": self._handle_list,
            "bundle.get": self._handle_get,
        }

    def _installer(self):
        from ..bundle_package import BundleInstaller
        from ..skills_store import SkillsStore

        cached = getattr(self._daemon, "_bundle_installer", None)
        if cached is not None:
            return cached
        daemon = self._daemon

        async def _mcp_register(config: dict):
            handler = daemon._get_handler("mcp.register_server")
            if handler is None:
                raise RuntimeError("mcp.register_server unavailable")
            return await handler(config)

        async def _mcp_unregister(server: str):
            handler = daemon._get_handler("mcp.unregister_server")
            if handler is None:
                return None
            return await handler({"server_url": server})

        skills = getattr(daemon, "_skills_store", None) or SkillsStore.with_project_dirs()
        cached = BundleInstaller(
            home_dir=getattr(daemon, "_auth_dir", "") or None,
            skills_store=skills,
            hook_engine=daemon._get_hooks(),
            connector_manager=daemon._get_connector_manager(),
            store=daemon.store,
            mcp_register=_mcp_register,
            mcp_unregister=_mcp_unregister,
        )
        daemon._bundle_installer = cached
        return cached

    async def _handle_install(self, params: dict) -> dict:
        path = str(params.get("path", "") or "")
        if not path:
            return {"status": "error", "message": "path required"}
        result = await self._installer().install(path)
        logger.info(
            "bundle.install path=%s ok=%s name=%s",
            path,
            result.get("ok"),
            result.get("name", ""),
        )
        return {"status": "ok" if result.get("ok") else "error", **result}

    async def _handle_uninstall(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        if not name:
            return {"status": "error", "message": "name required"}
        result = await self._installer().uninstall(name)
        return {"status": "ok" if result.get("ok") else "error", **result}

    async def _handle_list(self, params: dict) -> dict:
        rows = self._installer().list()
        return {"status": "ok", "bundles": rows, "count": len(rows)}

    async def _handle_get(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        record = self._installer().get(name)
        if record is None:
            return {"status": "error", "message": f"bundle not installed: {name}"}
        return {"status": "ok", "bundle": record}
