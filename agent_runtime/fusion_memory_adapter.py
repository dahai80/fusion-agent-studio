"""Fusion-memory adapter — replaces MemoryEngine storage backend with a proxy to
the fusion-memory HTTP JSON-RPC 2.0 hub (fm-server, 127.0.0.1).

Preserves the MemoryEngine *method surface* (9 methods called by MemoryDispatcher,
AgentRuntime.recall_relevant, Compactor.store_summary) so daemon + callers see no
API change. Storage moves off local SQLite memory.db to fusion-memory's episodic
commit / semantic retrieve / forgetting-curve consolidation + entity graph.

PRD §10.1 / issue #246. 100% offline — HTTP only to 127.0.0.1.
Synchronous HTTP (httpx.Client): MemoryDispatcher calls mem.store() synchronously
(memory.py:32) and runtime.recall_relevant runs via asyncio.to_thread — hence a
synchronous client.

env (operator):
  FUSION_MEMORY_BASE_URL  (default http://127.0.0.1:11435)
  FUSION_MEMORY_API_KEY   (required, aligned with fm-server Bearer B5)

Handler -> RPC mapping (see issue #246 table):
  store         -> commit      (content wrapped into a single-turn Interaction)
  recall        -> retrieve    (blocks -> MemoryEntry[])
  list_recent   -> retrieve    (degraded, recency approximation)
  get           -> get         (direct)
  delete        -> delete      (confirm=True, B5)
  delete_scope  -> (no mapping) (degrade: no-op + log)
  count         -> retrieve    (approximate: len(blocks), fm has no list-all-ids RPC)
  recall_relevant -> retrieve  (blocks -> formatted string)
  auto_forget   -> consolidate (remote equivalent, returns dropped count)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .memory_engine import MemoryEntry

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11435"
DEFAULT_TIMEOUT = 10.0


class FusionMemoryAdapter:
    """fusion-memory HTTP proxy, duck-typed to the MemoryEngine 9-method surface.
    Fails empty on error (log + empty return); never raises to interrupt the daemon
    main flow.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("FUSION_MEMORY_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("FUSION_MEMORY_API_KEY")
        if not self._api_key:
            raise RuntimeError("FUSION_MEMORY_API_KEY not configured (fm-server Bearer auth, B5)")
        self._timeout = timeout
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        logger.info("FusionMemoryAdapter created at %s", self._base_url)

    # ── RPC core ───────────────────────────────────────────────

    def _rpc(self, method: str, params: dict[str, Any]) -> Any | None:
        try:
            resp = self._client.post(
                f"/v1/memory/{method}",
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "fusion-memory %s HTTP %d %s",
                    method,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            data = resp.json()
            if data.get("error"):
                err = data["error"]
                logger.warning(
                    "fusion-memory %s RPC %s: %s",
                    method,
                    err.get("code"),
                    err.get("message"),
                )
                return None
            return data.get("result")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("fusion-memory %s error: %s", method, exc)
            return None

    # ── MemoryEngine surface (9 methods) ───────────────────────

    def store(
        self,
        content: str,
        scope: str = "default",
        tags: str = "",
        importance: int = 5,
        metadata: dict[str, Any] | None = None,
        tier: str = "",
        is_summary: bool = False,
        memory_type: str = "",
    ) -> str:
        interaction = {
            "id": f"store-{int(time.time() * 1000)}",
            "session_id": scope,
            "turns": [
                {
                    "turn_idx": 0,
                    "user_message": "",
                    "assistant_message": content,
                    "tool_calls": [],
                }
            ],
            "timestamp": int(time.time()),
            "metadata": {
                "scope": scope,
                "tags": tags,
                "importance": importance,
                "tier": tier,
                "is_summary": is_summary,
                "memory_type": memory_type,
                **(metadata or {}),
            },
        }
        ids = self._rpc("commit", {"session_id": scope, "interaction": interaction})
        if not ids:
            logger.warning("fusion-memory store failed for scope %s", scope)
            return ""
        logger.debug("fusion-memory stored id=%s scope=%s", ids[0], scope)
        return ids[0]

    def recall(
        self,
        query: str,
        scope: str = "",
        limit: int = 10,
        min_importance: int = 0,
        tier: str = "",
        memory_type: str = "",
    ) -> list[MemoryEntry]:
        return self._retrieve_to_entries(
            query=query, top_k=limit, scope=scope, memory_type=memory_type
        )

    def list_recent(
        self,
        scope: str = "",
        limit: int = 20,
        min_importance: int = 0,
        tier: str = "",
        memory_type: str = "",
    ) -> list[MemoryEntry]:
        # fm has no recency-only RPC; retrieve with an empty query is not viable, so
        # use scope as the query to approximate the most recent entries.
        # degraded: per issue #246, recency approximation.
        query = scope if scope else "*"
        return self._retrieve_to_entries(
            query=query, top_k=limit, scope=scope, memory_type=memory_type
        )

    def get(self, entry_id: str) -> MemoryEntry | None:
        # fm-server has no POST /v1/memory/get; get goes through the GET
        # /v1/memory/{id} path parameter (http.rs get_memory, server-side JSON-RPC
        # envelope). Client sends GET + Bearer and unwraps result.
        try:
            resp = self._client.get(f"/v1/memory/{entry_id}")
        except httpx.HTTPError as exc:
            logger.warning("fusion-memory get(%s) error: %s", entry_id, exc)
            return None
        if resp.status_code >= 400:
            logger.warning(
                "fusion-memory get(%s) HTTP %d %s",
                entry_id,
                resp.status_code,
                resp.text[:200],
            )
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("fusion-memory get(%s) bad json: %s", entry_id, exc)
            return None
        if data.get("error"):
            err = data["error"]
            logger.warning(
                "fusion-memory get(%s) RPC %s: %s",
                entry_id,
                err.get("code"),
                err.get("message"),
            )
            return None
        item = data.get("result")
        if not item:
            return None
        return self._item_to_entry(item)

    def delete(self, entry_id: str) -> bool:
        res = self._rpc("delete", {"id": entry_id, "confirm": True})
        deleted = res is not None
        if deleted:
            logger.debug("fusion-memory deleted %s", entry_id)
        return deleted

    def delete_scope(self, scope: str) -> int:
        # fm has no scope-delete RPC (issue #246: degrade to no-op + log).
        logger.warning(
            "fusion-memory delete_scope('%s') no-op — fm has no scope-delete RPC",
            scope,
        )
        return 0

    def count(self, scope: str = "", tier: str = "", memory_type: str = "") -> int:
        # fm has no list-all-ids RPC, so an exact count is impossible. Approximate:
        # retrieve with a large top_k and use len(blocks).
        # issue #246: count via get-accumulate (fm get is single-id, no enumerate,
        # hence the retrieve approximation).
        query = scope if scope else "*"
        result = self._rpc(
            "retrieve",
            {"text": query, "top_k": 1000, "token_budget": 100000, "aggregate": False},
        )
        if not result:
            return 0
        blocks = result.get("blocks", []) if isinstance(result, dict) else []
        return len(blocks)

    def recall_relevant(
        self, query: str, limit: int = 5, scope: str = "", memory_type: str = ""
    ) -> str:
        result = self._rpc(
            "retrieve",
            {
                "text": query,
                "top_k": limit,
                "token_budget": 4096,
                "aggregate": True,
                "session_id": scope or None,
            },
        )
        if not result:
            return ""
        return self._format_context(result)

    def auto_forget(self, max_entries: int = 1000, min_importance: int = 3) -> int:
        report = self._rpc("consolidate", {})
        if not report:
            return 0
        dropped = report.get("dropped", 0) if isinstance(report, dict) else 0
        logger.info("fusion-memory auto_forget dropped %d (consolidate)", dropped)
        return dropped

    # -- Helper: Compat (Compactor.store_summary calls store; no dedicated method) --

    def store_summary(self, summary: str, scope: str, original_count: int) -> str:
        return self.store(
            content=summary,
            scope=scope,
            tags="auto-summary",
            importance=3,
            metadata={"original_count": original_count, "type": "summary"},
            is_summary=True,
        )

    def close(self) -> None:
        self._client.close()

    # -- Internal mapping ------------------------------------------------------

    def _retrieve_to_entries(
        self,
        query: str,
        top_k: int,
        scope: str = "",
        memory_type: str = "",
    ) -> list[MemoryEntry]:
        params: dict[str, Any] = {
            "text": query,
            "top_k": top_k,
            "token_budget": 4096,
            "aggregate": True,
        }
        if scope:
            params["session_id"] = scope
        result = self._rpc("retrieve", params)
        if not result:
            return []
        blocks = result.get("blocks", []) if isinstance(result, dict) else []
        return [self._block_to_entry(b) for b in blocks]

    def _item_to_entry(self, item: dict[str, Any]) -> MemoryEntry:
        return MemoryEntry(
            id=item.get("id", ""),
            content=item.get("content", ""),
            scope=item.get("scope", "default"),
            tags=item.get("tags", ""),
            importance=item.get("weight", 5),
            created_at=item.get("last_accessed_timestamp", time.time()),
            metadata=item.get("metadata", {}),
            tier=item.get("tier", "long_term"),
            memory_type=item.get("memory_type", "Episodic"),
        )

    def _block_to_entry(self, block: dict[str, Any]) -> MemoryEntry:
        text = block.get("turns_text", "")
        mem_type = block.get("memory_type", "Episodic")
        # fm memory_type uppercase enum (Episodic/Semantic/Procedural) -> agent-studio
        # lowercase (user/feedback/project/reference) approximate mapping;
        # Semantic->project, the rest->project.
        local_type = "project"
        return MemoryEntry(
            id=block.get("interaction_id", ""),
            content=text,
            scope="default",
            tags="",
            importance=max(1, int(block.get("score", 0.5) * 10)),
            created_at=time.time(),
            metadata={
                "score": block.get("score", 0.0),
                "source_entities": block.get("source_entities", []),
                "fm_memory_type": mem_type,
            },
            tier="long_term",
            memory_type=local_type,
        )

    def _format_context(self, result: dict[str, Any]) -> str:
        blocks = result.get("blocks", [])
        if not blocks:
            return ""
        parts = []
        for b in blocks:
            score = b.get("score", 0.0)
            text = b.get("turns_text", "")
            mem_type = b.get("memory_type", "")
            parts.append(f"[记忆 (相关度: {score:.0%}, {mem_type})]\n{text}")  # i18n: user-facing
        return "\n".join(parts)

    # -- Compat property: AgentRuntime/daemon may access .db_path --------------

    @property
    def db_path(self) -> str:
        return self._base_url
