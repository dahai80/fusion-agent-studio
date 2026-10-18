"""Sub-dispatcher: MemoryDispatcher."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


def _supported_kwargs(fn: Callable, kwargs: dict[str, Any]) -> dict[str, Any]:
    # N15 #390: local MemoryEngine grew tenant/team/decay params while the
    # fusion-memory adapter did not. Drop unsupported keywords instead of
    # pinning every caller to the newer signature.
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in sig.parameters}


class MemoryDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "memory.store": self._handle_memory_store,
            "memory.recall": self._handle_memory_recall,
            "memory.list_recent": self._handle_memory_list_recent,
            "memory.get": self._handle_memory_get,
            "memory.delete": self._handle_memory_delete,
            "memory.delete_scope": self._handle_memory_delete_scope,
            "memory.count": self._handle_memory_count,
            "memory.recall_relevant": self._handle_memory_recall_relevant,
            "memory.auto_forget": self._handle_memory_auto_forget,
            "memory.decay_sweep": self._handle_memory_decay_sweep,
            "memory.cleanup_suggestions": self._handle_memory_cleanup_suggestions,
            "memory.conflicts": self._handle_memory_conflicts,
            "memory.backend": self._handle_memory_backend,
            "memory.pack_export": self._handle_memory_pack_export,
            "memory.pack_import": self._handle_memory_pack_import,
        }

    async def _handle_memory_store(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"status": "error", "message": "content parameter required"}
        mem = self._daemon._get_memory()
        scope = params.get("scope", "default")
        tenant_id = params.get("tenant_id", "")
        team_id = params.get("team_id", "")
        entry_id = mem.store(**_supported_kwargs(mem.store, {
            "content": content,
            "scope": scope,
            "tags": params.get("tags", ""),
            "importance": params.get("importance", 5),
            "metadata": params.get("metadata"),
            "tier": params.get("tier", ""),
            "memory_type": params.get("memory_type", ""),
            "tenant_id": tenant_id,
            "team_id": team_id,
        }))
        self._audit_write(
            "memory.store",
            entry_id,
            {"scope": scope, "tenant_id": tenant_id, "team_id": team_id},
        )
        logger.info(
            "memory.store: entry_id=%s scope=%s tenant=%s team=%s",
            entry_id,
            scope,
            tenant_id,
            team_id,
        )
        return {"entry_id": entry_id}

    def _audit_write(self, action: str, resource_id: str, details: dict) -> None:
        # N15 #390: memory writes are auditable (who/when/which scope).
        get_audit = getattr(self._daemon, "_get_audit_logger", None)
        if get_audit is None:
            return
        try:
            get_audit().log_action(
                actor_id=str(details.get("actor_id", "rpc") or "rpc"),
                action=action,
                resource_type="memory",
                resource_id=resource_id,
                details=details,
                result="success",
            )
        except Exception as e:
            logger.warning("memory audit log failed action=%s id=%s err=%s", action, resource_id, e)

    async def _handle_memory_recall(self, params: dict) -> dict:
        query = params.get("query", "")
        mem = self._daemon._get_memory()
        entries = mem.recall(**_supported_kwargs(mem.recall, {
            "query": query,
            "scope": params.get("scope", ""),
            "limit": params.get("limit", 10),
            "min_importance": params.get("min_importance", 0),
            "tier": params.get("tier", ""),
            "memory_type": params.get("memory_type", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
        }))
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_list_recent(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        entries = mem.list_recent(**_supported_kwargs(mem.list_recent, {
            "scope": params.get("scope", ""),
            "limit": params.get("limit", 20),
            "min_importance": params.get("min_importance", 0),
            "tier": params.get("tier", ""),
            "memory_type": params.get("memory_type", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
        }))
        return {"entries": [e.to_dict() for e in entries]}

    async def _handle_memory_get(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._daemon._get_memory()
        entry = mem.get(entry_id)
        if entry is None:
            return {"status": "error", "message": f"Entry not found: {entry_id}"}
        return {"entry": entry.to_dict()}

    async def _handle_memory_delete(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        mem = self._daemon._get_memory()
        deleted = mem.delete(entry_id)
        if deleted:
            self._audit_write("memory.delete", entry_id, {"tenant_id": params.get("tenant_id", "")})
        return {"deleted": deleted}

    async def _handle_memory_delete_scope(self, params: dict) -> dict:
        scope = params.get("scope", "")
        if not scope:
            return {"status": "error", "message": "scope parameter required"}
        mem = self._daemon._get_memory()
        count = mem.delete_scope(scope)
        self._audit_write(
            "memory.delete_scope", scope,
            {"tenant_id": params.get("tenant_id", ""), "deleted_count": count},
        )
        return {"deleted_count": count}

    async def _handle_memory_count(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        count = mem.count(**_supported_kwargs(mem.count, {
            "scope": params.get("scope", ""),
            "tier": params.get("tier", ""),
            "memory_type": params.get("memory_type", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
        }))
        return {"count": count}

    async def _handle_memory_recall_relevant(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        query = params.get("query", "")
        limit = params.get("limit", 5)
        scope = params.get("scope", "")
        memory_type = params.get("memory_type", "")
        result = mem.recall_relevant(
            query=query, limit=limit, scope=scope, memory_type=memory_type
        )
        return {"context": result}

    async def _handle_memory_auto_forget(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        max_entries = params.get("max_entries", 1000)
        min_importance = params.get("min_importance", 3)
        removed = mem.auto_forget(
            max_entries=max_entries, min_importance=min_importance
        )
        if removed:
            self._audit_write("memory.auto_forget", "bulk", {"removed": removed})
        return {"removed": removed}

    async def _handle_memory_decay_sweep(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        sweep = getattr(mem, "sweep", None)
        if sweep is None:
            return {"status": "error", "message": "memory backend has no sweep support"}
        result = sweep(**_supported_kwargs(sweep, {
            "dry_run": bool(params.get("dry_run", True)),
            "threshold": float(params.get("threshold", 0.5)),
            "max_age_days": float(params.get("max_age_days", 0)),
            "half_life_days": float(params.get("half_life_days", 14.0)),
            "scope": params.get("scope", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
            "tier": params.get("tier", ""),
            "memory_type": params.get("memory_type", ""),
            "include_summaries": bool(params.get("include_summaries", False)),
        }))
        if result.get("deleted"):
            self._audit_write("memory.decay_sweep", "bulk", {"deleted": result["deleted"]})
        return result

    async def _handle_memory_cleanup_suggestions(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        suggest = getattr(mem, "cleanup_suggestions", None)
        if suggest is None:
            return {"status": "error", "message": "memory backend has no sweep support"}
        return suggest(**_supported_kwargs(suggest, {
            "threshold": float(params.get("threshold", 0.5)),
            "max_age_days": float(params.get("max_age_days", 0)),
            "half_life_days": float(params.get("half_life_days", 14.0)),
            "scope": params.get("scope", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
        }))

    async def _handle_memory_conflicts(self, params: dict) -> dict:
        mem = self._daemon._get_memory()
        detect = getattr(mem, "detect_conflicts", None)
        if detect is None:
            return {"status": "error", "message": "memory backend has no conflict support"}
        conflicts = detect(**_supported_kwargs(detect, {
            "scope": params.get("scope", ""),
            "tenant_id": params.get("tenant_id", ""),
            "team_id": params.get("team_id", ""),
            "limit": int(params.get("limit", 200)),
            "threshold": float(params.get("threshold", 0.6)),
        }))
        return {"conflicts": conflicts, "count": len(conflicts)}

    async def _handle_memory_pack_export(self, params: dict) -> dict:
        from agent_runtime.memory_pack import export_pack

        out_dir = params.get("out_dir", "")
        if not out_dir:
            return {"status": "error", "message": "out_dir parameter required"}
        mem = self._daemon._get_memory()
        return export_pack(mem, out_dir, scope=params.get("scope", ""))

    async def _handle_memory_pack_import(self, params: dict) -> dict:
        from agent_runtime.memory_pack import import_pack

        in_dir = params.get("in_dir", "")
        if not in_dir:
            return {"status": "error", "message": "in_dir parameter required"}
        mem = self._daemon._get_memory()
        return import_pack(mem, in_dir, scope=params.get("scope", ""))

    async def _handle_memory_backend(self, params: dict) -> dict:
        # issue #246 后续: 暴露当前记忆后端 (fusion_memory adapter 或 legacy sqlite),
        # 供运维查活服务到底走的哪条路。鸭子类型按类名判, 免跨模块 import。
        mem = self._daemon._get_memory()
        cls_name = type(mem).__name__
        if cls_name == "FusionMemoryAdapter":
            backend = "fusion_memory"
            base_url = getattr(mem, "_base_url", "")
            api_key_configured = bool(getattr(mem, "_api_key", ""))
        else:
            backend = "sqlite"
            # MemoryEngine.db_path 是 PosixPath, JSON 不序列化 -> str()。
            base_url = str(getattr(mem, "db_path", ""))
            api_key_configured = False
        logger.info("memory.backend: %s base=%s", backend, base_url)
        return {
            "backend": backend,
            "base_url": base_url,
            "api_key_configured": api_key_configured,
        }

    # ── Safety handlers ──
