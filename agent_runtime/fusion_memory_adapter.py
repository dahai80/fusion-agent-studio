"""Fusion-memory adapter — replaces MemoryEngine storage backend with a proxy to
the fusion-memory HTTP JSON-RPC 2.0 hub (fm-server, 127.0.0.1).

Preserves the MemoryEngine *method surface* (9 methods called by MemoryDispatcher,
AgentRuntime.recall_relevant, Compactor.store_summary) so daemon + callers see no
API change. Storage moves off local SQLite memory.db to fusion-memory's episodic
commit / semantic retrieve / forgetting-curve consolidation + entity graph.

PRD §10.1 / issue #246。100% offline — HTTP only to 127.0.0.1。
同步 HTTP (httpx.Client): MemoryDispatcher 调用 mem.store() 是同步的
(memory.py:32), runtime.recall_relevant 走 asyncio.to_thread — 故用同步客户端。

env (operator):
  FUSION_MEMORY_BASE_URL  (默认 http://127.0.0.1:11435)
  FUSION_MEMORY_API_KEY   (必配, 对齐 fm-server Bearer B5)

Handler -> RPC 映射 (见 issue #246 表):
  store         -> commit      (content 包成单 turn Interaction)
  recall        -> retrieve    (blocks -> MemoryEntry[])
  list_recent   -> retrieve    (degraded, recency 近似)
  get           -> get         (direct)
  delete        -> delete      (confirm=True, B5)
  delete_scope  -> (无映射)    (degrade: no-op + log)
  count         -> retrieve    (近似: len(blocks), fm 无 list-all-ids RPC)
  recall_relevant -> retrieve  (blocks -> 格式化字符串)
  auto_forget   -> consolidate (remote 等价, 返回 dropped 数)
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
    """fusion-memory HTTP 代理, 鸭子类型 MemoryEngine 9 方法表面。
    失败 fail-empty (log + 空返回), 不抛异常中断 daemon 主流程。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("FUSION_MEMORY_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("FUSION_MEMORY_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "FUSION_MEMORY_API_KEY 未配置 (fm-server Bearer 鉴权, B5)"
            )
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
        # fm 无 recency-only RPC; retrieve 空查询不成立, 用 scope 作查询近似最近条目。
        # degraded: 按 issue #246, recency 近似。
        query = scope if scope else "*"
        return self._retrieve_to_entries(
            query=query, top_k=limit, scope=scope, memory_type=memory_type
        )

    def get(self, entry_id: str) -> MemoryEntry | None:
        # fm-server 无 POST /v1/memory/get; get 走 GET /v1/memory/{id} 路径参
        # (http.rs get_memory, 服务端组 JSON-RPC)。客户端发 GET + Bearer, 解 result。
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
        # fm 无 scope-delete RPC (issue #246: degrade no-op + log)。
        logger.warning(
            "fusion-memory delete_scope('%s') no-op — fm has no scope-delete RPC",
            scope,
        )
        return 0

    def count(self, scope: str = "", tier: str = "", memory_type: str = "") -> int:
        # fm 无 list-all-ids RPC, 无法精确 count。近似: retrieve 大 top_k, len(blocks)。
        # issue #246: count via get-accumulate (fm get 单 id, 无 enumerate, 故 retrieve 近似)。
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

    # ── 辅助: Compat (Compactor.store_summary 调 store, 无独立方法) ──

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

    # ── 内部映射 ────────────────────────────────────────────────

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
        # fm memory_type 大写枚举 (Episodic/Semantic/Procedural) -> agent-studio 小写
        # (user/feedback/project/reference) 近似映射; Semantic->project, 其余->project。
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
            parts.append(f"[记忆 (相关度: {score:.0%}, {mem_type})]\n{text}")
        return "\n".join(parts)

    # ── 兼容属性: AgentRuntime/daemon 可能访问 .db_path ──────────

    @property
    def db_path(self) -> str:
        return self._base_url
