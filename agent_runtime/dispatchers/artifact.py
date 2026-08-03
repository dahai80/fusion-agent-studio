"""Artifact dispatcher — expose artifact bridge RPC methods via DaemonServer.

Importers: dispatchers/__init__.py, daemon_server.py (_init_sub_dispatchers)
API: artifact.create/load/patch/list_all/snapshot/context_budget/auto_compact/ping_remote
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class ArtifactDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "artifact.create": self._handle_create,
            "artifact.load": self._handle_load,
            "artifact.patch": self._handle_patch,
            "artifact.list_all": self._handle_list_all,
            "artifact.snapshot": self._handle_snapshot,
            "artifact.context_budget": self._handle_context_budget,
            "artifact.auto_compact": self._handle_auto_compact,
            "artifact.ping_remote": self._handle_ping_remote,
        }

    def _get_bridge(self):
        return self._daemon._get_artifact_manager()

    async def _handle_create(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.create(
                name=params.get("name", ""),
                artifact_type=params.get("type", "document"),
                content=params.get("content", ""),
                agent_id=params.get("session_id", ""),
                metadata=params.get("metadata"),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.create failed: %s", e)
            return self._err(str(e))

    async def _handle_load(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.load(
                artifact_id=params.get("artifact_id", ""),
                preview_only=params.get("preview_only", False),
                section=params.get("section", ""),
                max_tokens=params.get("max_tokens", 0),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.load failed: %s", e)
            return self._err(str(e))

    async def _handle_patch(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.patch(
                artifact_id=params.get("artifact_id", ""),
                operation=params.get("operation", ""),
                content=params.get("content", ""),
                section=params.get("section", ""),
                agent_id=params.get("session_id", ""),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.patch failed: %s", e)
            return self._err(str(e))

    async def _handle_list_all(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.list_all(
                agent_id=params.get("session_id", ""),
                page=params.get("page", 1),
                page_size=params.get("page_size", 20),
                sort=params.get("sort", "updated_at"),
                filters=params.get("filters"),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.list_all failed: %s", e)
            return self._err(str(e))

    async def _handle_snapshot(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.snapshot(
                artifact_id=params.get("artifact_id", ""),
                label=params.get("label", ""),
                author=params.get("author", ""),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.snapshot failed: %s", e)
            return self._err(str(e))

    async def _handle_context_budget(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.context_budget(
                agent_id=params.get("session_id", ""),
                context_window=params.get("context_window"),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.context_budget failed: %s", e)
            return self._err(str(e))

    async def _handle_auto_compact(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            result = await bridge.auto_compact(
                artifact_id=params.get("artifact_id", ""),
                token_budget=params.get("token_budget", 0),
            )
            return result
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.auto_compact failed: %s", e)
            return self._err(str(e))

    async def _handle_ping_remote(self, params: dict) -> dict:
        bridge = self._get_bridge()
        try:
            available = await bridge.check_remote()
            return {"remote_available": available, "url": bridge.remote_url}
        except (ValueError, TypeError, RuntimeError, OSError) as e:
            logger.error("artifact.ping_remote failed: %s", e)
            return self._err(str(e))
