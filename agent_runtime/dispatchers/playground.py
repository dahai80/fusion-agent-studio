"""E6: playground + asset registry RPCs — playground.replay / asset.*."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class PlaygroundDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "playground.replay": self._handle_replay,
            "playground.fromTrace": self._handle_from_trace,
            "asset.put": self._handle_asset_put,
            "asset.list": self._handle_asset_list,
            "asset.checkout": self._handle_asset_checkout,
        }

    def _registry(self):
        from ..asset_registry import AssetRegistry

        reg = getattr(self._daemon, "_asset_registry", None)
        if reg is None:
            reg = AssetRegistry()
            self._daemon._asset_registry = reg
        return reg

    def _playground(self):
        from ..playground import Playground

        client = getattr(self._daemon, "mlx_client", None)
        if client is None:
            raise RuntimeError("no mlx client attached to daemon")
        return Playground(client)

    async def _handle_replay(self, params: dict) -> dict:
        messages = params.get("messages")
        if not isinstance(messages, list) or not messages:
            return {"error": "messages (non-empty list) required"}
        try:
            pg = self._playground()
        except RuntimeError as e:
            return {"error": str(e)}
        return await pg.replay(
            messages,
            system=str(params.get("system", "") or ""),
            params=params.get("params") if isinstance(params.get("params"), dict) else None,
        )

    async def _handle_from_trace(self, params: dict) -> dict:
        trace_id = str(params.get("trace_id", "") or "")
        if not trace_id:
            return {"error": "trace_id required"}
        try:
            pg = self._playground()
        except RuntimeError as e:
            return {"error": str(e)}
        return await pg.from_trace(
            trace_id,
            system=str(params.get("system", "") or ""),
            params=params.get("params") if isinstance(params.get("params"), dict) else None,
            prompt_override=str(params.get("prompt", "") or ""),
        )

    async def _handle_asset_put(self, params: dict) -> dict:
        kind = str(params.get("kind", "") or "")
        name = str(params.get("name", "") or "")
        content = params.get("content")
        if content is None:
            return {"error": "content required"}
        try:
            rec = self._registry().put(kind, name, str(content), created_by=str(params.get("created_by", "") or ""))
        except ValueError as e:
            return {"error": str(e)}
        logger.info("asset.put %s/%s v%s", kind, name, rec.get("version"))
        return rec

    async def _handle_asset_list(self, params: dict) -> dict:
        return {
            "assets": self._registry().list(
                kind=str(params.get("kind", "") or "") or None,
                name=str(params.get("name", "") or "") or None,
            )
        }

    async def _handle_asset_checkout(self, params: dict) -> dict:
        kind = str(params.get("kind", "") or "")
        name = str(params.get("name", "") or "")
        version = params.get("version")
        rec = self._registry().checkout(kind, name, version)
        if rec is None:
            return {"error": f"asset not found: {kind}/{name}" + (f"@{version}" if version else "")}
        return rec
