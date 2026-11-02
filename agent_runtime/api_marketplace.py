"""#531: deployed agents surface for the Cowork capability marketplace
Sub-agents tab (cross-repo wiring with fusion-cowork#228).

Read-only list/detail over the SAME native engine data the Dashboard
(#528) uses — no second store, no forked lifecycle:
  - listing = agents whose status is "published" (drafts/untested never
    appear; unpublish/disable in the Dashboard delists on next fetch)
  - the publish registry (§5.3) enriches entries with publisher/version
  - invocation contract: POST the agent's test endpoint with the caller's
    Bearer key; the calling workspace's directory scope and approval
    policy apply on the Cowork side (cowork#228).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from fastapi import Depends

from agent_runtime.errors import ErrorCode, raise_api_error

logger = logging.getLogger(__name__)

RPC = Callable[[str, dict], Awaitable[dict]]


def register_marketplace(
    app: Any,
    rpc: RPC,
    require_auth: Callable[..., Any],
) -> None:
    def _catalog_titles() -> Dict[str, Dict[str, Any]]:
        try:
            from agent_runtime.agent_platform.capabilities import (
                active_capabilities,
            )

            return {c["id"]: c for c in active_capabilities()}
        except Exception as e:  # pragma: no cover - degrade to raw ids
            logger.warning("marketplace: capability catalog unavailable: %s", e)
            return {}

    def _registry() -> Any:
        from agent_runtime.agent_platform.publish_registry import PublishRegistry

        return PublishRegistry()

    def _entry_for(
        agent: Dict[str, Any],
        titles: Dict[str, Dict[str, Any]],
        registry: Any,
        run_counts: Dict[str, int],
    ) -> Dict[str, Any]:
        agent_id = str(agent.get("id") or agent.get("agent_id") or "")
        tools = agent.get("tools") or []
        rec = registry.get(agent_id) if registry is not None else {}
        capabilities = []
        for entry in tools:
            # #536: connector-style drafts carry dict-shaped tool entries
            # ({"name": ..., "type": ...}) — normalize to the name; string
            # entries pass through. Unrecognizable entries are skipped
            # with a warning instead of 500ing the whole listing.
            if isinstance(entry, str):
                tid = entry
                definition = None
            elif isinstance(entry, dict):
                tid = str(entry.get("name") or entry.get("id") or "")
                definition = entry
            else:
                logger.warning(
                    "marketplace: agent %s has unrecognizable tool entry %r — skipped",
                    agent_id, entry)
                continue
            if not tid:
                continue
            cap = {
                "id": tid,
                "title": titles.get(tid, {}).get("title", tid),
                "mutation": bool(titles.get(tid, {}).get("mutation")),
            }
            if definition is not None:
                cap["definition"] = definition
            capabilities.append(cap)
        return {
            "agent_id": agent_id,
            "name": agent.get("name") or agent_id,
            "description": agent.get("description") or "",
            "capabilities": capabilities,
            "eval_score": None,
            "run_count": run_counts.get(agent_id),
            "version": agent.get("version") or rec.get("version"),
            "published_at": rec.get("published_at"),
            "publisher": rec.get("publisher"),
            "invoke": {
                "test": f"/v1/agents-platform/agents/{agent_id}/test",
                "method": "POST",
                "auth": "Authorization: Bearer <api key>",
                "body": {"message": "<task for the agent>"},
            },
        }

    async def _run_counts() -> Dict[str, int]:
        try:
            agg = await rpc("analytics.agent_usage", {})
            counts: Dict[str, int] = {}
            for row in agg.get("agents") or []:
                aid = str(row.get("agent_id") or "")
                if aid and row.get("runs"):
                    counts[aid] = int(row["runs"])
            return counts
        except Exception as e:
            logger.warning("marketplace: usage aggregation unavailable: %s", e)
            return {}

    @app.get("/v1/marketplace/agents",
             dependencies=[Depends(require_auth)])
    async def marketplace_agents() -> Dict[str, Any]:
        body = await rpc("agent.list", {})
        agents = body.get("agents") or body.get("data") or []
        published = [a for a in agents if a.get("status") == "published"]
        titles = _catalog_titles()
        try:
            registry = _registry()
        except Exception as e:
            logger.warning("marketplace: publish registry unavailable: %s", e)
            registry = None
        counts = await _run_counts()
        entries = [
            _entry_for(a, titles, registry, counts)
            for a in published
        ]
        logger.info("marketplace.agents: %d published of %d total",
                    len(entries), len(agents))
        return {"agents": entries, "total": len(entries)}

    @app.get("/v1/marketplace/agents/{agent_id}",
             dependencies=[Depends(require_auth)])
    async def marketplace_agent(agent_id: str) -> Dict[str, Any]:
        body = await rpc("agent.get", {"agent_id": agent_id})
        agent = body.get("agent")
        if agent is None:
            raise_api_error(ErrorCode.AGENT_NOT_FOUND,
                            param="agent_id",
                            detail=f"agent not found: {agent_id}")
        if agent.get("status") != "published":
            # drafts/untested/disabled agents are never marketable; hide
            # their very existence behind the same 404 as a missing agent
            raise_api_error(ErrorCode.AGENT_NOT_FOUND,
                            param="agent_id",
                            detail=f"agent not published: {agent_id}")
        titles = _catalog_titles()
        try:
            registry = _registry()
        except Exception as e:
            logger.warning("marketplace: publish registry unavailable: %s", e)
            registry = None
        counts = await _run_counts()
        entry = _entry_for(agent, titles, registry, counts)
        entry["usage_example"] = (
            f'curl -X POST <base>{entry["invoke"]["test"]} '
            '-H "Authorization: Bearer <api key>" '
            '-H "content-type: application/json" '
            '-d \'{"message": "hello"}\''
        )
        return entry
