"""#555: flow.* RPC handlers — web canvas flow documents + version snapshots."""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_runtime.flow_store import FlowStore, FlowStoreError, resolve_tenant_id

logger = logging.getLogger(__name__)


class FlowDispatcher:
    def __init__(self, daemon: Any):
        self._daemon = daemon

    def get_handlers(self) -> dict[str, Callable]:
        return {
            "flow.create": self._handle_flow_create,
            "flow.save": self._handle_flow_save,
            "flow.list": self._handle_flow_list,
            "flow.load": self._handle_flow_load,
            "flow.delete": self._handle_flow_delete,
            "flow.versions": self._handle_flow_versions,
            "flow.restore": self._handle_flow_restore,
        }

    def _store(self) -> FlowStore:
        store = getattr(self._daemon, "_flow_store", None)
        if store is None:
            store = FlowStore()
            self._daemon._flow_store = store
        return store

    async def _handle_flow_create(self, params: dict) -> dict:
        graph = params.get("graph")
        name = str(params.get("name", "") or "")
        description = str(params.get("description", "") or "")
        tenant = resolve_tenant_id()
        try:
            out = self._store().save_flow(
                graph, tenant_id=tenant, name=name, description=description
            )
        except FlowStoreError as exc:
            logger.warning("flow.create rejected: %s", exc)
            return {"error": str(exc)}
        return {**out, "tenant_id": tenant}

    async def _handle_flow_save(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        graph = params.get("graph")
        name = str(params.get("name", "") or "")
        description = str(params.get("description", "") or "")
        if not flow_id:
            return {"error": "flow_id required"}
        tenant = resolve_tenant_id()
        try:
            out = self._store().save_flow(
                graph, tenant_id=tenant, name=name, description=description, flow_id=flow_id
            )
        except FlowStoreError as exc:
            logger.warning("flow.save rejected: %s", exc)
            return {"error": str(exc)}
        return {**out, "tenant_id": tenant}

    async def _handle_flow_list(self, params: dict) -> dict:
        tenant = resolve_tenant_id()
        flows = self._store().list_flows(tenant_id=tenant)
        return {"flows": flows, "count": len(flows), "tenant_id": tenant}

    async def _handle_flow_load(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        if not flow_id:
            return {"error": "flow_id required"}
        tenant = resolve_tenant_id()
        try:
            doc = self._store().load_flow(flow_id, tenant_id=tenant)
        except FlowStoreError as exc:
            logger.warning("flow.load failed: %s", exc)
            return {"error": str(exc)}
        return {**doc, "tenant_id": tenant}

    async def _handle_flow_delete(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        if not flow_id:
            return {"error": "flow_id required"}
        tenant = resolve_tenant_id()
        deleted = self._store().delete_flow(flow_id, tenant_id=tenant)
        if not deleted:
            return {"error": f"flow '{flow_id}' not found for tenant '{tenant or 'default'}'"}
        return {"deleted": True, "flow_id": flow_id}

    async def _handle_flow_versions(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        if not flow_id:
            return {"error": "flow_id required"}
        tenant = resolve_tenant_id()
        try:
            versions = self._store().list_versions(flow_id, tenant_id=tenant)
        except FlowStoreError as exc:
            return {"error": str(exc)}
        return {"flow_id": flow_id, "versions": versions, "count": len(versions)}

    async def _handle_flow_restore(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        try:
            version = int(params.get("version", 0))
        except (TypeError, ValueError):
            return {"error": "version must be an integer"}
        if not flow_id or version < 1:
            return {"error": "flow_id and version required"}
        tenant = resolve_tenant_id()
        try:
            out = self._store().restore_version(flow_id, version, tenant_id=tenant)
        except FlowStoreError as exc:
            logger.warning("flow.restore failed: %s", exc)
            return {"error": str(exc)}
        return {**out, "tenant_id": tenant}
