"""#330: deny-by-default allowlisted RPC-over-HTTP bridge for the web module.

The web GUI (web/) cannot speak UDS JSON-RPC. Instead of hand-writing dozens
of REST routes, expose a single POST /v1/rpc endpoint that dispatches to the
daemon's handler registry (the same resolution /v1/runs uses via _rpc()).
Only methods in WEB_RPC_ALLOWLIST may pass; everything else is rejected with
a 400 before the handler is ever resolved. Streaming methods (agent/chat
execution) are deliberately NOT here — they have dedicated SSE routes.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Request

from agent_runtime.errors import ErrorCode, raise_api_error

logger = logging.getLogger(__name__)

RPC = Callable[[str, dict], Awaitable[dict]]

# GUI-facing read/write surface (step-1 scope: AgentStudio + AIAgent tabs).
# Streaming/agent-execution, task.*, planner.*, chat.stream, daemon control
# and lifecycle methods are intentionally excluded. Additions require an
# explicit entry here AND a test row in tests/test_issue_330_rpc_bridge.py.
WEB_RPC_ALLOWLIST: frozenset[str] = frozenset(
    {
        # agents
        "agent.create",
        "agent.get",
        "agent.list",
        "agent.update",
        "agent.delete",
        "agent.clone",
        "agent.archive",
        "agent.configure",
        "agent.publish",
        "agent.unpublish",
        "agent.get_soul",
        "agent.update_soul",
        "agent.resolve_persona",
        "agent.get_persona_layer",
        "agent.set_persona_layer",
        "agent.import_agents_md",
        "agent.export_agents_md",
        "agent.list_skills",
        "agent.add_skill",
        "agent.delete_skill",
        "agent.versions",
        "agent.snapshot",
        "agent.test",
        "permission.list",
        "permission.update",
        "skill.execute",
    "skills.list",
    "skills.load",
    "skills.install",
    "skills.delete",
        "research.adaptive",
        # memory
        "memory.store",
        "memory.recall",
        "memory.recall_relevant",
        "memory.list_recent",
        "memory.get",
        "memory.delete",
        "memory.delete_scope",
        "memory.count",
        "memory.auto_forget",
        "memory.decay_sweep",
        "memory.cleanup_suggestions",
        "memory.conflicts",
        "memory.backend",
        # safety
        "safety.check",
        "safety.classify_action",
        "safety.evaluate_action",
        "safety.approve",
        "safety.approve_action",
        "safety.reject",
        "safety.reject_action",
        "safety.get_pending_actions",
        "safety.add_policy",
        "safety.set_auto_mode",
        "safety.set_network_policy",
        "safety.get_network_policy",
        # connectors
        "connector.list",
        "connector.get",
        "connector.create",
        "connector.update",
        "connector.delete",
        "connector.connect",
        "connector.disconnect",
        "connector.test",
        # apikeys / cron / hooks
        "apikey.list",
        "apikey.create",
        "apikey.revoke",
        "apikey.rotate",
        "apikey.update",
        "cron.register",
        "cron.unregister",
        "cron.list",
        "cron.list_executions",
        "hooks.register",
        "hooks.list",
        "hooks.test",
        # tools (daemon core registry)
        "tool.list",
        "tool.get",
        "tool.get_schema",
        "tool.dynamic_register",
        "tool.dynamic_unregister",
        # team
        "team.orchestrate",
        "team.health",
        "team.swarm_agents",
        "team.plaza_channels",
        "team.plaza_messages",
        "team.plaza_break_in",
        "team.plaza_create",
        "team.plaza_broadcast",
        "team.roster_register",
        "team.roster_list",
        "team.roster_status",
        "team.mailbox_send",
        "team.mailbox_broadcast",
        "team.mailbox_inbox",
        "team.mailbox_ack",
        "team.mailbox_stats",
        # dashboard / ops (read-only)
        "daemon.status",
        "dashboard.overview",
        "analytics.agent_usage",
        "audit.list",
        "mlx.status",
        # N11 #350: session SLO telemetry (read-only)
        "telemetry.metrics",
        "telemetry.session_slo",
        # R4 #351: governance page — budget caps (set is mutating, audited)
        "budget.status",
        "telemetry.session_slo_export",
        "budget.set",
        # N9 #348: run artifact versioned triple (read-only)
        "run_artifact.list",
        "run_artifact.get",
        "run_artifact.versions",
        # marketplace
        "marketplace.list_categories",
        "marketplace.search",
        "marketplace.get",
        "marketplace.publish",
        "marketplace.unpublish",
        "marketplace.install",
        "marketplace.uninstall",
        # chat sessions + styles
        "chat.create",
        "chat.get",
        "chat.delete",
        "chat.list",
        "chat.send",
        "chat.history",
        "chat.edit",
        "chat.branch",
        "chat.branches",
        "chat.switch_branch",
        "chat.message_tree",
        "style.list",
        "style.get",
        # workflows (list/edit via JSON form; canvas is a later step)
        "graph.list",
        "graph.get",
        "graph.create",
        "graph.update",
        "graph.delete",
        "graph.cancel",
        "graph.status",
    }
)

# Last dot-segment of the method decides whether the call is logged to the
# audit trail (mutating verbs). Read verbs are not audited.
_MUTATING_VERBS = frozenset(
    {
        "create",
        "update",
        "delete",
        "register",
        "unregister",
        "publish",
        "unpublish",
        "install",
        "uninstall",
        "archive",
        "clone",
        "configure",
        "restore",
        "send",
        "edit",
        "branch",
        "connect",
        "disconnect",
        "store",
        "apply",
        "approve",
        "reject",
        "set",
        "add",
        "add_skill",
        "delete_skill",
        "switch_branch",
        "auto_forget",
        "delete_scope",
        "break_in",
        "broadcast",
        "orchestrate",
        "execute",
        "dynamic_register",
        "dynamic_unregister",
    }
)


def _is_mutating(method: str) -> bool:
    return method.rsplit(".", 1)[-1] in _MUTATING_VERBS


def register_rpc_bridge(
    app: FastAPI,
    rpc: RPC,
    auth_dependency: Callable[..., Any],
    audit: Callable[..., None] | None = None,
) -> None:
    """Wire POST /v1/rpc onto the FastAPI app.

    rpc: async (method, params) -> dict resolver (api_server._rpc — resolves
    through daemon._get_handler, 503 when no daemon attached). auth_dependency:
    api_server._require_auth_if_configured. audit: optional log_action callable
    (api_server._get_audit_logger().log_action).
    """

    @app.post("/v1/rpc", dependencies=[Depends(auth_dependency)])
    async def v1_rpc_bridge(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, detail="body must be JSON")
        if not isinstance(body, dict):
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, detail="body must be an object")
        method = str(body.get("method", "") or "").strip()
        params = body.get("params")
        if not method:
            raise_api_error(ErrorCode.PARAM_REQUIRED, param="method")
        if not isinstance(params, dict):
            params = {}
        if method not in WEB_RPC_ALLOWLIST:
            logger.warning("web rpc bridge rejected non-allowlisted method: %s", method)
            raise_api_error(
                ErrorCode.PARAM_FORMAT_ERROR,
                detail=f"method not allowed: {method}",
            )
        result = await rpc(method, params)
        if audit is not None and _is_mutating(method):
            ok = isinstance(result, dict) and result.get("status") != "error"
            audit(
                actor_id="web",
                action=f"web.rpc.{method}",
                resource_type="rpc",
                resource_id=method,
                details={},
                result="success" if ok else "error",
            )
        logger.info(
            "web rpc %s mutating=%s ok=%s",
            method,
            _is_mutating(method),
            not (isinstance(result, dict) and result.get("status") == "error"),
        )
        return result
