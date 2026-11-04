"""#555: flow.* RPC handlers — web canvas flow documents + version snapshots.

#558: template.* handlers — builtin plan templates + saved tenant templates
(flow store rows with kind='template'); instantiate clones into a fresh graph.
"""

from __future__ import annotations

import logging
import uuid
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
            "template.list": self._handle_template_list,
            "template.save": self._handle_template_save,
            "template.instantiate": self._handle_template_instantiate,
            "template.delete": self._handle_template_delete,
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

    def _builtin_templates(self) -> list[dict]:
        from agent_runtime.templates import TemplateManager, register_plan_templates

        if TemplateManager.count() == 0:
            register_plan_templates()
        return TemplateManager.list()

    async def _handle_template_list(self, params: dict) -> dict:
        builtins = [
            {**t, "source": "builtin"} for t in self._builtin_templates()
        ]
        tenant = resolve_tenant_id()
        saved = [
            {
                "name": f.get("name", ""),
                "description": f.get("description", ""),
                "flow_id": f.get("flow_id", ""),
                "version": f.get("version", 1),
                "source": "tenant",
            }
            for f in self._store().list_flows(tenant_id=tenant, kind="template")
        ]
        return {"templates": builtins + saved, "count": len(builtins) + len(saved)}

    async def _handle_template_save(self, params: dict) -> dict:
        name = str(params.get("name", "") or "")
        if not name:
            return {"error": "name required"}
        graph = params.get("graph")
        description = str(params.get("description", "") or "")
        tenant = resolve_tenant_id()
        try:
            out = self._store().save_flow(
                graph,
                tenant_id=tenant,
                name=name,
                description=description,
                flow_id=str(params.get("flow_id", "") or ""),
                kind="template",
            )
        except FlowStoreError as exc:
            logger.warning("template.save rejected: %s", exc)
            return {"error": str(exc)}
        logger.info("template saved id=%s tenant=%s", out.get("flow_id"), tenant)
        return {**out, "name": name, "kind": "template", "tenant_id": tenant}

    async def _handle_template_instantiate(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        name = str(params.get("name", "") or "")
        tenant = resolve_tenant_id()
        graph_data: dict | None = None
        tpl_name = ""
        if flow_id:
            try:
                doc = self._store().load_flow(flow_id, tenant_id=tenant)
                graph_data = doc.get("graph")
                tpl_name = doc.get("name", "")
            except FlowStoreError as exc:
                return {"error": str(exc)}
        else:
            if not name:
                return {"error": "name or flow_id required"}
            from agent_runtime.templates import TemplateManager, register_plan_templates

            if TemplateManager.count() == 0:
                register_plan_templates()
            try:
                graph = TemplateManager.get(name)
            except KeyError as exc:
                return {"error": str(exc)}
            graph_data = graph.to_dict()
            tpl_name = name
        try:
            from agent_runtime.graph import AgentGraph

            graph = AgentGraph.from_dict(graph_data)
        except Exception as exc:
            logger.warning("template.instantiate parse failed: %s", exc)
            return {"error": f"template graph parse error: {exc}"}
        graph.id = uuid.uuid4().hex[:16]
        graph.name = f"{tpl_name}-instance"
        logger.info("template instantiated tpl=%s graph=%s tenant=%s", tpl_name, graph.id, tenant)
        return {"graph": graph.to_dict(), "graph_id": graph.id, "name": graph.name, "source": tpl_name}

    async def _handle_template_delete(self, params: dict) -> dict:
        flow_id = str(params.get("flow_id", "") or "")
        if not flow_id:
            return {"error": "flow_id required"}
        tenant = resolve_tenant_id()
        try:
            doc = self._store().load_flow(flow_id, tenant_id=tenant)
        except FlowStoreError as exc:
            return {"error": str(exc)}
        if doc.get("name") and not flow_id:
            return {"error": "builtin templates cannot be deleted"}
        deleted = self._store().delete_flow(flow_id, tenant_id=tenant)
        if not deleted:
            return {"error": f"template '{flow_id}' not found for tenant '{tenant or 'default'}'"}
        return {"deleted": True, "flow_id": flow_id}
