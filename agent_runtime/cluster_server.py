"""Cluster HTTP server — REST API for multi-node cluster management.

Listens on port 11457 (default). Provides endpoints consumed by
fusion-studio MultiNodeEngine.swift for cluster status, node management,
task distribution, KV cache, routing, and observability.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)

CLUSTER_PORT = 11457


class ClusterState:
    """In-memory cluster state for single-node dev mode."""

    def __init__(self):
        self.nodes: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.routing_strategy: str = "least_loaded"
        self.kv_cache: dict[str, dict[str, Any]] = {}
        self.autoscaler_config: dict[str, Any] = {
            "enabled": False,
            "min_nodes": 2,
            "max_nodes": 8,
            "scale_up_threshold": 0.8,
            "scale_down_threshold": 0.3,
            "cooldown_seconds": 60,
            "idle_timeout_seconds": 300,
            "policy": "threshold",
            "check_interval": 30,
            "rebalance_threshold": 0.2,
        }
        self._register_local_node()

    def _register_local_node(self):
        node_id = f"local-{uuid.uuid4().hex[:8]}"
        mem_gb = self._detect_memory_gb()
        gpu_cores = self._detect_gpu_cores()
        device = self._detect_device_model()
        self.nodes[node_id] = {
            "node_id": node_id,
            "hostname": platform.node(),
            "ip_address": "127.0.0.1",
            "port": 11457,
            "status": "online",
            "total_memory_gb": mem_gb,
            "available_memory_gb": mem_gb * 0.7,
            "cpu_cores": self._detect_cpu_cores(),
            "gpu_cores": gpu_cores,
            "device_model": device,
            "uma_size_gb": mem_gb if device and "Apple" in device else None,
            "active_tasks": 0,
            "max_tasks": 4,
            "score": 100.0,
            "last_heartbeat": time.time(),
            "role": "master",
        }
        logger.info("Registered local node: %s (%s, %.1f GB)", node_id, device, mem_gb)

    @staticmethod
    def _detect_cpu_cores() -> int:
        try:
            import os as _os

            return _os.cpu_count() or 8
        except Exception:
            return 8

    @staticmethod
    def _detect_memory_gb() -> float:
        try:
            import os as _os

            return (
                _os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES") / (1024**3)
            )
        except Exception:
            return 16.0

    @staticmethod
    def _detect_gpu_cores() -> int:
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                if "Total Number of Cores" in line:
                    return int(line.split(":")[-1].strip())
        except Exception:
            pass
        return 0

    @staticmethod
    def _detect_device_model() -> str:
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return platform.processor() or "Unknown"


state = ClusterState()


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("Cluster API starting on port %d", CLUSTER_PORT)
    yield
    logger.info("Cluster API shutting down")


app = FastAPI(title="Fusion Cluster API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "role": "master"}


@app.get("/api/v1/cluster/stats")
async def cluster_stats():
    nodes_online = sum(
        1 for n in state.nodes.values() if n["status"] in ("online", "busy")
    )
    total_mem = sum(n["total_memory_gb"] for n in state.nodes.values())
    avail_mem = sum(n["available_memory_gb"] for n in state.nodes.values())
    active_tasks = sum(1 for t in state.tasks.values() if t["status"] == "running")
    total_tasks = len(state.tasks)
    completed_tasks = sum(1 for t in state.tasks.values() if t["status"] == "completed")
    failed_tasks = sum(1 for t in state.tasks.values() if t["status"] == "failed")
    util = (1 - avail_mem / total_mem) if total_mem > 0 else 0

    return {
        "cluster": {
            "online_nodes": nodes_online,
            "total_nodes": len(state.nodes),
            "active_tasks": active_tasks,
            "total_memory_gb": round(total_mem, 2),
            "available_memory_gb": round(avail_mem, 2),
            "utilization": round(util, 4),
        },
        "tasks": {
            "total": total_tasks,
            "completed": completed_tasks,
            "failed": failed_tasks,
        },
    }


@app.get("/api/nodes")
async def list_nodes():
    online = sum(1 for n in state.nodes.values() if n["status"] in ("online", "busy"))
    return {
        "total": len(state.nodes),
        "online": online,
        "nodes": list(state.nodes.values()),
    }


@app.get("/api/v1/nodes/{node_id}/metrics")
async def node_metrics(node_id: str):
    node = state.nodes.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    avail = node["available_memory_gb"]
    total = node["total_memory_gb"]
    return {
        "node_id": node_id,
        "status": node["status"],
        "role": node.get("role"),
        "score": node["score"],
        "available_memory_gb": avail,
        "total_memory_gb": total,
        "active_tasks": node["active_tasks"],
        "max_tasks": node["max_tasks"],
        "network_rtt_ms": None,
        "load_metrics": {
            "uma_used_ratio": round(1 - avail / total, 4) if total > 0 else 0,
            "cpu_percent": 0.0,
            "metal_util": 0.0,
            "task_queue_len": node["active_tasks"],
            "net_rtt_ms": 0.0,
        },
    }


@app.post("/api/join")
async def join_node(body: dict):
    ip = body.get("ip_address", "127.0.0.1")
    port = body.get("port", 11457)
    node_id = f"node-{uuid.uuid4().hex[:8]}"
    state.nodes[node_id] = {
        "node_id": node_id,
        "hostname": ip,
        "ip_address": ip,
        "port": port,
        "status": "online",
        "total_memory_gb": 16.0,
        "available_memory_gb": 12.0,
        "cpu_cores": 8,
        "gpu_cores": 0,
        "device_model": "Remote Node",
        "uma_size_gb": None,
        "active_tasks": 0,
        "max_tasks": 4,
        "score": 50.0,
        "last_heartbeat": time.time(),
        "role": "worker",
    }
    logger.info("Node joined: %s (%s:%d)", node_id, ip, port)
    return {"node_id": node_id, "status": "online"}


@app.delete("/api/nodes/{node_id}")
async def remove_node(node_id: str):
    if node_id not in state.nodes:
        raise HTTPException(status_code=404, detail="Node not found")
    del state.nodes[node_id]
    logger.info("Node removed: %s", node_id)
    return {"deleted": True}


@app.get("/api/tasks")
async def list_tasks():
    return {"total": len(state.tasks), "tasks": list(state.tasks.values())}


class TaskSubmitRequest(BaseModel):
    name: str = ""
    mode: str = "inference"
    model_name: str = ""
    priority: int = 5
    required_capability: str | None = None


@app.post("/api/tasks/submit")
async def submit_task(req: TaskSubmitRequest):
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    now = time.time()

    best_node = None
    best_score = -1
    for n in state.nodes.values():
        if n["status"] in ("online", "busy") and n["active_tasks"] < n["max_tasks"]:
            if n["score"] > best_score:
                best_score = n["score"]
                best_node = n

    assigned = []
    if best_node:
        assigned.append(best_node["node_id"])
        best_node["active_tasks"] += 1
        best_node["status"] = (
            "busy" if best_node["active_tasks"] >= best_node["max_tasks"] else "online"
        )

    task = {
        "task_id": task_id,
        "name": req.name,
        "mode": req.mode,
        "model_name": req.model_name,
        "status": "running" if assigned else "pending",
        "assigned_nodes": assigned,
        "created_at": now,
        "started_at": now if assigned else None,
        "completed_at": None,
        "error": None,
        "required_capability": req.required_capability,
        "priority": req.priority,
    }
    state.tasks[task_id] = task
    logger.info(
        "Task submitted: %s (%s) -> %s", task_id, req.name, assigned or "pending"
    )
    return task


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: dict | None = None):
    task = state.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["status"] = "cancelled"
    task["cancel_reason"] = (body or {}).get("reason", "")
    for nid in task.get("assigned_nodes", []):
        node = state.nodes.get(nid)
        if node:
            node["active_tasks"] = max(0, node["active_tasks"] - 1)
    logger.info("Task cancelled: %s", task_id)
    return {"task_id": task_id, "status": "cancelled"}


@app.post("/api/tasks/{task_id}/degrade")
async def degrade_task(task_id: str, body: dict | None = None):
    task = state.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    target = (body or {}).get("target_model")
    task["status"] = "degraded"
    task["degraded_from_model"] = task.get("model_name", "")
    if target:
        task["model_name"] = target
    task["degradation_count"] = task.get("degradation_count", 0) + 1
    logger.info("Task degraded: %s -> %s", task_id, target)
    return {"task_id": task_id, "status": "degraded"}


@app.post("/api/tasks/{task_id}/migrate")
async def migrate_task(task_id: str, body: dict):
    task = state.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    target_node = body.get("target_node_id", "")
    old_nodes = task.get("assigned_nodes", [])
    for nid in old_nodes:
        node = state.nodes.get(nid)
        if node:
            node["active_tasks"] = max(0, node["active_tasks"] - 1)
    task["assigned_nodes"] = [target_node] if target_node else []
    target = state.nodes.get(target_node)
    if target:
        target["active_tasks"] += 1
    logger.info("Task migrated: %s -> %s", task_id, target_node)
    return {
        "task_id": task_id,
        "status": "running",
        "assigned_nodes": task["assigned_nodes"],
    }


@app.get("/api/v1/tasks/{task_id}/progress")
async def task_progress(task_id: str):
    task = state.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "name": task.get("name"),
        "status": task.get("status"),
        "progress": 1.0 if task["status"] == "completed" else 0.0,
        "total_shards": 1,
        "completed_shards": 1 if task["status"] == "completed" else 0,
        "assigned_nodes": task.get("assigned_nodes"),
        "elapsed_seconds": None,
        "remaining_seconds": None,
        "model_name": task.get("model_name"),
    }


@app.get("/api/v1/tasks/{task_id}/timeline")
async def task_timeline(task_id: str):
    task = state.tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = []
    if task.get("created_at"):
        events.append(
            {
                "timestamp": str(task["created_at"]),
                "event": "created",
                "detail": "Task created",
            }
        )
    if task.get("started_at"):
        events.append(
            {
                "timestamp": str(task["started_at"]),
                "event": "started",
                "detail": "Execution started",
            }
        )
    if task["status"] in ("completed", "failed", "cancelled"):
        ts = (
            task.get("completed_at") or task.get("started_at") or task.get("created_at")
        )
        events.append(
            {
                "timestamp": str(ts or ""),
                "event": task["status"],
                "detail": f"Task {task['status']}",
            }
        )
    return {
        "task_id": task_id,
        "name": task.get("name"),
        "status": task.get("status"),
        "events": events,
    }


@app.get("/api/v1/autoscaler/config")
async def get_autoscaler_config():
    return state.autoscaler_config


@app.put("/api/v1/autoscaler/config")
async def update_autoscaler_config(body: dict):
    for k, v in body.items():
        if k in state.autoscaler_config:
            state.autoscaler_config[k] = v
    return state.autoscaler_config


@app.get("/api/v1/observability/suggestions")
async def get_suggestions():
    suggestions = []
    util = _cluster_utilization()
    if util > 0.8:
        suggestions.append(
            {
                "priority": "high",
                "category": "scaling",
                "title": "Cluster under high load",
                "suggestion": "Consider adding more nodes or scaling up",
                "related_alert": None,
            }
        )
    if len(state.nodes) < 2:
        suggestions.append(
            {
                "priority": "medium",
                "category": "reliability",
                "title": "Single node cluster",
                "suggestion": "Add worker nodes for redundancy",
                "related_alert": None,
            }
        )
    return {"suggestions": suggestions, "error": None}


@app.get("/api/v1/observability/alerts")
async def get_alerts():
    return {"alerts": state.alerts}


@app.get("/api/v1/observability/logs/export")
async def export_logs():
    import json as _json

    log_lines = []
    for task in state.tasks.values():
        log_lines.append(_json.dumps(task))
    return "\n".join(log_lines)


@app.get("/api/routing/summary")
async def routing_summary():
    nodes_info = []
    total_load = 0.0
    for n in state.nodes.values():
        load = n["active_tasks"] / n["max_tasks"] if n["max_tasks"] > 0 else 0
        nodes_info.append(
            {
                "node_id": n["node_id"],
                "load": round(load, 4),
                "active_tasks": n["active_tasks"],
                "max_tasks": n["max_tasks"],
            }
        )
        total_load += load
    avg = total_load / len(nodes_info) if nodes_info else 0
    return {
        "strategy": state.routing_strategy,
        "nodes": nodes_info,
        "total_load": round(total_load, 4),
        "avg_load": round(avg, 4),
    }


@app.post("/api/routing/strategy")
async def set_routing_strategy(body: dict):
    strategy = body.get("strategy", "least_loaded")
    state.routing_strategy = strategy
    logger.info("Routing strategy set to: %s", strategy)
    return {"strategy": strategy}


@app.post("/api/kv/register")
async def register_kv_cache(body: dict):
    cache_id = body.get("cache_id", uuid.uuid4().hex[:12])
    state.kv_cache[cache_id] = {
        "cache_id": cache_id,
        "model_name": body.get("model_name", ""),
        "node_id": body.get("node_id", ""),
        "size_mb": body.get("size_mb", 0),
        "access_count": 0,
        "ttl_seconds": body.get("ttl_seconds", 3600),
        "created_at": time.time(),
    }
    logger.info("KV cache registered: %s", cache_id)
    return {"cache_id": cache_id, "status": "ok"}


@app.get("/api/kv/find/{model_name}")
async def find_kv_cache(model_name: str):
    for entry in state.kv_cache.values():
        if entry["model_name"] == model_name:
            return entry
    raise HTTPException(status_code=404, detail="KV cache not found for model")


def _cluster_utilization() -> float:
    total = sum(n["total_memory_gb"] for n in state.nodes.values())
    avail = sum(n["available_memory_gb"] for n in state.nodes.values())
    return (1 - avail / total) if total > 0 else 0


def run_cluster_server(host: str = "127.0.0.1", port: int = CLUSTER_PORT):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


# /v1/ aliases for consistency with main API (issue #78)
_V1_ALIASES = {
    "/api/health": "/v1/cluster/health",
    "/api/v1/cluster/stats": "/v1/cluster/stats",
    "/api/nodes": "/v1/cluster/nodes",
    "/api/v1/nodes/{node_id}/metrics": "/v1/cluster/nodes/{node_id}/metrics",
    "/api/join": "/v1/cluster/join",
    "/api/tasks": "/v1/cluster/tasks",
    "/api/tasks/submit": "/v1/cluster/tasks/submit",
}
for _old, _new in _V1_ALIASES.items():
    for _route in app.routes:
        if hasattr(_route, "path") and _route.path == _old:
            app.routes.append(type(_route)(
                path=_new,
                endpoint=_route.endpoint,
                methods=_route.methods,
            ))
            break


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_cluster_server()
