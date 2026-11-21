"""Artifact bridge — adapt local ArtifactManager to remote artifacts-engine RPC with auto-fallback.

Importers: daemon_server.py (_get_artifact_manager), dispatchers/artifact.py
API: ArtifactBridge.create/load/patch/list_all/snapshot/context_budget/auto_compact
Data schemas: delegates to ArtifactManager (local) or JSON-RPC 2.0 to artifacts-engine (remote)

#714 part 2 annotation: record-RPC adaptation is studio-product behavior and
stays local; the kernel worker-output channel (artifact_channel.py shim) is
the ref-only mechanism for team/DAG member deliveries.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .artifact_tools import ArtifactManager
from .paths import studio_home

logger = logging.getLogger(__name__)

_DEFAULT_REMOTE_URL = "http://127.0.0.1:11451"
_RPC_TIMEOUT = 10.0

_quarantine: Any = None


def _get_quarantine() -> Any:
    # #770: lazy ArtifactQuarantine under studio home — materialized on the
    # first flagged load only (no-eager-mkdir, #591); clean loads never
    # touch the filesystem beyond the artifact store itself.
    global _quarantine
    if _quarantine is None:
        from fusion_harness.artifact_scan import ArtifactQuarantine

        # First flagged load — the persistence write is imminent, so
        # creating the studio home here is event-driven, not eager (#591).
        home = studio_home()
        home.mkdir(parents=True, exist_ok=True)
        path = str(home / "artifact_quarantine.json")
        _quarantine = ArtifactQuarantine(path=path)
        logger.info("artifact quarantine ready path=%s", path)
    return _quarantine


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
                return self._scan_loaded(
                    {"status": "ok", "source": "remote", **result}, artifact_id
                )
            except (httpx.HTTPError, RuntimeError, OSError) as e:
                logger.warning("artifact.load remote failed, fallback local: %s", e)
        result = self.local.load_artifact(
            artifact_id=artifact_id,
            preview_only=preview_only,
            section=section,
            max_tokens=max_tokens,
        )
        result["source"] = "local"
        return self._scan_loaded(result, artifact_id)

    def _scan_loaded(self, result: dict[str, Any], artifact_id: str) -> dict[str, Any]:
        # #770: pre-load scan boundary — every artifact body entering a
        # prompt passes scan_artifact (kernel regex sets: prompt injection
        # + credential exfiltration). Flagged content is quarantined and
        # returned STAMPED (quoted + origin marker) rather than dropped, so
        # the caller sees both the content and its untrusted status; the
        # scan report rides the result as artifact_scan metadata. Scan
        # failure degrades to unscanned pass-through with a loud log —
        # availability over silent loss, matching bridge fallback style.
        if result.get("status") != "ok":
            return result
        content = result.get("content")
        if not isinstance(content, str) or not content:
            return result
        try:
            from fusion_harness.artifact_scan import scan_artifact, stamp_untrusted
        except ImportError as e:
            logger.error("artifact_scan unavailable, load unscanned: %s", e)
            return result
        report = scan_artifact(content, source=artifact_id, kind="artifact")
        result["artifact_scan"] = {"clean": report.clean, "rules": report.rule_names()}
        if report.clean:
            return result
        logger.warning(
            "artifact load flagged id=%s rules=%s quarantined=true",
            artifact_id,
            ",".join(report.rule_names()),
        )
        entry = _get_quarantine().add(report, preview=content[:200])
        result["artifact_scan"]["quarantine_id"] = entry["id"]
        result["content"] = stamp_untrusted(content, artifact_id, report.rule_names())
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
        return self.local.get_active_artifacts_context_budget_aware(agent_id, context_window, limit)

    def advance_generation_phase(self, artifact_id: str, target_phase: str) -> dict[str, Any]:
        return self.local.advance_generation_phase(artifact_id, target_phase)
