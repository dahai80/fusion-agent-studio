"""#583: cluster-ops proxy surface.

Thin passthrough from the daemon REST surface (/v1/cluster-ops/*) to the
agent-studio cluster_server (agent_runtime/cluster_server.py, port 11457,
port-registry `fusion-agent-studio-cluster`). The web UI is same-origin
with the daemon, so cluster ops (stats / nodes / tasks / autoscaler /
observability / routing) ride the daemon instead of a cross-origin hop.
Unreachable cluster → 503 with the underlying error (fail visibly, no
silent fallback).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_OPS_URL = "http://127.0.0.1:11457"

_TIMEOUT = 10.0


def cluster_ops_url() -> str:
    raw = (os.environ.get("FUSION_CLUSTER_OPS_URL", "") or DEFAULT_CLUSTER_OPS_URL).strip()
    return raw.rstrip("/") or DEFAULT_CLUSTER_OPS_URL


async def cluster_ops_forward(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    raw: bool = False,
) -> Any:
    url = cluster_ops_url() + path
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False) as client:
            resp = await client.request(method, url, json=json_body)
    except Exception as e:
        logger.warning("cluster ops unreachable (%s %s): %s", method, url, e)
        raise HTTPException(
            status_code=503,
            detail=f"cluster server unreachable at {cluster_ops_url()}: {e}",
        )
    if resp.status_code >= 400:
        detail = resp.text[:300] if not raw else resp.text[:300]
        logger.warning("cluster ops %s %s -> %s: %s", method, url, resp.status_code, detail)
        raise HTTPException(status_code=resp.status_code, detail=detail)
    if raw:
        return resp.text
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def register_cluster_ops(app: FastAPI, require_auth) -> None:
    @app.get("/v1/cluster-ops/stats")
    async def v1_cluster_stats():
        return await cluster_ops_forward("GET", "/api/v1/cluster/stats")

    @app.get("/v1/cluster-ops/nodes")
    async def v1_cluster_nodes():
        return await cluster_ops_forward("GET", "/api/nodes")

    @app.get("/v1/cluster-ops/nodes/{node_id}/metrics")
    async def v1_cluster_node_metrics(node_id: str):
        return await cluster_ops_forward(
            "GET", f"/api/v1/nodes/{node_id}/metrics"
        )

    @app.get("/v1/cluster-ops/autoscaler/config")
    async def v1_cluster_autoscaler_get():
        return await cluster_ops_forward("GET", "/api/v1/autoscaler/config")

    @app.put("/v1/cluster-ops/autoscaler/config", dependencies=[Depends(require_auth)])
    async def v1_cluster_autoscaler_put(request: Request):
        body = await request.json()
        return await cluster_ops_forward(
            "PUT", "/api/v1/autoscaler/config", json_body=body
        )

    @app.get("/v1/cluster-ops/observability/alerts")
    async def v1_cluster_alerts():
        return await cluster_ops_forward("GET", "/api/v1/observability/alerts")

    @app.get("/v1/cluster-ops/observability/suggestions")
    async def v1_cluster_suggestions():
        return await cluster_ops_forward("GET", "/api/v1/observability/suggestions")

    @app.get("/v1/cluster-ops/observability/logs/export")
    async def v1_cluster_logs_export():
        from fastapi import Response

        text = await cluster_ops_forward(
            "GET", "/api/v1/observability/logs/export", raw=True
        )
        return Response(
            content=text,
            media_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="cluster-logs.txt"'},
        )

    @app.get("/v1/cluster-ops/tasks")
    async def v1_cluster_tasks():
        return await cluster_ops_forward("GET", "/api/tasks")

    @app.post("/v1/cluster-ops/tasks", dependencies=[Depends(require_auth)])
    async def v1_cluster_task_submit(request: Request):
        body = await request.json()
        return await cluster_ops_forward("POST", "/api/tasks/submit", json_body=body)

    @app.post("/v1/cluster-ops/tasks/{task_id}/cancel", dependencies=[Depends(require_auth)])
    async def v1_cluster_task_cancel(task_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return await cluster_ops_forward(
            "POST", f"/api/tasks/{task_id}/cancel", json_body=body or {}
        )

    @app.post("/v1/cluster-ops/tasks/{task_id}/degrade", dependencies=[Depends(require_auth)])
    async def v1_cluster_task_degrade(task_id: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return await cluster_ops_forward(
            "POST", f"/api/tasks/{task_id}/degrade", json_body=body or {}
        )

    @app.post("/v1/cluster-ops/tasks/{task_id}/migrate", dependencies=[Depends(require_auth)])
    async def v1_cluster_task_migrate(task_id: str, request: Request):
        body = await request.json()
        return await cluster_ops_forward(
            "POST", f"/api/tasks/{task_id}/migrate", json_body=body
        )

    @app.get("/v1/cluster-ops/routing/summary")
    async def v1_cluster_routing_summary():
        return await cluster_ops_forward("GET", "/api/routing/summary")

    @app.post("/v1/cluster-ops/routing/strategy", dependencies=[Depends(require_auth)])
    async def v1_cluster_routing_strategy(request: Request):
        body = await request.json()
        return await cluster_ops_forward(
            "POST", "/api/routing/strategy", json_body=body
        )

    logger.info("cluster-ops proxy registered at /v1/cluster-ops/* -> %s", cluster_ops_url())
