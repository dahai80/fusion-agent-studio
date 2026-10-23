"""#437 (E1): Agents console — native REST surface + capability door.

Port of the f-ecommerce /agents-platform + /agent-platform endpoints to
fusion-agent-studio itself (the fas_client proxy hop is gone — FAS is the
host). The five GUI elements (agents list, Form/YAML/Test editor, assistant
panel, capability spine, seeded templates) are served from web/console/ and
talk to these routes; the single audited dispatch door
(POST /v1/agent-platform/call) is the security boundary for every
agent-callable capability, with mutations gated through the approval
workbench (approve/reject below) and every outcome audited.

Authoring YAML is the contract: create/update compile it through
yaml_compiler (capability checks, effort->temperature mapping, NO authority
granting) and persist the document as an authoring.yaml sidecar next to the
manifest so the Form<->YAML round-trip is lossless.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from agent_runtime.errors import ErrorCode, raise_api_error

logger = logging.getLogger(__name__)

RPC = Callable[[str, dict], Awaitable[dict]]


def register_agents_platform(
    app: Any,
    rpc: RPC,
    get_daemon: Callable[[], Any],
    require_auth: Callable[..., Any],
) -> None:
    """Attach all /v1/agents-platform + /v1/agent-platform routes.

    Must be registered BEFORE the web dist "/" mount so these API routes
    win over static files (same ordering contract as the RPC bridge).

    Routes register directly on app (repo convention — every other surface
    decorates app). The installed FastAPI's include_router() leaves an
    _IncludedRouter marker object without .path in app.routes, which breaks
    tests and any consumer iterating app.routes."""

    router = app

    def _door():
        from agent_runtime.agent_platform.door import get_default_door

        return get_default_door(get_daemon())

    def _agent_dir(agent_id: str) -> Path:
        daemon = get_daemon()
        if daemon is None:
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail="daemon not attached to API server")
        return Path(daemon._agent_dir(agent_id))

    def _sidecar_read(agent_id: str) -> Optional[str]:
        try:
            path = _agent_dir(agent_id) / "authoring.yaml"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("authoring.yaml read failed agent=%s: %s",
                           agent_id, e)
        return None

    def _sidecar_write(agent_id: str, text: str) -> None:
        try:
            path = _agent_dir(agent_id) / "authoring.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except Exception as e:
            # fail-soft: decompile() still round-trips from the manifest
            logger.warning("authoring.yaml write failed agent=%s: %s",
                           agent_id, e)

    def _compile_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
        from agent_runtime.agent_platform.yaml_compiler import (
            YamlCompileError,
            compile_doc,
        )

        try:
            return compile_doc(doc)
        except YamlCompileError as e:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, detail=str(e))
            return {}  # unreachable — raise_api_error raises

    def _doc_to_manifest_params(compiled: Dict[str, Any]) -> Dict[str, Any]:
        """Compiled definition -> agent.create/update kwargs. The manifest
        keeps plain tool-name strings; the compiled per-tool config lives
        in the sidecar contract (sync_agent_tools re-derives schemas from
        the local capability registry at run time)."""
        definition = compiled["definition"]
        model = definition.get("model") or {}
        params: Dict[str, Any] = {
            "name": definition.get("name", ""),
            "description": definition.get("description", ""),
            "system_prompt": definition.get("system_prompt", ""),
            "model": str(model.get("name") or "base"),
            "temperature": float(model.get("temperature") or 0.2),
            "max_tokens": int(model.get("max_tokens") or 2048),
            "tools": [t["name"] for t in definition.get("tools", [])],
            "status": "draft",
        }
        return params

    def _definition_for_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
        """Manifest dict -> compiler-shaped definition (decompile input)."""
        return {
            "name": agent.get("name") or "",
            "description": agent.get("description") or "",
            "system_prompt": agent.get("system_prompt") or "",
            "model": {
                "name": agent.get("model") or "base",
                "temperature": agent.get("temperature") or 0.2,
            },
            "tools": agent.get("tools") or [],
            "metadata": agent.get("metadata") or {},
        }

    def _yaml_dump(doc: Dict[str, Any]) -> str:
        import yaml

        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                              width=100)

    # ── console shell (whoami drives the sidebar, like the original) ────
    @router.get("/v1/agents-platform/whoami")
    async def agents_platform_whoami():
        return {
            "user": "studio",
            "role": "admin",
            "menus": [
                {"id": "agents", "label": "Agents", "href": "agents.html"},
            ],
        }

    @router.get("/v1/agents-platform/health")
    async def agents_platform_health():
        daemon = get_daemon()
        version = ""
        if daemon is not None:
            version = str(getattr(daemon, "version", "") or "")
        return {"ok": daemon is not None, "engine": "fusion-agent-studio",
                "version": version}

    # ── capability spine (the curated, agent-callable façade) ───────────
    @router.get("/v1/agents-platform/capability-catalog")
    async def agents_capability_catalog():
        from agent_runtime.agent_platform.capabilities import (
            active_capabilities,
        )

        caps = active_capabilities()
        counts: Dict[str, int] = {}
        for c in caps:
            counts[c.get("source", "?")] = counts.get(c.get("source", "?"),
                                                      0) + 1
        return {"capabilities": caps, "counts": counts}

    @router.get("/v1/agent-platform/capabilities")
    async def agent_platform_capabilities():
        from agent_runtime.agent_platform.capabilities import (
            active_capabilities,
        )

        return {"capabilities": active_capabilities()}

    @router.post("/v1/agent-platform/call",
                dependencies=[Depends(require_auth)])
    async def agent_platform_call(body: Dict[str, Any]):
        door = _door()
        try:
            status_code, data = await door.call(body)
            return JSONResponse(status_code=status_code, content=data)
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            detail = getattr(e, "detail", str(e))
            logger.info("agent-platform/call denied: %s %s",
                        status_code, detail)
            return JSONResponse(status_code=status_code,
                                content={"error": detail})

    # ── approval workbench (mutation gate — a human decides) ────────────
    @router.get("/v1/agent-platform/approvals")
    async def agent_platform_approvals(status: str = "", limit: int = 100):
        return {"approvals": _door().approvals.list(status=status,
                                                    limit=limit)}

    # #446: external approval-task creation — outside consumers (e.g.
    # EnviPermit) ride the same workbench + requester != approver
    # invariant. `payload` is opaque consumer data (their object ids,
    # evidence summary) and is returned verbatim in the decision webhook.
    @router.post("/v1/agent-platform/approvals",
                 dependencies=[Depends(require_auth)])
    async def agent_platform_create_approval(body: Dict[str, Any]):
        title = str(body.get("title") or "").strip()
        requester = str(body.get("requester") or "").strip()
        assigned_to = str(body.get("assigned_to") or "").strip()
        payload = body.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if not requester:
            raise_api_error(ErrorCode.PARAM_REQUIRED,
                            detail="requester is required")
        if not assigned_to:
            raise_api_error(ErrorCode.PARAM_REQUIRED,
                            detail="assigned_to (reviewer) is required")
        try:
            rec = _door().approvals.create_external(
                title=title or "external approval",
                requester=requester,
                assigned_to=assigned_to,
                params=payload,
                agent_id=str(body.get("agent_id") or ""))
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            return JSONResponse(status_code=status_code,
                                content={"error": getattr(e, "detail",
                                                          str(e))})
        return {"approval_id": rec["id"], "status": rec["status"],
                "kind": rec.get("kind"),
                "assigned_to": rec.get("assigned_to"),
                "poll": "/v1/agent-platform/approvals?status=pending"}

    @router.post("/v1/agent-platform/approvals/{approval_id}/approve",
                dependencies=[Depends(require_auth)])
    async def agent_platform_approve(approval_id: str,
                                     body: Dict[str, Any]):
        try:
            rec = await _door().approve(approval_id,
                                        str(body.get("decided_by") or ""))
            return rec
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            return JSONResponse(status_code=status_code,
                                content={"error": getattr(e, "detail",
                                                          str(e))})

    @router.post("/v1/agent-platform/approvals/{approval_id}/reject",
                dependencies=[Depends(require_auth)])
    async def agent_platform_reject(approval_id: str,
                                    body: Dict[str, Any]):
        try:
            rec = await _door().reject(approval_id,
                                       str(body.get("decided_by") or ""),
                                       str(body.get("reason") or ""))
            return rec
        except Exception as e:
            status_code = getattr(e, "status_code", 500)
            return JSONResponse(status_code=status_code,
                                content={"error": getattr(e, "detail",
                                                          str(e))})

    # ── #446: decision webhooks (approval.decided) ─────────────────────
    @router.get("/v1/agent-platform/webhooks")
    async def agent_platform_webhooks_list():
        from agent_runtime.agent_platform.webhooks import (
            get_webhook_registry,
        )

        return {"webhooks": get_webhook_registry().list()}

    @router.post("/v1/agent-platform/webhooks",
                 dependencies=[Depends(require_auth)])
    async def agent_platform_webhooks_register(body: Dict[str, Any]):
        from agent_runtime.agent_platform.webhooks import (
            get_webhook_registry,
        )

        url = str(body.get("url") or "").strip()
        secret = str(body.get("secret") or "").strip()
        if not url or not secret:
            raise_api_error(ErrorCode.PARAM_REQUIRED,
                            detail="url and secret are required")
        try:
            rec = get_webhook_registry().register(
                url, secret, body.get("events") or ["approval.decided"])
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        return rec

    @router.delete("/v1/agent-platform/webhooks/{webhook_id}",
                   dependencies=[Depends(require_auth)])
    async def agent_platform_webhooks_delete(webhook_id: str):
        from agent_runtime.agent_platform.webhooks import (
            get_webhook_registry,
        )

        deleted = get_webhook_registry().delete(webhook_id)
        if not deleted:
            raise_api_error(ErrorCode.RESOURCE_NOT_FOUND,
                            detail=f"webhook '{webhook_id}' not found")
        return {"deleted": True}

    # ── mutation execution (door-only: reached after an approval lands) ──
    # The route table's /v1/agent-platform/mutations/* paths resolve through
    # this IN-PROCESS dispatcher — the door has already authenticated the
    # caller, re-validated params, and (for mutations) waited for approval,
    # so self-hosted dispatch never loops back out over HTTP.
    async def _self_dispatch(method: str, path: str,
                             body: Dict[str, Any]) -> tuple:
        if path == "/v1/agents-platform/agents" and method == "GET":
            result = await rpc("agent.list", {})
            return 200, {"agents": result.get("agents")
                         or result.get("data") or []}
        if path == "/v1/agent-platform/mutations/graph.create" \
                and method == "POST":
            name = str(body.get("name") or "").strip()
            if not name:
                return 400, {"error": "name is required"}
            # the capability contract exposes name/description only — build
            # a minimal valid skeleton (START -> END) so the approved
            # mutation passes engine validation
            result = await rpc("langgraph.create", {
                "name": name,
                "nodes": [
                    {"node_id": "start", "node_type": "START_NODE",
                     "label": "Start"},
                    {"node_id": "end", "node_type": "END_NODE",
                     "label": "End"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
                "entry_node_id": "start",
            })
            ok = not (isinstance(result, dict)
                      and result.get("status") == "error")
            logger.info("mutation graph.create executed: name=%s ok=%s",
                        name, ok)
            return 200 if ok else 422, result
        return 404, {"error": f"no in-process route for {method} {path}"}

    from agent_runtime.agent_platform import door as _door_mod
    _door_mod.set_self_dispatch(_self_dispatch)

    # ── agents CRUD (draft -> publish lifecycle over the native daemon) ─
    @router.get("/v1/agents-platform/agents")
    async def agents_platform_list():
        body = await rpc("agent.list", {})
        agents = body.get("agents") or body.get("data") or []
        return {"agents": [
            {k: a.get(k) for k in
             ("id", "name", "description", "status", "version_int",
              "version", "published_at", "author", "tools", "model")}
            for a in agents]}

    @router.post("/v1/agents-platform/agents",
                dependencies=[Depends(require_auth)])
    async def agents_platform_create(body: Dict[str, Any]):
        doc = body.get("definition_doc") or {}
        compiled = _compile_doc(doc)
        params = _doc_to_manifest_params(compiled)
        out = await rpc("agent.create", params)
        if not out.get("agent_id"):
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail="agent engine returned no agent_id")
        agent_id = out["agent_id"]
        _sidecar_write(agent_id, _yaml_dump(doc))
        return {"agent_id": agent_id,
                "warnings": compiled.get("warnings", []),
                "model": params["model"]}

    @router.get("/v1/agents-platform/agents/{agent_id}")
    async def agents_platform_get(agent_id: str):
        body = await rpc("agent.get", {"agent_id": agent_id})
        agent = body.get("agent")
        if agent is None:
            raise_api_error(ErrorCode.AGENT_NOT_FOUND,
                            detail=f"agent not found: {agent_id}")
        from agent_runtime.agent_platform.yaml_compiler import decompile

        yaml_text = _sidecar_read(agent_id) or decompile(
            _definition_for_agent(agent))
        import yaml

        return {"agent": agent, "yaml": yaml_text,
                "doc": yaml.safe_load(yaml_text) or {}}

    @router.put("/v1/agents-platform/agents/{agent_id}",
                dependencies=[Depends(require_auth)])
    async def agents_platform_update(agent_id: str, body: Dict[str, Any]):
        doc = body.get("definition_doc") or {}
        compiled = _compile_doc(doc)
        params = _doc_to_manifest_params(compiled)
        out = await rpc("agent.update", {"agent_id": agent_id, **params})
        _sidecar_write(agent_id, _yaml_dump(doc))
        return {"updated": bool(out.get("updated")),
                "warnings": compiled.get("warnings", []),
                "model": params["model"]}

    @router.post("/v1/agents-platform/agents/{agent_id}/publish",
                dependencies=[Depends(require_auth)])
    async def agents_platform_publish(agent_id: str,
                                      request: Request):
        out = await rpc("agent.publish", {"agent_id": agent_id})
        if out.get("status") == "error":
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail=out.get("message", "publish failed"))
        # Server-side publish registry (§5.3/§9): authority resolves from
        # THIS record at door time — never a client-asserted author field.
        user = ""
        try:
            user = str(getattr(request.state, "user", "") or "")
        except Exception:
            pass
        from agent_runtime.agent_platform.publish_registry import (
            PublishRegistry,
        )

        PublishRegistry().set(agent_id, user or "studio",
                              out.get("version"))
        return {"agent_id": agent_id, "status": out.get("status"),
                "version": out.get("version")}

    @router.delete("/v1/agents-platform/agents/{agent_id}",
                dependencies=[Depends(require_auth)])
    async def agents_platform_delete(agent_id: str):
        out = await rpc("agent.delete", {"agent_id": agent_id})
        from agent_runtime.agent_platform.publish_registry import (
            PublishRegistry,
        )

        PublishRegistry().remove(agent_id)
        return {"deleted": bool(out.get("deleted", True))}

    @router.post("/v1/agents-platform/agents/{agent_id}/test",
                dependencies=[Depends(require_auth)])
    async def agents_platform_test(agent_id: str, body: Dict[str, Any]):
        message = str(body.get("message") or "").strip()
        if not message:
            raise_api_error(ErrorCode.PARAM_REQUIRED,
                            detail="message is required")
        out = await rpc("agent.test",
                        {"agent_id": agent_id, "input": message})
        if out.get("status") == "error":
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail=out.get("message", "agent run failed"))
        events = out.get("events") or []
        return {"status": out.get("status"),
                "events": [{"type": ev.get("type"),
                            "content": ev.get("content")}
                           for ev in events]}

    # ── authoring aids: seeded templates, YAML compile, LLM composer ────
    @router.get("/v1/agents-platform/templates")
    async def agents_platform_templates():
        from agent_runtime.agent_platform.templates import AGENT_TEMPLATES

        return {"templates": AGENT_TEMPLATES}

    @router.post("/v1/agents-platform/compile")
    async def agents_platform_compile(body: Dict[str, Any]):
        from agent_runtime.agent_platform.yaml_compiler import (
            YamlCompileError,
            compile_yaml,
        )

        text = str(body.get("yaml") or "")
        try:
            compiled = compile_yaml(text)
        except YamlCompileError as e:
            return {"ok": False, "error": str(e)}
        import yaml

        return {"ok": True, "doc": yaml.safe_load(text),
                "definition": compiled["definition"],
                "warnings": compiled["warnings"]}

    @router.post("/v1/agents-platform/compose",
                dependencies=[Depends(require_auth)])
    async def agents_platform_compose(body: Dict[str, Any]):
        """One-sentence ask -> draft authoring YAML via the LLM gateway.
        Honest failure: model output that does not compile returns the raw
        text + the compile error for manual fixing."""
        ask = str(body.get("ask") or "").strip()
        if not ask:
            raise_api_error(ErrorCode.PARAM_REQUIRED, detail="ask is required")
        daemon = get_daemon()
        if daemon is None:
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail="daemon not attached to API server")
        from agent_runtime.agent_platform.capabilities import (
            active_capabilities,
        )
        from agent_runtime.agent_platform.yaml_compiler import (
            YamlCompileError,
            compile_doc,
        )

        catalog = "\n".join(
            f"- {c['id']} ({c['title']}; "
            f"{'MUTATION, approval-gated' if c['mutation'] else 'read-only'})"
            f" — {c['description']}"
            for c in active_capabilities())
        system = (
            "You convert a one-sentence agent request into an "
            "agent-authoring YAML document.\n\n"
            "Rules:\n"
            "- Output ONLY a YAML document in this exact shape:\n"
            "name: <short name>\n"
            "description: <one line>\n"
            "model:\n  id: base\n  effort: low\n"
            "system: |\n  <behavioral contract: role, rules, tone; say "
            "what the agent should do, never how>\n"
            "tools:\n  - <capability id>\n"
            "metadata:\n  template: custom\n"
            "- tools may ONLY use ids from the catalog below; pick the "
            "minimal set that covers the request, preferring read-only.\n"
            "- The system prompt must demand skepticism, honest citation, "
            "and state that mutations go through human approval.\n"
            "- No commentary outside the YAML.\n\n"
            f"Capability catalog:\n{catalog}"
        )
        try:
            resp = await daemon._gateway.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": ask}],
                temperature=0.2, max_tokens=1500)
        except Exception as e:
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail=f"model gateway unavailable: {e}")
        text = (getattr(resp, "content", "") or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("yaml"):
                text = text[4:]
            text = text.strip()
        lines = text.splitlines()
        try:
            start = next(i for i, ln in enumerate(lines)
                         if ln.startswith(("name:", "description:", "model:",
                                           "system:", "tools:", "metadata:")))
            doc_text = "\n".join(lines[start:])
        except StopIteration:
            doc_text = text
        import yaml

        try:
            doc = yaml.safe_load(doc_text) or {}
            compiled = compile_doc(doc)
        except (yaml.YAMLError, YamlCompileError) as e:
            return {"ok": False, "error": f"model output did not compile: "
                                          f"{e}", "yaml": doc_text}
        return {"ok": True, "doc": doc, "yaml": _yaml_dump(doc),
                "definition": compiled["definition"],
                "warnings": compiled.get("warnings", [])}

    # ── assistant panel descriptor (model honesty + tuned registry) ─────
    @router.get("/v1/agents-platform/agent-descriptor")
    async def agents_platform_agent_descriptor():
        daemon = get_daemon()
        llm_enabled = False
        tuned_model = None
        if daemon is not None:
            try:
                llm_enabled = bool(daemon._gateway.default_model)
            except Exception:
                llm_enabled = False
            try:
                # registered fine-tuned model (trainer publish, E4): the
                # "tuned" radio reflects reality, never a hardcoded label
                from agent_runtime.agent_platform.publish_registry import (
                    PublishRegistry,
                )

                reg = PublishRegistry(str(
                    Path.home() / ".fusion-agent-studio"
                    / "tuned_model.json"))
                rec = reg.get("tuned") or {}
                if rec.get("model_id"):
                    tuned_model = {"model_id": rec["model_id"],
                                   "label": rec.get("label") or "tuned"}
            except Exception:
                tuned_model = None
        return {"llm_workflows_enabled": llm_enabled,
                "capabilities": [], "tuned_model": tuned_model}

    # ── assistant panel built-in chat (one-shot over the gateway) ───────
    @router.post("/v1/agents-platform/assistant",
                dependencies=[Depends(require_auth)])
    async def agents_platform_assistant(body: Dict[str, Any]):
        message = str(body.get("message") or "").strip()
        if not message:
            raise_api_error(ErrorCode.PARAM_REQUIRED,
                            detail="message is required")
        daemon = get_daemon()
        if daemon is None:
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail="daemon not attached to API server")
        context = body.get("context") or {}
        ctx_text = ""
        if isinstance(context, dict) and context:
            ctx_text = "\nPage context: " + json.dumps(
                context, ensure_ascii=False)[:600]
        system = (
            "You are the assistant built into the Agents console of "
            "fusion-agent-studio. You help the user author agents: explain "
            "the Form/YAML/Test editor, the capability registry, the "
            "draft->publish->approval lifecycle. Be concise and concrete. "
            "If the user describes an agent they want, draft the "
            "authoring YAML (name/description/model/system/tools) using "
            "only these capability ids: "
            + ", ".join(c["id"] for c in _active_caps()) + "."
            + ctx_text
        )
        try:
            resp = await daemon._gateway.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": message}],
                temperature=0.3, max_tokens=1200)
        except Exception as e:
            raise_api_error(ErrorCode.INTERNAL_ERROR,
                            detail=f"model gateway unavailable: {e}")
        return {"reply": (getattr(resp, "content", "") or "").strip()}

    def _active_caps() -> List[Dict[str, Any]]:
        from agent_runtime.agent_platform.capabilities import (
            active_capabilities,
        )

        return active_capabilities()

    logger.info("agents-platform console routes registered "
                "(agents CRUD + capability door + approvals)")
