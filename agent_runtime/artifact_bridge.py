"""Artifact bridge — adapt local ArtifactManager to remote artifacts-engine RPC with auto-fallback.

Importers: daemon_server.py (_get_artifact_manager), dispatchers/artifact.py
API: ArtifactBridge.create/load/patch/list_all/snapshot/context_budget/auto_compact
Data schemas: delegates to ArtifactManager (local) or JSON-RPC 2.0 to artifacts-engine (remote)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .artifact_tools import ArtifactManager

logger = logging.getLogger(__name__)

_DEFAULT_REMOTE_URL = "http://127.0.0.1:11451"
_RPC_TIMEOUT = 10.0


class ArtifactBridge:
    """Wraps ArtifactManager with remote RPC calls to fusion-artifacts-engine.
    Tries remote first; falls back to local on any failure.
    """

    def __init__(
        self,
        local_manager: ArtifactManager | None = None,
        remote_url: str = _DEFAULT_REMOTE_URL,
    ):
        self.local = local_manager or ArtifactManager()
        self.remote_url = remote_url.rstrip("/")
        self._remote_available: bool | None = None

    async def _rpc(self, method: str, params: dict | None = None) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
            resp = await client.post(
                f"{self.remote_url}/rpc",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result", {})

    async def check_remote(self) -> bool:
        try:
            result = await self._rpc("ping")
            self._remote_available = result.get("pong", False)
        except (httpx.HTTPError, RuntimeError, OSError) as e:
            logger.debug("artifact remote ping failed: %s", e)
            self._remote_available = False
        return bool(self._remote_available)

    # ── AS-2: create ──────────────────────────────────────────────

    async def create(
        self,
        name: str,
        artifact_type: str,
        content: str,
        agent_id: str = "",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                result = await self._rpc(
                    "artifact.create",
                    {
                        "session_id": agent_id,
                        "name": name,
                        "type": artifact_type,
                        "content": content,
                        "metadata": metadata or {},
                    },
                )
                logger.info("artifact.create via remote: name=%s", name)
                return {"status": "ok", "source": "remote", "result": result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.create remote failed, fallback local: %s", e)
        result = self.local.create_artifact(
            name=name,
            artifact_type=artifact_type,
            content=content,
            agent_id=agent_id,
            metadata=metadata or {},
        )
        result["source"] = "local"
        return result

    # ── AS-1: load ────────────────────────────────────────────────

    async def load(
        self,
        artifact_id: str,
        preview_only: bool = False,
        section: str = "",
        max_tokens: int = 0,
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                params: dict[str, Any] = {
                    "artifact_id": artifact_id,
                    "preview_only": preview_only,
                }
                if section:
                    params["section"] = section
                result = await self._rpc("artifact.load", params)
                logger.info("artifact.load via remote: id=%s", artifact_id)
                return {"status": "ok", "source": "remote", **result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.load remote failed, fallback local: %s", e)
        result = self.local.load_artifact(
            artifact_id=artifact_id,
            preview_only=preview_only,
            section=section,
            max_tokens=max_tokens,
        )
        result["source"] = "local"
        return result

    # ── AS-3: patch ───────────────────────────────────────────────

    async def patch(
        self,
        artifact_id: str,
        operation: str,
        content: str = "",
        section: str = "",
        agent_id: str = "",
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        op_map = {
            "replace": "replace_section",
            "append": "append",
            "prepend": "prepend",
            "section_replace": "replace_section",
            "delete_section": "delete_section",
        }
        remote_op = op_map.get(operation, operation)
        if self._remote_available:
            try:
                params: dict[str, Any] = {
                    "artifact_id": artifact_id,
                    "operation": remote_op,
                    "content": content,
                }
                if section:
                    params["anchor"] = section
                result = await self._rpc("artifact.patch", params)
                logger.info("artifact.patch via remote: id=%s op=%s", artifact_id, operation)
                return {"status": "ok", "source": "remote", "result": result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.patch remote failed, fallback local: %s", e)
        result = self.local.patch_artifact(
            artifact_id=artifact_id,
            operation=operation,
            content=content,
            section=section,
            agent_id=agent_id,
        )
        result["source"] = "local"
        return result

    # ── AS-5: list_all ────────────────────────────────────────────

    async def list_all(
        self,
        agent_id: str = "",
        page: int = 1,
        page_size: int = 20,
        sort: str = "updated_at",
        filters: dict | None = None,
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                result = await self._rpc(
                    "artifact.list_all",
                    {"page": page, "page_size": page_size, "sort": sort, "filters": filters},
                )
                logger.info("artifact.list_all via remote: page=%d", page)
                return {"status": "ok", "source": "remote", **result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.list_all remote failed, fallback local: %s", e)
        artifacts = self.local.list_artifacts(agent_id)
        return {
            "status": "ok",
            "source": "local",
            "artifacts": artifacts,
            "total": len(artifacts),
        }

    # ── AS-4: snapshot ────────────────────────────────────────────

    async def snapshot(
        self,
        artifact_id: str,
        label: str = "",
        author: str = "",
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                params: dict[str, Any] = {"artifact_id": artifact_id}
                if label:
                    params["label"] = label
                if author:
                    params["author"] = author
                result = await self._rpc("artifact.create_snapshot", params)
                logger.info("artifact.snapshot via remote: id=%s", artifact_id)
                return {"status": "ok", "source": "remote", "result": result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.snapshot remote failed: %s", e)
        return {"status": "error", "message": "snapshots require remote artifacts-engine"}

    # ── AS-6: context_budget ──────────────────────────────────────

    async def context_budget(
        self,
        agent_id: str = "",
        context_window: int | None = None,
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                params: dict[str, Any] = {}
                if agent_id:
                    params["session_id"] = agent_id
                if context_window is not None:
                    params["context_window"] = context_window
                result = await self._rpc("context.budget", params)
                logger.info("context.budget via remote: agent=%s", agent_id)
                return {"status": "ok", "source": "remote", **result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("context.budget remote failed, fallback local: %s", e)
        result = self.local.get_context_budget(agent_id=agent_id)
        result["source"] = "local"
        return result

    # ── AS-7: auto_compact ────────────────────────────────────────

    async def auto_compact(
        self,
        artifact_id: str,
        token_budget: int = 0,
    ) -> dict[str, Any]:
        if self._remote_available is None:
            await self.check_remote()
        if self._remote_available:
            try:
                result = await self._rpc(
                    "artifact.auto_compact",
                    {"artifact_id": artifact_id, "token_budget": token_budget},
                )
                logger.info("artifact.auto_compact via remote: id=%s", artifact_id)
                return {"status": "ok", "source": "remote", **result}
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.auto_compact remote failed: %s", e)
        return {"status": "error", "message": "auto_compact requires remote artifacts-engine"}

    # ── Local pass-through ────────────────────────────────────────

    def get_active_artifacts_context(self, agent_id: str, limit: int = 5) -> str:
        return self.local.get_active_artifacts_context(agent_id, limit)

    def get_active_artifacts_context_budget_aware(
        self,
        agent_id: str,
        context_window: int = 32768,
        limit: int = 5,
    ) -> dict[str, Any]:
        return self.local.get_active_artifacts_context_budget_aware(
            agent_id, context_window, limit
        )
