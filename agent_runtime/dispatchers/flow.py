"""#555: flow.* RPC handlers — web canvas flow documents + version snapshots.

#558: template.* handlers — builtin plan templates + saved tenant templates
(flow store rows with kind='template'); instantiate clones into a fresh graph.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from agent_runtime.flow_store import FlowStore, FlowStoreError, resolve_tenant_id

logger = logging.getLogger(__name__)


_NL_SYSTEM_PROMPT = """You are a workflow graph builder for a local AI agent platform.
Convert the user's request into graph mutation ops. Respond ONLY with JSON:
{"reply": "<one short sentence, same language as the user>",
 "ops": [{"op": "...", ...}]}
Allowed ops (and only these):
- {"op":"add_node","id":"<short-id>","type":"<type>","label":"<human label>","config":{...}}
- {"op":"remove_node","id":"<existing-id>"}
- {"op":"set_config","id":"<existing-id>","config":{...}}
- {"op":"connect","source":"<existing-id>","target":"<existing-id>","label":"<edge label or empty>"}
- {"op":"disconnect","source":"<existing-id>","target":"<existing-id>"}
- {"op":"clear_graph"}
- {"op":"instantiate_template","name":"<template name>"}
Rules:
- "type" MUST be one of the allowed node types listed by the caller. Never invent types.
- For llm/planner nodes set "model" in config to the caller-provided default
  model when the current graph state shows one; otherwise omit it and the
  server fills it.
- "config" values may reference run inputs with {{ variable }} syntax.
- connect only ids that exist (after earlier ops in the same list).
- Every graph needs exactly one start node and at least one end node.
- If the request is better served by a template from the provided list, prefer
  {"op":"instantiate_template"} plus follow-up ops on the instantiated graph,
  and mention the template in reply.
- If the request is ambiguous, ask a clarifying question in "reply" with empty ops.
"""

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
            "flow.template_list": self._handle_template_list,
            "flow.template_save": self._handle_template_save,
            "flow.template_instantiate": self._handle_template_instantiate,
            "flow.template_delete": self._handle_template_delete,
            "graph.nl_plan": self._handle_graph_nl_plan,
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

        # Seed on the plan-template sentinel, not count()==0: other paths
        # (e.g. register_default_templates) may have already populated the
        # global manager, and skipping then left the #558 set missing.
        if not TemplateManager.has("basic-llm-prompt"):
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

            if not TemplateManager.has("basic-llm-prompt"):
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

    async def _handle_graph_nl_plan(self, params: dict) -> dict:
        prompt = str(params.get("prompt", "") or "")
        if not prompt.strip():
            return {"error": "prompt required"}
        catalog = params.get("catalog") or []
        allowed_types = {str(c.get("type", "")) for c in catalog if isinstance(c, dict) and c.get("type")}
        if not allowed_types:
            from agent_runtime.graph import AgentGraph

            allowed_types = set(AgentGraph._VALID_NODE_TYPES)
        templates = params.get("templates") or []
        template_names = {
            str(t.get("name", ""))
            for t in templates
            if isinstance(t, dict) and t.get("name")
        }
        graph_state = params.get("graph_json")
        default_model = str(params.get("default_model", "") or "")
        catalog_desc = ", ".join(sorted(allowed_types))
        template_desc = ", ".join(sorted(template_names)) if template_names else "(none)"
        system = (
            f"{_NL_SYSTEM_PROMPT}\nAllowed node types: {catalog_desc}\n"
            f"Available templates: {template_desc}\n"
        )
        if isinstance(graph_state, dict) and graph_state.get("nodes"):
            system += f"Current graph state: {json.dumps(graph_state, ensure_ascii=False)}\n"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        model = str(params.get("model", "") or "")
        rt = self._daemon._get_runtime()
        gw = getattr(rt, "llm_gateway", None)
        if gw is None:
            return {"error": "no llm gateway available on daemon runtime"}
        try:
            resp = await gw.chat(messages, model=model, temperature=0.2)
        except Exception as exc:
            logger.warning("graph.nl_plan llm call failed: %s", exc)
            return {"error": f"llm call failed: {exc}"}
        content = getattr(resp, "content", "") or ""
        try:
            from fusion_core.parse import parse_llm_json_lenient

            plan = parse_llm_json_lenient(content)
        except Exception as exc:
            logger.warning("graph.nl_plan parse failed: %s; raw=%r", exc, content[:200])
            return {"error": f"model reply is not valid plan JSON: {exc}", "raw": content[:500]}
        if isinstance(plan, list):
            plan = {"ops": plan}
        ops = plan.get("ops") or []
        if not isinstance(ops, list):
            return {"error": "plan.ops must be a list", "raw": content[:500]}
        # Validate ops against the registry-grounded catalog + graph state.
        existing = set()
        if isinstance(graph_state, dict):
            nodes_raw = graph_state.get("nodes")
            if isinstance(nodes_raw, list):
                existing = {str(n.get("id", "")) for n in nodes_raw if isinstance(n, dict)}
            elif isinstance(nodes_raw, dict):
                existing = set(nodes_raw.keys())
        errors: list[str] = []
        # Two sets: `targets` is the union of state ids and every id added in
        # this plan (plans apply atomically, so an op may reference ids added
        # LATER in the same list — models emit connects before adds);
        # `existing` replays add/remove/clear in order for duplicate detection.
        targets = set(existing)
        for op in ops:
            if isinstance(op, dict) and str(op.get("op", "")) == "add_node":
                nid = str(op.get("id", ""))
                if nid and nid.replace("-", "").replace("_", "").isalnum():
                    targets.add(nid)
        for i, op in enumerate(ops):
            # Deterministic fill (Rule 5): llm/planner nodes need an executable
            # model — the model-provided default wins, else caller default_model.
            if (
                default_model
                and str(op.get("op", "")) == "add_node"
                and str(op.get("type", "")) in ("llm", "planner")
            ):
                cfg = op.get("config")
                if not isinstance(cfg, dict):
                    cfg = {}
                    op["config"] = cfg
                if not cfg.get("model"):
                    cfg["model"] = default_model
            if not isinstance(op, dict):
                errors.append(f"op[{i}]: not an object")
                continue
            kind = str(op.get("op", ""))
            if kind == "add_node":
                nid = str(op.get("id", ""))
                ntype = str(op.get("type", ""))
                if not nid or not nid.replace("-", "").replace("_", "").isalnum():
                    errors.append(f"op[{i}]: add_node needs a short alphanumeric id")
                elif nid in existing:
                    errors.append(f"op[{i}]: node '{nid}' already exists — use set_config instead")
                if ntype not in allowed_types:
                    errors.append(
                        f"op[{i}]: unknown node type '{ntype}' — allowed: {catalog_desc}"
                    )
                else:
                    existing.add(nid)
                    targets.add(nid)
            elif kind in ("remove_node", "set_config"):
                if str(op.get("id", "")) not in targets:
                    errors.append(f"op[{i}]: {kind} target '{op.get('id')}' not in graph")
                if kind == "remove_node" and str(op.get("id", "")) in existing:
                    existing.discard(str(op.get("id", "")))
            elif kind in ("connect", "disconnect"):
                src, tgt = str(op.get("source", "")), str(op.get("target", ""))
                if src not in targets or tgt not in targets:
                    errors.append(
                        f"op[{i}]: {kind} endpoints must exist in graph (got '{src}' -> '{tgt}')"
                    )
            elif kind == "clear_graph":
                existing.clear()
            elif kind == "instantiate_template":
                name = str(op.get("name", ""))
                if template_names and name not in template_names:
                    errors.append(
                        f"op[{i}]: unknown template '{name}' — available: {template_desc}"
                    )
            else:
                errors.append(f"op[{i}]: unknown op kind '{kind}'")
        if errors:
            logger.info("graph.nl_plan rejected %d/%d ops", len(errors), len(ops))
            return {
                "error": "invalid plan ops",
                "details": errors,
                "allowed_types": sorted(allowed_types),
                "reply": str(plan.get("reply", "")),
            }
        reply = str(plan.get("reply", ""))
        logger.info("graph.nl_plan ok ops=%d templates=%s", len(ops), template_desc[:80])
        return {"reply": reply, "ops": ops}
