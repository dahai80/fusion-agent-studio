"""API server — FastAPI + WebSocket for Fusion Agent Studio.

Provides REST endpoints for graph management, execution, and monitoring.
WebSocket for real-time event streaming during graph execution.
Callers: fusion-studio GUI, external API clients, SDK clients.
Affected API: /v1/* REST endpoints, WebSocket /ws/execute/*
Data schemas: AgentStatusTracker, ArtifactManager, KnowledgeBaseManager, AuditLogger, etc.
User instruction: "对比fusion-agent-studio看还有哪些缺失，尽快补齐"
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from agent_runtime.errors import ErrorCode, raise_api_error
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)


def _app_version() -> str:
    # Single source of truth is pyproject; stale hardcoded strings misreport
    # the running build in health/status payloads.
    try:
        from importlib.metadata import version

        return version("fusion-agent-studio")
    except Exception:
        return "unknown"


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("Fusion Agent Studio API starting")
    yield
    for sid, task in _active_sessions.items():
        if not task.done():
            task.cancel()
    logger.info("Fusion Agent Studio API shutting down")


def _resolve_cors_origins() -> list[str]:
    # 审计 D-6: 默认仅 localhost origin, 防 CSWSH/CSRF 从浏览器跨源打 daemon.
    # env FUSION_API_CORS_ORIGINS=a,b 显式扩展.
    env_origins = os.environ.get("FUSION_API_CORS_ORIGINS", "").strip()
    if env_origins:
        return [o.strip() for o in env_origins.split(",") if o.strip()]
    return [
        "http://127.0.0.1",
        "http://localhost",
        "http://127.0.0.1:11442",
        "http://localhost:11442",
    ]


app = FastAPI(title="Fusion Agent Studio API", version="0.3.27", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # 审计 D-6: CORS 收紧. 原 allow_origins=["*"]+allow_credentials=True 是
    # 反模式, 任意网页 JS 可向 127.0.0.1:11442 发简单请求触发图执行 (RCE).
    # 默认仅允许 localhost origin; FUSION_API_CORS_ORIGINS 可配多源 (逗号分隔).
    # 不再 allow_credentials=True (本地 daemon 无需凭据跨源).
    allow_origins=_resolve_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS"],
    allow_headers=["x-api-key", "content-type", "authorization"],
)

# #271: env-gated multi-tenant identity. FUSION_IDENTITY_ENABLED=1 installs the
# fusion-core TenantMiddleware (X-Tenant-Id header + bearer JWT verify via
# fusion-identity:11470). Unset = current local ApiKeyManager behavior unchanged.
try:
    from agent_runtime.identity_integration import install_identity_middleware

    install_identity_middleware(app)
except Exception as _identity_err:  # noqa: BLE001 — never block server boot
    logger.warning("identity middleware install skipped: %s", _identity_err)


class GraphCreateRequest(BaseModel):
    name: str = ""
    description: str = ""
    graph_data: dict


class GraphExecuteRequest(BaseModel):
    graph_id: str
    input_text: str = ""
    session_id: str = ""
    variables: dict[str, Any] = {}


class GraphResponse(BaseModel):
    graph_id: str
    name: str
    description: str
    graph_data: dict


class ExecutionResponse(BaseModel):
    session_id: str
    events: list[dict]
    status: str


_store: AgentStore | None = None
_runtime: AgentRuntime | None = None
_active_sessions: dict[str, asyncio.Task] = {}
_daemon: Any = None


def set_daemon(daemon: Any) -> None:
    global _daemon
    _daemon = daemon


def _get_store() -> AgentStore:
    global _store
    if _store is None:
        # #500: daemon 模式必须复用 daemon.store —— --store-path 测试隔离对
        # REST/SSE 图端点同样生效; 此前这里懒建默认用户库, 隔离形同虚设.
        daemon_store = getattr(_daemon, "store", None)
        _store = daemon_store if daemon_store is not None else AgentStore()
        logger.info(
            "api_server store resolved: %s",
            getattr(_store, "db_path", "<default>"),
        )
    return _store


def _get_runtime() -> AgentRuntime:
    # #327: REST/WS/SSE 直调路径曾各自懒建裸 runtime (llm_gateway=empty),
    # 真实 LLM 节点落到 "No available model" 且静默空输出 (仅 start/think ""
    # /done, 无 token 事件). daemon 模式下复用 daemon 已接线 runtime
    # (gateway+safety+memory+telemetry), 与 graph.execute_async 同源.
    daemon_rt = getattr(_daemon, "_get_runtime", None)
    if daemon_rt is not None:
        try:
            rt = daemon_rt()
            if rt is not None:
                return rt
        except Exception as e:
            logger.warning("daemon runtime fetch failed, falling back: %s", e)
    global _runtime
    if _runtime is None:
        from tools import create_default_registry

        _runtime = AgentRuntime(tool_registry=create_default_registry())
        # #498: LLM-facing SKILL.md tools on the top-level REST runtime too.
        _runtime._register_skill_tools()
        # #488: ToolSearch ON by default — same rule, top-level surface only.
        _runtime._register_toolsearch()
        # #487: subagent_spawn ON by default — same rule, top-level surface only.
        _runtime._register_subagent_tools()
    return _runtime


def _get_status_tracker():
    global _status_tracker
    if _status_tracker is None:
        from agent_runtime.agent_api import AgentStatusTracker

        _status_tracker = AgentStatusTracker()
    return _status_tracker


def _get_artifact_mgr():
    global _artifact_mgr
    if _artifact_mgr is None:
        from agent_runtime.artifact_tools import ArtifactManager

        _artifact_mgr = ArtifactManager()
    return _artifact_mgr


def _get_kb_manager():
    global _kb_manager
    if _kb_manager is None:
        from agent_runtime.knowledge_base import KnowledgeBaseManager

        _kb_manager = KnowledgeBaseManager()
    return _kb_manager


def _get_version_store():
    global _version_store
    if _version_store is None:
        from agent_runtime.agent_version import AgentVersionStore

        _version_store = AgentVersionStore()
    return _version_store


def _get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        from agent_runtime.audit_logger import AuditLogger

        _audit_logger = AuditLogger()
    return _audit_logger


def _get_metrics_engine():
    global _metrics_engine
    if _metrics_engine is None:
        from agent_runtime.metrics_engine import MetricsEngine

        _metrics_engine = MetricsEngine()
    return _metrics_engine


def _get_telemetry():
    global _telemetry
    if _telemetry is None:
        from agent_runtime.telemetry import TelemetryEngine

        _telemetry = TelemetryEngine()
    return _telemetry


_status_tracker = None
_artifact_mgr = None
_kb_manager = None
_version_store = None
_audit_logger = None
_metrics_engine = None
_telemetry = None


def _load_agents_index() -> dict:
    if _daemon is not None and hasattr(_daemon, "_agents") and _daemon._agents:
        return dict(_daemon._agents)
    idx_path = Path.home() / ".fusion-agent-studio" / "agents" / "index.json"
    if idx_path.exists():
        try:
            with open(idx_path) as f:
                return json.load(f)
        except Exception:
            pass
    index = _rebuild_agents_index_from_disk()
    if index:
        logger.info("agents index rebuilt from disk: %d agents", len(index))
    return index


def _rebuild_agents_index_from_disk() -> dict:
    agents_root = Path.home() / ".fusion-agent-studio" / "agents"
    if not agents_root.is_dir():
        return {}
    index: dict[str, dict] = {}
    for agent_dir in agents_root.iterdir():
        if not agent_dir.is_dir():
            continue
        manifest_path = agent_dir / ".fusion-agent" / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skip unreadable manifest %s: %s", manifest_path, e)
            continue
        agent_id = agent_dir.name
        meta["id"] = agent_id
        index[agent_id] = meta
    if index:
        idx_path = agents_root / "index.json"
        try:
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=4, ensure_ascii=False)
            logger.info("persisted rebuilt agents index -> %s", idx_path)
        except OSError as e:
            logger.warning("failed to persist agents index: %s", e)
    return index


def _paginate(data: list, page: int = 1, limit: int = 20) -> dict:
    total = len(data)
    start = (page - 1) * limit
    end = start + limit
    return {
        "data": data[start:end],
        "total": total,
        "page": page,
        "limit": limit,
    }


def _static_env_key() -> str:
    # #327: operator-set static key for gateway/module integration
    # (FUSION_AGENT_STUDIO_API_KEY). Loopback deployments use a documented
    # local default (see web/README.md); remote deployments must override.
    # Falls back to the persisted module key file so launchd-started
    # daemons (no shell env) keep the same key as gateway config.
    from .shell_registry import static_module_key

    return static_module_key()


def _extract_caller_key(request: Request) -> str:
    # #327: accept x-api-key (native) or Authorization: Bearer (the gateway
    # /shell/<id>/ proxy injects the module api_key as a Bearer header).
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            api_key = authz[7:].strip()
    return api_key


def _check_static_key(api_key: str) -> dict | None:
    # #436 (K4): dual-accept env static key + gateway-issued fm- key
    # (persisted file) during the migration grace window.
    from .shell_registry import accepted_module_keys

    if not api_key:
        return None
    env_key = os.environ.get("FUSION_AGENT_STUDIO_API_KEY", "").strip()
    for key in accepted_module_keys():
        if hmac.compare_digest(key, api_key):
            key_id = "static-env" if key == env_key else "module-issued"
            return {"valid": True, "key_id": key_id, "permissions": ["agent:execute", "admin"]}
    return None


def _validate_key_result(api_key: str, client_ip: str = "") -> dict | None:
    if not api_key:
        return None
    static = _check_static_key(api_key)
    if static is not None:
        return static
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    result = mgr.validate(api_key, client_ip=client_ip)
    if result.get("valid"):
        return result
    return None


async def _optional_auth(request: Request) -> dict | None:
    api_key = _extract_caller_key(request)
    if not api_key:
        return None
    client_ip = request.client.host if request.client else ""
    return _validate_key_result(api_key, client_ip=client_ip)


async def _require_auth(request: Request) -> dict:
    api_key = _extract_caller_key(request)
    if not api_key:
        raise_api_error(ErrorCode.API_KEY_MISSING)
    result = await _optional_auth(request)
    if result is None:
        raise_api_error(ErrorCode.API_KEY_INVALID)
    return result


def _auth_configured() -> bool:
    # 审计 D-6: 若已配置任意 api-key, 则所有变更端点强制鉴权 (生产模式).
    # 未配置任何 key 时为本地受信模式, 放行 (向后兼容下游 fusion-studio/cli).
    # #327: FUSION_AGENT_STUDIO_API_KEY (gateway 静态模块 key) 单独配置即视为
    # 已配置 — 否则 gateway 部署只带静态 key 时端点仍全开放.
    if _static_env_key():
        return True
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    return bool(mgr.list_keys())


async def _require_auth_if_configured(request: Request) -> dict | None:
    # 审计 D-6: 配置了 api-key 则变更端点必须鉴权; 否则本地受信放行.
    if not _auth_configured():
        return None
    return await _require_auth(request)


def _validate_api_key_str(api_key: str) -> dict | None:
    # 审计 P0-2: query-string 鉴权校验. WS/SSE 浏览器 EventSource 不能设自定义 header,
    # 故走 ?api_key= query param. 复用 ApiKeyManager.validate.
    # 返回 validate result dict (含 valid) 或 None (空 key).
    if not api_key:
        return None
    return _validate_key_result(api_key)


def _require_auth_qs_or_raise(request: Request, api_key: str = Query("")) -> dict | None:
    # 审计 P0-2: SSE (GET) 用 FastAPI Depends, 校验 query-string api_key.
    # #327: gateway /shell 代理注入 Bearer 头 (EventSource 无法带 query key 的
    # 嵌入路径), Bearer 与 query 二选一.
    if not _auth_configured():
        return None
    if not api_key:
        api_key = _extract_caller_key(request)
    if not api_key:
        raise_api_error(ErrorCode.API_KEY_MISSING)
    result = _validate_api_key_str(api_key)
    if result is None or not result.get("valid"):
        raise_api_error(ErrorCode.API_KEY_INVALID)
    return result


def _ws_auth_ok(api_key: str) -> tuple[bool, str]:
    # 审计 P0-2: WS 握手前校验 query-string api_key (accept 后无法干净抛 HTTP).
    # 返回 (ok, reason). 配置了 key 则必须有效; 否则本地受信放行.
    if not _auth_configured():
        return True, ""
    if not api_key:
        return False, "api key required"
    result = _validate_api_key_str(api_key)
    if result is None or not result.get("valid"):
        return False, "api key invalid"
    return True, ""


@app.get("/health")
async def health():
    # 审计 P1-26: 原静态 {"status":"ok"} 是健康谎报 — 不探 MLX 可达性, MLX 宕
    # 时仍报 ok 致负载均衡/监控误路由. 现探 daemon 网关 MLX 可达性, 不可达降级.
    mlx_reachable = False
    if _daemon is not None and hasattr(_daemon, "_check_mlx_health"):
        try:
            mlx_reachable = await _daemon._check_mlx_health()
        except Exception as e:
            logger.warning("/health MLX probe failed: %s", e)
    return {
        "status": "ok" if mlx_reachable else "degraded",
        "mlx_reachable": mlx_reachable,
        "version": _app_version(),
        "persistence": "sqlite",
    }


@app.get("/metrics")
async def prometheus_metrics():
    # 审计 P2-21: Prometheus 拉取端点. 暴露 telemetry counters/latencies +
    # daemon active executions/queue, 文本 exposition 格式, 接 Grafana/Prometheus.
    # 无密钥 (metrics 不含 secret), 但生产部署应在反代层限源 IP.
    lines: list[str] = []
    tele = _get_telemetry()
    m = tele.metrics() if tele else {}
    for name, val in (m.get("counters") or {}).items():
        lines.append(f"# TYPE fusion_counter_{name} counter")
        lines.append(f"fusion_counter_{name} {val}")
    for name, val in (m.get("latencies") or {}).items():
        lines.append(f"# TYPE fusion_latency_{name} gauge")
        lines.append(f"fusion_latency_{name} {val}")
    lines.append("# TYPE fusion_telemetry_spans_total gauge")
    lines.append(f"fusion_telemetry_spans_total {m.get('total_spans', 0)}")
    lines.append("# TYPE fusion_telemetry_traces_total gauge")
    lines.append(f"fusion_telemetry_traces_total {m.get('total_traces', 0)}")
    lines.append("# TYPE fusion_telemetry_active_spans gauge")
    lines.append(f"fusion_telemetry_active_spans {m.get('active_spans', 0)}")
    if _daemon is not None:
        active_exec = len(getattr(_daemon, "_active_executions", {}) or {})
        lines.append("# TYPE fusion_daemon_active_executions gauge")
        lines.append(f"fusion_daemon_active_executions {active_exec}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


# ── /v1 versioned routes ──


@app.get("/v1/health")
async def v1_health():
    mlx_reachable = False
    if _daemon is not None and hasattr(_daemon, "_check_mlx_health"):
        try:
            mlx_reachable = await _daemon._check_mlx_health()
        except Exception as e:
            logger.warning("/v1/health MLX probe failed: %s", e)
    return {
        "status": "ok" if mlx_reachable else "degraded",
        "mlx_reachable": mlx_reachable,
        "version": _app_version(),
        "persistence": "sqlite",
    }


@app.get("/v1/dashboard")
async def get_dashboard(request: Request):
    tracker = _get_status_tracker()
    agents_index = _load_agents_index()
    published = tracker.list_published(agents_index)
    active_count = len([a for a in published if a.get("status") == "published"])
    metrics = _get_metrics_engine()
    tele = _get_telemetry()
    tele_metrics = tele.metrics() if tele else {}
    recent_sessions = metrics.query_sessions(status="", limit=5) if metrics else []
    today_ts = time.time() - 86400
    today_sessions = (
        [
            s
            for s in metrics.query_sessions(status="", limit=1000)
            if getattr(s, "started_at", 0) > today_ts
        ]
        if metrics
        else []
    )
    error_count = len(
        [s for s in today_sessions if getattr(s, "status", "") == "error"]
    )
    dashboard = {
        "today_requests": len(today_sessions),
        "total_token_consumption": tele_metrics.get("tokens", {}).get("total", 0),
        "active_agents": active_count,
        "error_count": error_count,
        "recent_agents": published[:5],
        "recent_errors": [
            s.to_dict() if hasattr(s, "to_dict") else s
            for s in recent_sessions
            if getattr(s, "status", "") == "error"
        ][:5],
        "telemetry": tele_metrics,
    }
    logger.info(
        "dashboard: requests=%d active=%d errors=%d",
        len(today_sessions),
        active_count,
        error_count,
    )
    return dashboard


# ── Graph endpoints ──


@app.post("/v1/graphs", response_model=GraphResponse, dependencies=[Depends(_require_auth_if_configured)])
async def v1_create_graph(req: GraphCreateRequest):
    from agent_runtime.graph import AgentGraph

    if not isinstance(req.graph_data, dict):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="graph_data")
    if "nodes" not in req.graph_data:
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="graph_data", detail="graph_data must contain 'nodes'")

    graph = AgentGraph.from_dict(req.graph_data)
    if req.name:
        graph.name = req.name
    if req.description:
        graph.description = req.description
    _get_store().save_graph(graph)
    logger.info("Created graph %s: %s", graph.id, graph.name)
    return GraphResponse(
        graph_id=graph.id,
        name=graph.name,
        description=graph.description,
        graph_data=graph.to_dict(),
    )


@app.get("/v1/graphs")
async def v1_list_graphs(
    page: int = 1,
    limit: int = 20,
    sort_field: str = "created_at",
    sort_order: str = "desc",
):
    graphs = _get_store().list_graphs()
    results = []
    for g in graphs:
        full = _get_store().load_graph(g["id"])
        if full:
            results.append(
                GraphResponse(
                    graph_id=full.id,
                    name=full.name,
                    description=full.description,
                    graph_data=full.to_dict(),
                ).model_dump()
            )
    if sort_order == "asc":
        results.reverse()
    return _paginate(results, page, limit)


@app.get("/v1/graphs/{graph_id}", response_model=GraphResponse)
async def v1_get_graph(graph_id: str):
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="graph_id")
    return GraphResponse(
        graph_id=graph.id,
        name=graph.name,
        description=graph.description,
        graph_data=graph.to_dict(),
    )


@app.delete("/v1/graphs/{graph_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_delete_graph(graph_id: str):
    deleted = _get_store().delete_graph(graph_id)
    if not deleted:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="graph_id")
    logger.info("Deleted graph %s", graph_id)
    return {"deleted": True}


@app.post("/v1/graphs/{graph_id}/execute", response_model=ExecutionResponse, dependencies=[Depends(_require_auth_if_configured)])
async def v1_execute_graph(graph_id: str, req: GraphExecuteRequest):
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="graph_id")
    rt = _get_runtime()
    session_id = req.session_id or str(uuid.uuid4())[:8]
    events = []
    try:
        async for event in rt.execute_graph(graph, req.input_text):
            ev_dict = (
                event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
            )
            events.append(ev_dict)
    except Exception:
        logger.exception("Graph execution failed: %s", graph_id)
        return ExecutionResponse(session_id=session_id, events=events, status="error")
    logger.info("Graph %s executed: %d events", graph_id, len(events))
    return ExecutionResponse(session_id=session_id, events=events, status="completed")


# ── Agent endpoints ──


@app.get("/v1/agents")
async def v1_list_agents(
    page: int = 1, limit: int = 20, status: str = "", keyword: str = ""
):
    agents_index = _load_agents_index()
    agents = list(agents_index.values())
    if status:
        agents = [a for a in agents if a.get("status") == status]
    if keyword:
        agents = [a for a in agents if keyword.lower() in a.get("name", "").lower()]
    return _paginate(agents, page, limit)


@app.get("/v1/agents/published")
async def v1_list_published_agents():
    agents_index = _load_agents_index()
    tracker = _get_status_tracker()
    agents = tracker.list_published(agents_index)
    return {"agents": agents}


@app.get("/v1/agents/{agent_id}/definition")
async def v1_get_agent_definition(agent_id: str):
    agents_index = _load_agents_index()
    manifest_data = agents_index.get(agent_id)
    if not manifest_data:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    tracker = _get_status_tracker()
    return tracker.get_definition(manifest_data)


@app.get("/v1/agents/{agent_id}/status")
async def v1_get_agent_status(agent_id: str):
    tracker = _get_status_tracker()
    status = tracker.get_status(agent_id)
    return {
        "agent_id": agent_id,
        "status": status.to_dict() if hasattr(status, "to_dict") else status,
    }


@app.get("/v1/agents/{agent_id}/history")
async def v1_get_agent_history(agent_id: str, limit: int = 20, page: int = 1):
    tracker = _get_status_tracker()
    history = tracker.get_history(agent_id, limit=limit * page)
    items = [h.to_dict() if hasattr(h, "to_dict") else h for h in history]
    return _paginate(items, page, limit)


@app.get("/v1/agents/{agent_id}/artifacts")
async def v1_list_agent_artifacts(agent_id: str):
    mgr = _get_artifact_mgr()
    artifacts = mgr.list_artifacts(agent_id=agent_id)
    return {"artifacts": artifacts}


@app.get("/v1/agents/{agent_id}/preview")
async def v1_get_agent_preview(agent_id: str):
    agents_index = _load_agents_index()
    meta = agents_index.get(agent_id)
    if not meta:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    kb_ids = meta.get("knowledge_base_ids", [])
    rag_strategy = meta.get("rag_strategy", "")
    preview = {
        "agentId": agent_id,
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "avatar": meta.get("style", "") or "🤖",
        "tools": meta.get("tools", []),
        "ragEnabled": bool(kb_ids) or rag_strategy not in ("none", ""),
        "permissions": meta.get(
            "permissions",
            {
                "readKnowledge": bool(kb_ids),
                "writeKnowledge": False,
                "deleteKnowledge": False,
                "executeCode": "code_execution" in meta.get("tools", []),
                "accessNetwork": meta.get("web_search_enabled", False),
            },
        ),
    }
    return {"preview": preview}


@app.post("/v1/agents/{agent_id}/test", dependencies=[Depends(_require_auth_if_configured)])
async def v1_test_agent(
    agent_id: str, project_id: str = "", kb_id: str = "", message: str = "hello"
):
    if not message:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="message")
    agents_index = _load_agents_index()
    meta = agents_index.get(agent_id)
    if not meta:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    effective_kb = kb_id or (
        meta.get("knowledge_base_ids", [""])[0]
        if meta.get("knowledge_base_ids")
        else ""
    )
    return {
        "agent_id": agent_id,
        "project_id": project_id,
        "kb_id": effective_kb,
        "status": "test_dispatched",
        "message": message,
    }


@app.post("/v1/agents/{agent_id}/duplicate", dependencies=[Depends(_require_auth_if_configured)])
async def v1_duplicate_agent(agent_id: str):
    agents_index = _load_agents_index()
    meta = agents_index.get(agent_id)
    if not meta:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    try:
        from agent_runtime.agent_package import AgentPackage

        pkg = AgentPackage(agent_id)
        new_pkg = pkg.fork()
        logger.info("Duplicated agent %s -> %s", agent_id, new_pkg.agent_id)
        _get_audit_logger().log_action(
            actor_id="api",
            action="agent.duplicate",
            resource_type="agent",
            resource_id=agent_id,
            result="success",
        )
        return {"original_id": agent_id, "new_agent_id": new_pkg.agent_id}
    except Exception as exc:
        logger.error("Failed to duplicate agent %s: %s", agent_id, exc)
        raise_api_error(ErrorCode.INTERNAL_ERROR, detail=str(exc))


@app.post("/v1/agents/{agent_id}/snapshot", dependencies=[Depends(_require_auth_if_configured)])
async def v1_snapshot_agent(agent_id: str, label: str = ""):
    agents_index = _load_agents_index()
    meta = agents_index.get(agent_id)
    if not meta:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    vs = _get_version_store()
    record = vs.save_snapshot(agent_id, snapshot_data=meta, label=label)
    logger.info("Snapshot agent %s: version=%s", agent_id, record.version_id)
    _get_audit_logger().log_action(
        actor_id="api",
        action="agent.snapshot",
        resource_type="agent",
        resource_id=agent_id,
        details={"version_id": record.version_id},
        result="success",
    )
    return {
        "version_id": record.version_id,
        "agent_id": agent_id,
        "label": label,
        "created_at": record.created_at,
    }


@app.get("/v1/agents/{agent_id}/versions")
async def v1_list_agent_versions(agent_id: str, page: int = 1, limit: int = 20):
    vs = _get_version_store()
    versions = vs.list_versions(agent_id)
    items = [v.to_dict() for v in versions]
    return _paginate(items, page, limit)


@app.post("/v1/agents/{agent_id}/versions/{version_id}/restore", dependencies=[Depends(_require_auth_if_configured)])
async def v1_restore_agent_version(agent_id: str, version_id: str):
    vs = _get_version_store()
    snapshot = vs.restore_version(agent_id, version_id)
    if snapshot is None:
        raise_api_error(ErrorCode.RESOURCE_NOT_FOUND, param="version_id")
    logger.info("Restored agent %s to version %s", agent_id, version_id)
    _get_audit_logger().log_action(
        actor_id="api",
        action="agent.restore_version",
        resource_type="agent",
        resource_id=agent_id,
        details={"version_id": version_id},
        result="success",
    )
    return {"agent_id": agent_id, "version_id": version_id, "snapshot_data": snapshot}


# ── Test-agent scheduling endpoints (#285) ──
# #285: minimal, documented REST contract so external automated test platforms
# (e.g. fusion-autotest) can submit/poll/cancel agent runs without speaking
# JSON-RPC. These wrap the existing task.* RPC handlers on the daemon (which
# own the TaskStore + cron registration). Env-gated: when the daemon is not
# wired into the API server (CLI-only mode), these return a 503 with a clear
# message instead of crashing.


def _daemon_or_503():
    if _daemon is None:
        raise_api_error(
            ErrorCode.INTERNAL_ERROR,
            detail="daemon not attached to API server; start via daemon mode (start.sh) to use /v1/runs",
        )
    return _daemon


async def _rpc(method: str, params: dict) -> dict:
    daemon = _daemon_or_503()
    handler = daemon._get_handler(method)
    if handler is None:
        raise_api_error(ErrorCode.INTERNAL_ERROR, detail=f"RPC method not found: {method}")
    return await handler(params)


@app.post("/v1/runs", dependencies=[Depends(_require_auth_if_configured)])
async def v1_submit_run(request: Request):
    body = await request.json()
    goal = str(body.get("goal", "")).strip()
    if not goal:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="goal")
    timeout = float(body.get("timeout", 0) or 0)
    constraints = body.get("constraints", {}) or {}
    agent_id = str(body.get("agent_id", "") or "")
    graph_id = str(body.get("graph_id", "") or "")
    title = str(body.get("title", "") or goal[:80])
    idempotency_key = str(body.get("idempotency_key", "") or "")
    # #460: per-run agent overrides — same SubAgentSpec fields the UDS
    # subagent.run channel accepts, so HTTP-only deployments get equivalent
    # per-run tools/model/max_turns/budget_tokens control.
    spec = body.get("spec")
    if spec is None:
        spec = {}
    elif not isinstance(spec, dict):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec", detail="spec must be an object")
    else:
        spec = dict(spec)
    for key in ("tools", "model", "max_turns", "budget_tokens"):
        if body.get(key) is not None:
            spec.setdefault(key, body[key])
    # #461: agent_overrides alias — same SubAgentSpec constraint fields
    # under the stricter /v1/runs contract: tools are constraints, never
    # expansions (intersected with the target agent's own allowlist),
    # unknown tool names 400, effective overrides surfaced in the
    # response, run status/result and the audit log.
    agent_overrides = body.get("agent_overrides")
    if agent_overrides is not None:
        if not isinstance(agent_overrides, dict):
            raise_api_error(
                ErrorCode.PARAM_FORMAT_ERROR, param="agent_overrides",
                detail="agent_overrides must be an object",
            )
        _OVERRIDE_KEYS = {
            "tools", "disallowed_tools", "model", "max_turns", "budget_tokens",
        }
        unknown = sorted(set(agent_overrides) - _OVERRIDE_KEYS)
        if unknown:
            raise_api_error(
                ErrorCode.PARAM_FORMAT_ERROR, param="agent_overrides",
                detail=f"unsupported agent_overrides keys: {', '.join(unknown)}",
            )
        for key in ("tools", "disallowed_tools"):
            if key in agent_overrides and not isinstance(agent_overrides[key], list):
                raise_api_error(
                    ErrorCode.PARAM_FORMAT_ERROR, param=f"agent_overrides.{key}"
                )
        if "model" in agent_overrides and not isinstance(agent_overrides["model"], str):
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="agent_overrides.model")
        try:
            for key in ("max_turns", "budget_tokens"):
                if key in agent_overrides:
                    agent_overrides[key] = int(agent_overrides[key])
        except (TypeError, ValueError):
            raise_api_error(
                ErrorCode.PARAM_FORMAT_ERROR,
                param="agent_overrides.max_turns, agent_overrides.budget_tokens",
            )
        for key, val in dict(agent_overrides).items():
            spec.setdefault(key, val)
    _SPEC_ALLOWED_KEYS = {
        "name", "model", "tools", "max_turns", "budget_tokens", "system_prompt",
        "disallowed_tools", "mcp_servers", "skills", "permission_mode", "effort",
        "team",
    }
    unknown = sorted(set(spec) - _SPEC_ALLOWED_KEYS)
    if unknown:
        raise_api_error(
            ErrorCode.PARAM_FORMAT_ERROR, param="spec",
            detail=f"unsupported spec keys: {', '.join(unknown)}",
        )
    if spec:
        if "tools" in spec and not isinstance(spec["tools"], list):
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec.tools")
        if "model" in spec and not isinstance(spec["model"], str):
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec.model")
        try:
            if "max_turns" in spec:
                spec["max_turns"] = int(spec["max_turns"])
            if "budget_tokens" in spec:
                spec["budget_tokens"] = int(spec["budget_tokens"])
        except (TypeError, ValueError):
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec.max_turns, spec.budget_tokens")
        if "max_turns" in spec and spec["max_turns"] <= 0:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec.max_turns",
                            detail="max_turns must be > 0")
        if "budget_tokens" in spec and spec["budget_tokens"] < 0:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="spec.budget_tokens",
                            detail="budget_tokens must be >= 0")
    # #461: resolve effective overrides BEFORE task.submit so a 400 never
    # leaves an orphan pending task behind. Tools are constraints: the
    # caller allowlist is intersected with the target agent's own tools
    # (a caller cannot grant tools the agent doesn't have).
    effective_overrides: dict = {}
    if spec:
        for key in ("tools", "disallowed_tools", "model", "max_turns", "budget_tokens"):
            if key in spec:
                effective_overrides[key] = spec[key]
        if spec.get("tools"):
            toolset = await _rpc("subagent.toolset", {"tools": spec["tools"]})
            available = {str(t) for t in (toolset or {}).get("available") or []}
            if available:
                unknown_tools = sorted({str(t) for t in spec["tools"]} - available)
                if unknown_tools:
                    raise_api_error(
                        ErrorCode.PARAM_FORMAT_ERROR, param="agent_overrides.tools",
                        detail=f"unknown tool names: {', '.join(unknown_tools)}",
                    )
                if agent_id:
                    own_tools: set[str] = set()
                    try:
                        ares = await _rpc("agent.get", {"agent_id": agent_id})
                        for entry in (ares.get("agent") or {}).get("tools") or []:
                            own_tools.add(
                                entry.get("name") if isinstance(entry, dict) else str(entry)
                            )
                    except Exception as exc:
                        logger.warning(
                            "/v1/runs agent.get for override intersection failed agent=%s: %s",
                            agent_id, exc,
                        )
                    if own_tools:
                        spec["tools"] = [t for t in spec["tools"] if t in own_tools]
                        effective_overrides["tools"] = list(spec["tools"])
                        logger.info(
                            "/v1/runs override intersection agent=%s effective_tools=%s",
                            agent_id, spec["tools"],
                        )
            else:
                logger.warning(
                    "/v1/runs toolset unavailable — skipping tool name validation"
                )
    # #432: sandbox selection — local (default) or multi-node:<node_id>.
    from agent_runtime.sandbox_router import delegate_run, parse_sandbox

    try:
        sandbox = parse_sandbox(str(body.get("sandbox", "") or ""))
    except ValueError as e:
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="sandbox", detail=str(e))
    # Submit a task record (immediate trigger). The task store owns the run_id.
    submit_params = {
        "title": title,
        "description": goal,
        "agent_id": agent_id,
        "graph_id": graph_id,
        "trigger": "immediate",
        "input": goal,
        "status": "pending",
        "idempotency_key": idempotency_key,
        "sandbox": sandbox.raw,
    }
    # constraints -> task priority + max_retries (best-effort passthrough).
    if isinstance(constraints, dict):
        if "priority" in constraints:
            submit_params["priority"] = int(constraints["priority"])
        if "max_retries" in constraints:
            submit_params["max_retries"] = int(constraints["max_retries"])
    res = await _rpc("task.submit", submit_params)
    task = res.get("task", {}) if isinstance(res, dict) else {}
    run_id = task.get("task_id", "")
    logger.info(
        "/v1/runs submitted run_id=%s goal=%r timeout=%s sandbox=%s spec_keys=%s",
        run_id,
        goal[:60],
        timeout,
        sandbox.raw,
        sorted(spec) if spec else [],
    )
    # #460: spec overrides present -> run via the SubAgentRunner path
    # (same machinery as UDS subagent.run), task-linked background execution.
    # Cluster delegation keeps its own path — overrides are local-only for now.
    if spec and sandbox.is_local and run_id and not res.get("deduped"):
        # #461: who constrained what — overrides ride the run record + audit log.
        _get_audit_logger().log_action(
            actor_id="api",
            action="run.overrides",
            resource_type="task",
            resource_id=run_id,
            result="success",
            details={"agent_id": agent_id, "effective_overrides": effective_overrides},
        )
        run_res = await _rpc(
            "subagent.run_task",
            {
                "task_id": run_id,
                "spec": spec,
                "goal": goal,
                "agent_id": agent_id,
                "effective_overrides": effective_overrides,
            },
        )
        if isinstance(run_res, dict) and run_res.get("status") == "error":
            await _rpc(
                "task.status",
                {
                    "task_id": run_id,
                    "status": "failed",
                    "last_error": run_res.get("message", "subagent dispatch failed"),
                },
            )
            raise_api_error(
                ErrorCode.INTERNAL_ERROR,
                detail=run_res.get("message", "subagent dispatch failed"),
            )
        return {
            "run_id": run_id,
            "status": "running",
            "deduped": False,
            "spec_applied": True,
            "effective_overrides": effective_overrides,
        }
    if not sandbox.is_local and run_id:
        delegation = await delegate_run(sandbox, run_id, agent_id, graph_id, goal)
        if not delegation.get("ok"):
            await _rpc(
                "task.status",
                {
                    "task_id": run_id,
                    "status": "failed",
                    "last_error": delegation.get("error", "multi-node delegation failed"),
                },
            )
            raise_api_error(
                ErrorCode.INTERNAL_ERROR,
                detail=delegation.get("error", "multi-node delegation failed"),
            )
        logger.info(
            "/v1/runs %s delegated to cluster node=%s cluster_task=%s",
            run_id,
            sandbox.node,
            delegation.get("cluster_task_id", ""),
        )
        return {
            "run_id": run_id,
            "status": "running",
            "deduped": res.get("deduped", False),
            "sandbox": delegation,
        }
    # If a graph_id is bound, kick off execution immediately so the run
    # progresses without waiting for a separate cron tick. execute_async
    # backgrounds the graph run (evidence jsonl + task writeback) and returns
    # in <1s — the HTTP response must not block on a multi-minute LLM run
    # (the old synchronous graph.execute call held the POST open and timed
    # clients out).
    if graph_id:
        try:
            await _rpc(
                "graph.execute_async",
                {"graph_id": graph_id, "input": goal, "task_id": run_id},
            )
        except Exception as exc:
            logger.exception("/v1/runs graph.execute_async for %s failed (task still queued): %s", run_id, exc)
    return {"run_id": run_id, "status": task.get("status", "pending"), "deduped": res.get("deduped", False)}


# ---------------- #455: inbound webhooks ----------------

WEBHOOK_MAX_BODY_BYTES = 64 * 1024


def _webhook_mgr():
    from agent_runtime.triggers import get_webhook_manager

    return get_webhook_manager()


@app.get("/v1/webhooks", dependencies=[Depends(_require_auth_if_configured)])
async def v1_list_webhooks():
    return {"webhooks": _webhook_mgr().list()}


@app.post("/v1/webhooks", dependencies=[Depends(_require_auth_if_configured)])
async def v1_create_webhook(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    graph_id = str(body.get("graph_id", "")).strip()
    if not name or not graph_id:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="name, graph_id")
    try:
        rate_limit = int(body.get("rate_limit_per_min", 30) or 30)
    except (TypeError, ValueError):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="rate_limit_per_min")
    secret = str(body.get("secret", "") or "")
    webhook = _webhook_mgr().create(
        name=name, graph_id=graph_id, secret=secret, rate_limit_per_min=rate_limit
    )
    _get_audit_logger().log_action(
        actor_id="api",
        action="webhook.create",
        resource_type="webhook",
        resource_id=webhook.id,
        details={"name": name, "graph_id": graph_id},
        result="success",
    )
    # Secret is returned exactly once, at creation time.
    data = webhook.to_dict(include_secret=True)
    return {"webhook": data}


@app.delete("/v1/webhooks/{hook_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_delete_webhook(hook_id: str):
    if not _webhook_mgr().delete(hook_id):
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="hook_id",
                        detail=f"webhook not found: {hook_id}")
    _get_audit_logger().log_action(
        actor_id="api",
        action="webhook.delete",
        resource_type="webhook",
        resource_id=hook_id,
        result="success",
    )
    return {"deleted": hook_id}


@app.post("/v1/webhooks/{hook_id}/enabled", dependencies=[Depends(_require_auth_if_configured)])
async def v1_set_webhook_enabled(hook_id: str, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    webhook = _webhook_mgr().set_enabled(hook_id, enabled)
    if webhook is None:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="hook_id",
                        detail=f"webhook not found: {hook_id}")
    return {"webhook": webhook.to_dict()}


@app.post("/v1/webhooks/{hook_id}/invoke")
async def v1_invoke_webhook(hook_id: str, request: Request):
    # #455: external trigger surface. Auth is mandatory and per-hook:
    # HMAC-SHA256 hexdigest of the raw request body in X-Signature.
    mgr = _webhook_mgr()
    webhook = mgr.get(hook_id)
    if webhook is None or not webhook.enabled:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="hook_id",
                        detail=f"webhook not found: {hook_id}")
    content_length = int(request.headers.get("content-length", 0) or 0)
    if content_length > WEBHOOK_MAX_BODY_BYTES or content_length < 0:
        raise HTTPException(status_code=413, detail="payload exceeds 64KB limit")
    raw_body = await request.body()
    if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload exceeds 64KB limit")
    signature = request.headers.get("x-signature", "")
    client_ip = request.client.host if request.client else ""
    started = time.time()
    if not mgr.verify_signature(webhook, raw_body, signature):
        logger.warning("webhook invoke rejected bad signature hook=%s ip=%s", hook_id, client_ip)
        _get_audit_logger().log_action(
            actor_id=f"webhook:{hook_id}",
            action="webhook.invoke",
            resource_type="webhook",
            resource_id=hook_id,
            details={"ip": client_ip, "sig_valid": False, "run_id": ""},
            result="failure",
        )
        raise_api_error(ErrorCode.API_KEY_INVALID,
                        detail="invalid or missing X-Signature")
    if not mgr.check_rate_limit(webhook):
        _get_audit_logger().log_action(
            actor_id=f"webhook:{hook_id}",
            action="webhook.invoke",
            resource_type="webhook",
            resource_id=hook_id,
            details={"ip": client_ip, "sig_valid": True, "rate_limited": True},
            result="failure",
        )
        raise_api_error(ErrorCode.RATE_LIMIT_REACHED,
                        detail="webhook rate limit exceeded, retry later")
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, ValueError):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="body",
                        detail="body must be valid JSON")
    if not isinstance(payload, dict):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="body",
                        detail="body must be a JSON object")
    idem_key = request.headers.get("x-idempotency-key", "")
    if idem_key:
        cache_key = f"{hook_id}:{idem_key}"
        prior_run_id = mgr.idempotent_run_id(cache_key)
        if prior_run_id:
            logger.info("webhook invoke deduped hook=%s key=%s run_id=%s",
                        hook_id, idem_key, prior_run_id)
            return {"run_id": prior_run_id, "status": "deduplicated", "deduped": True}
    goal = str(payload.get("input") or payload.get("goal") or "").strip()
    submit_params = {
        "title": f"webhook:{webhook.name}",
        "description": goal or f"webhook trigger {hook_id}",
        "graph_id": webhook.graph_id,
        "trigger": "webhook",
        "input": goal or json.dumps(payload, ensure_ascii=False)[:2000],
        "status": "pending",
        "idempotency_key": idem_key,
    }
    res = await _rpc("task.submit", submit_params)
    task = res.get("task", {}) if isinstance(res, dict) else {}
    run_id = task.get("task_id", "")
    mgr.record_invoke(webhook)
    if idem_key and run_id:
        mgr.record_idempotency(f"{hook_id}:{idem_key}", run_id)
    latency_ms = int((time.time() - started) * 1000)
    _get_audit_logger().log_action(
        actor_id=f"webhook:{hook_id}",
        action="webhook.invoke",
        resource_type="webhook",
        resource_id=hook_id,
        details={
            "ip": client_ip,
            "sig_valid": True,
            "run_id": run_id,
            "latency_ms": latency_ms,
        },
        result="success",
    )
    logger.info("webhook invoke hook=%s run_id=%s latency_ms=%s", hook_id, run_id, latency_ms)
    if webhook.graph_id and run_id:
        try:
            await _rpc("graph.execute_async", {
                "graph_id": webhook.graph_id,
                "input": submit_params["input"],
                "task_id": run_id,
            })
        except Exception as exc:
            logger.exception("webhook graph.execute_async for %s failed (task queued): %s",
                             run_id, exc)
    return {"run_id": run_id, "status": task.get("status", "pending"),
            "deduped": bool(res.get("deduped")) if isinstance(res, dict) else False}


# ---------------- #501: automation trigger REST ----------------
# Cron trigger definitions are plain JSON data editable over REST; the
# daemon's cron.* RPC handlers own the CronManager (SQLite persistence, so
# schedules survive daemon restarts). Failing executions are recorded by
# CronManager, broadcast as cron.execution_failed, and listed here.


@app.get("/v1/cron", dependencies=[Depends(_require_auth_if_configured)])
async def v1_list_cron_jobs():
    return await _rpc("cron.list", {})


@app.post("/v1/cron", dependencies=[Depends(_require_auth_if_configured)])
async def v1_create_cron_job(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="body",
                        detail="body must be a JSON object")
    graph_id = str(body.get("graph_id", "") or "")
    if not graph_id:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="graph_id")
    res = await _rpc("cron.register", body)
    _get_audit_logger().log_action(
        actor_id="api",
        action="cron.create",
        resource_type="cron_job",
        resource_id=str((res.get("job") or {}).get("id", "")),
        details={"graph_id": graph_id, "name": str(body.get("name", ""))},
        result="success",
    )
    return res


@app.get("/v1/cron/executions", dependencies=[Depends(_require_auth_if_configured)])
async def v1_list_cron_executions(request: Request):
    job_id = request.query_params.get("job_id", "")
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="limit",
                        detail="limit must be an integer")
    return await _rpc("cron.list_executions", {"job_id": job_id, "limit": limit})


@app.get("/v1/cron/{job_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_get_cron_job(job_id: str):
    jobs = (await _rpc("cron.list", {})).get("jobs", [])
    for job in jobs:
        if job.get("id") == job_id:
            return {"job": job}
    raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="job_id",
                    detail=f"cron job not found: {job_id}")


@app.delete("/v1/cron/{job_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_delete_cron_job(job_id: str):
    jobs = (await _rpc("cron.list", {})).get("jobs", [])
    if not any(job.get("id") == job_id for job in jobs):
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="job_id",
                        detail=f"cron job not found: {job_id}")
    res = await _rpc("cron.unregister", {"id": job_id})
    _get_audit_logger().log_action(
        actor_id="api",
        action="cron.delete",
        resource_type="cron_job",
        resource_id=job_id,
        details={},
        result="success",
    )
    return res


@app.post("/v1/cron/{job_id}/trigger", dependencies=[Depends(_require_auth_if_configured)])
async def v1_trigger_cron_job(job_id: str):
    res = await _rpc("cron.trigger", {"id": job_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="job_id",
                        detail=str(res.get("message", "trigger failed")))
    _get_audit_logger().log_action(
        actor_id="api",
        action="cron.trigger",
        resource_type="cron_job",
        resource_id=job_id,
        details={},
        result="success",
    )
    return res


@app.get("/v1/runs/{run_id}")
async def v1_get_run(run_id: str):
    res = await _rpc("task.get", {"task_id": run_id})
    task = res.get("task") if isinstance(res, dict) else None
    if not task:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="run_id", detail=f"run not found: {run_id}")
    status = task.get("status", "pending")
    progress = {
        "events": int((task.get("last_result") or {}).get("events", 0)),
        "artifact_ids": list(task.get("artifact_ids") or []),
        "retry_count": int(task.get("retry_count", 0)),
    }
    return {
        "run_id": run_id,
        "status": status,
        "progress": progress,
        "error": task.get("last_error", ""),
        "agent_id": task.get("agent_id", ""),
        "graph_id": task.get("graph_id", ""),
        "created_at": task.get("created_at", 0),
        "updated_at": task.get("updated_at", 0),
        # #461: effective overrides so callers can verify constraints stuck.
        "effective_overrides": (task.get("last_result") or {}).get("effective_overrides", {}),
    }


@app.get("/v1/runs/{run_id}/result")
async def v1_get_run_result(run_id: str):
    res = await _rpc("task.get", {"task_id": run_id})
    task = res.get("task") if isinstance(res, dict) else None
    if not task:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="run_id", detail=f"run not found: {run_id}")
    last_result = task.get("last_result") or {}
    # steps: best-effort extraction from last_result.events count + tool_errors.
    steps = []
    if isinstance(last_result, dict):
        steps.append({
            "session_id": last_result.get("session_id", ""),
            "events": int(last_result.get("events", 0)),
        })
    return {
        "run_id": run_id,
        "status": task.get("status", "pending"),
        "steps": steps,
        "artifacts": list(task.get("artifact_ids") or []),
        "logs": [],
        "last_result": last_result,
        "last_error": task.get("last_error", ""),
    }


@app.get("/v1/runs/{run_id}/events")
async def v1_get_run_events(run_id: str):
    # #406: run event waterfall — per-node spans + run totals.
    res = await _rpc("task.events", {"task_id": run_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="run_id", detail=res.get("message", ""))
    return res


@app.get("/api/v1/runs/{run_id}/events")
async def api_v1_get_run_events(run_id: str):
    return await v1_get_run_events(run_id)



# #432: run event stream for the fusion-agent CLI — SSE tail of a run's
# structural spans + lifecycle status. Token deltas stay on the graph/agent
# SSE endpoints; this stream is what an operator attaches to after submit.
@app.get("/v1/runs/{run_id}/stream", dependencies=[Depends(_require_auth_qs_or_raise)])
async def v1_stream_run(run_id: str, timeout: float = 1800, interval: float = 0.5):
    import asyncio

    terminal = {"completed", "failed", "canceled"}

    async def event_generator():
        seen_nodes: set[str] = set()
        last_status = ""
        deadline = asyncio.get_event_loop().time() + max(timeout, 1.0)
        while True:
            res = await _rpc("task.get", {"task_id": run_id})
            task = res.get("task") if isinstance(res, dict) else None
            if not task:
                yield f"data: {json.dumps({'type': 'error', 'message': f'run not found: {run_id}'})}\n\n"
                return
            status = str(task.get("status", ""))
            if status != last_status:
                last_status = status
                yield f"data: {json.dumps({'type': 'status', 'run_id': run_id, 'status': status})}\n\n"
            ev = await _rpc("task.events", {"task_id": run_id})
            spans = (ev.get("spans") or {}) if isinstance(ev, dict) else {}
            for node in spans.get("nodes") or []:
                key = str(node.get("node_id", "")) + ":" + str(node.get("type", ""))
                if key in seen_nodes:
                    continue
                seen_nodes.add(key)
                yield f"data: {json.dumps({'type': 'span', 'run_id': run_id, 'span': node}, ensure_ascii=False)}\n\n"
            if status in terminal:
                yield f"data: {json.dumps({'type': 'done', 'run_id': run_id, 'status': status})}\n\n"
                return
            if asyncio.get_event_loop().time() > deadline:
                yield f"data: {json.dumps({'type': 'timeout', 'run_id': run_id, 'status': status})}\n\n"
                return
            await asyncio.sleep(max(interval, 0.1))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(_require_auth_if_configured)])
async def v1_cancel_run(run_id: str):
    res = await _rpc("task.cancel", {"task_id": run_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(
            ErrorCode.INTERNAL_ERROR,
            detail=res.get("message", f"run not cancelable: {run_id}"),
        )
    logger.info("/v1/runs canceled run_id=%s", run_id)
    return {"run_id": run_id, "status": "canceled"}


# #497: event tree — fork a finished/failed run at any recorded event and
# continue from there (submit the next execution with fork_from + the forked
# run id); list branches of a run family; switch the active branch pointer.
@app.post("/v1/runs/{run_id}/fork", dependencies=[Depends(_require_auth_if_configured)])
async def v1_fork_run(run_id: str, request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    res = await _rpc("run.fork", {
        "run_id": run_id,
        "at_event_id": str((body or {}).get("at_event_id", "") or ""),
        "title": str((body or {}).get("title", "") or ""),
    })
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="run_id", detail=res.get("message", ""))
    logger.info("/v1/runs forked run_id=%s -> %s", run_id, res.get("run_id"))
    return res


@app.get("/v1/runs/{run_id}/branches", dependencies=[Depends(_require_auth_if_configured)])
async def v1_list_run_branches(run_id: str):
    res = await _rpc("run.branches", {"run_id": run_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="run_id", detail=res.get("message", ""))
    return res


@app.post(
    "/v1/runs/{run_id}/branches/{branch_id}/activate",
    dependencies=[Depends(_require_auth_if_configured)],
)
async def v1_activate_run_branch(run_id: str, branch_id: str):
    res = await _rpc("run.branch.activate", {"run_id": run_id, "branch_id": branch_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(
            ErrorCode.AGENT_NOT_FOUND, param="branch_id", detail=res.get("message", "")
        )
    logger.info("/v1/runs branch activated run_id=%s branch=%s", run_id, branch_id)
    return res


# #425: session management REST surface (list/messages/resume/fork/rename/inject).
@app.get("/v1/sessions")
async def v1_list_sessions(limit: int = 50, offset: int = 0, tag: str = ""):
    return await _rpc("sessions.list", {"limit": limit, "offset": offset, "tag": tag})


@app.get("/v1/sessions/{session_id}")
async def v1_get_session(session_id: str):
    res = await _rpc("sessions.get", {"session_id": session_id})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="session_id", detail=res.get("message", ""))
    return res


@app.get("/v1/sessions/{session_id}/messages")
async def v1_get_session_messages(session_id: str, limit: int = 200):
    res = await _rpc("sessions.messages", {"session_id": session_id, "limit": limit})
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="session_id", detail=res.get("message", ""))
    return res


@app.post("/v1/sessions/{session_id}/resume", dependencies=[Depends(_require_auth_if_configured)])
async def v1_resume_session(session_id: str, request: Request):
    body = await request.json() if await request.body() else {}
    res = await _rpc(
        "sessions.resume",
        {"session_id": session_id, "input": str(body.get("input", "") or "")},
    )
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="session_id", detail=res.get("message", ""))
    logger.info("/v1/sessions/%s resumed -> %s", session_id, res.get("background_session_id"))
    return res


@app.post("/v1/sessions/{session_id}/fork", dependencies=[Depends(_require_auth_if_configured)])
async def v1_fork_session(session_id: str, request: Request):
    body = await request.json() if await request.body() else {}
    res = await _rpc(
        "sessions.fork",
        {"session_id": session_id, "title": str(body.get("title", "") or "")},
    )
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="session_id", detail=res.get("message", ""))
    logger.info("/v1/sessions/%s forked", session_id)
    return res


@app.patch("/v1/sessions/{session_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_rename_session(session_id: str, request: Request):
    body = await request.json()
    res = await _rpc(
        "sessions.rename",
        {
            "session_id": session_id,
            "title": body.get("title"),
            "tag": body.get("tag"),
            "summary": body.get("summary"),
        },
    )
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="session_id", detail=res.get("message", ""))
    return res


@app.post("/v1/sessions/{session_id}/inject", dependencies=[Depends(_require_auth_if_configured)])
async def v1_inject_session_input(session_id: str, request: Request):
    body = await request.json()
    res = await _rpc(
        "sessions.inject_input",
        {"session_id": session_id, "text": str(body.get("input", body.get("text", "")) or "")},
    )
    if isinstance(res, dict) and res.get("status") == "error":
        raise_api_error(ErrorCode.INTERNAL_ERROR, detail=res.get("message", ""))
    logger.info("/v1/sessions/%s injected pending=%s", session_id, res.get("pending"))
    return res


@app.get("/v1/runs")
async def v1_list_runs(status: str = "", limit: int = 100, agent_id: str = ""):
    res = await _rpc("task.list", {"status": status, "agent_id": agent_id, "limit": limit})
    return {"runs": res.get("tasks", []) if isinstance(res, dict) else [], "total": res.get("total", 0) if isinstance(res, dict) else 0}


# ── Shell ext fields (#407): palette search + notification sources ──
# Contract (fusion-web-shell#24): search returns {items:[{title,route,kind}]},
# route = in-module hash route the shell deep-links via `${entry}#${route}`.
# notifications/runs returns terminal runs; the host dedupes unseen ids.


@app.get("/v1/search")
async def v1_search(q: str = "", limit: int = 20):
    query = (q or "").strip().lower()
    if not query:
        return {"items": []}
    items = []
    try:
        for g in _get_store().list_graphs():
            name = str(g.get("name") or g.get("id") or "")
            desc = str(g.get("description") or "")
            if query in name.lower() or query in desc.lower():
                items.append({"title": name, "route": "/canvas", "kind": "workflow"})
            if len(items) >= limit:
                break
    except Exception:
        logger.exception("/v1/search graph scan failed")
    try:
        res = await _rpc("task.list", {"limit": 100})
        for t in (res.get("tasks", []) if isinstance(res, dict) else []):
            title = str(t.get("title") or t.get("task_id") or "")
            tid = str(t.get("task_id") or "")
            if tid and (query in title.lower() or query in tid.lower()):
                items.append({"title": f"运行: {title}", "route": f"/runs/{tid}", "kind": "run"})
            if len(items) >= limit * 2:
                break
    except Exception:
        logger.exception("/v1/search run scan failed")
    items = items[: max(1, limit)]
    logger.info("/v1/search q=%r hits=%d", q, len(items))
    return {"items": items}


@app.get("/v1/notifications/runs")
async def v1_notifications_runs(limit: int = 32):
    terminal = {"completed", "failed", "cancelled", "canceled"}
    items = []
    try:
        res = await _rpc("task.list", {"limit": 100})
        tasks = res.get("tasks", []) if isinstance(res, dict) else []
        tasks.sort(key=lambda t: float(t.get("updated_at") or 0), reverse=True)
        for t in tasks:
            status = str(t.get("status") or "")
            if status not in terminal:
                continue
            tid = str(t.get("task_id") or "")
            if not tid:
                continue
            items.append({
                "id": tid,
                "title": f"运行结束: {t.get('title') or tid} ({status})",
                "route": f"/runs/{tid}",
                "level": "error" if status in {"failed", "cancelled", "canceled"} else "info",
                "ts": float(t.get("updated_at") or 0),
            })
            if len(items) >= max(1, limit):
                break
    except Exception:
        logger.exception("/v1/notifications/runs poll failed")
    logger.info("/v1/notifications/runs items=%d", len(items))
    return {"count": len(items), "items": items}


# ── Knowledge Base endpoints ──


@app.post("/v1/knowledge-bases", dependencies=[Depends(_require_auth_if_configured)])
async def v1_create_kb(request: Request):
    body = await request.json()
    name = body.get("name", "")
    if not name:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="name")
    mgr = _get_kb_manager()
    kb = mgr.create_kb(
        name=name,
        description=body.get("description", ""),
        tags=body.get("tags"),
        scope=body.get("scope", "default"),
    )
    logger.info("Created KB %s: %s", kb.id, kb.name)
    _get_audit_logger().log_action(
        actor_id="api",
        action="kb.create",
        resource_type="knowledge_base",
        resource_id=kb.id,
        result="success",
    )
    return {"knowledge_base": kb.to_dict()}


@app.get("/v1/knowledge-bases")
async def v1_list_kbs(
    page: int = 1, limit: int = 20, keyword: str = "", scope: str = ""
):
    mgr = _get_kb_manager()
    return mgr.list_kbs(page=page, limit=limit, keyword=keyword, scope=scope)


@app.get("/v1/knowledge-bases/rag-status")
async def v1_rag_status():
    mgr = _get_kb_manager()
    available = await mgr.is_rag_available()
    status = await mgr.rag_status()
    return {"rag_available": available, **status}


@app.get("/v1/knowledge-bases/{kb_id}")
async def v1_get_kb(kb_id: str):
    mgr = _get_kb_manager()
    kb = mgr.get_kb(kb_id)
    if kb is None:
        raise_api_error(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, param="kb_id")
    return {"knowledge_base": kb.to_dict()}


@app.patch("/v1/knowledge-bases/{kb_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_update_kb(kb_id: str, request: Request):
    body = await request.json()
    mgr = _get_kb_manager()
    kb = mgr.update_kb(kb_id, body)
    if kb is None:
        raise_api_error(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, param="kb_id")
    logger.info("Updated KB %s", kb_id)
    return {"knowledge_base": kb.to_dict()}


@app.delete("/v1/knowledge-bases/{kb_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_delete_kb(kb_id: str):
    mgr = _get_kb_manager()
    deleted = mgr.delete_kb(kb_id)
    if not deleted:
        raise_api_error(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, param="kb_id")
    _get_audit_logger().log_action(
        actor_id="api",
        action="kb.delete",
        resource_type="knowledge_base",
        resource_id=kb_id,
        result="success",
    )
    return {"deleted": True}


@app.post("/v1/knowledge-bases/{kb_id}/files", dependencies=[Depends(_require_auth_if_configured)])
async def v1_upload_kb_file(kb_id: str, request: Request):
    mgr = _get_kb_manager()
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if not upload:
            raise_api_error(ErrorCode.PARAM_REQUIRED, param="file")
        import tempfile

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f"_{upload.filename}"
        ) as tmp:
            content = await upload.read()
            tmp.write(content)
            tmp_path = tmp.name
        try:
            info = mgr.add_file(kb_id, tmp_path, content_type=upload.content_type or "")
            logger.info("Uploaded file to KB %s: %s", kb_id, info.filename)
            _get_audit_logger().log_action(
                actor_id="api",
                action="kb.upload_file",
                resource_type="knowledge_base",
                resource_id=kb_id,
                details={"filename": info.filename, "size": info.size},
                result="success",
            )
            return {"file": info.to_dict()}
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        body = await request.json()
        file_path = body.get("file_path", "")
        if not file_path:
            raise_api_error(ErrorCode.PARAM_REQUIRED, param="file_path")
        info = mgr.add_file(kb_id, file_path, content_type=body.get("content_type", ""))
        return {"file": info.to_dict()}


@app.get("/v1/knowledge-bases/{kb_id}/files")
async def v1_list_kb_files(kb_id: str):
    mgr = _get_kb_manager()
    files = mgr.list_files(kb_id)
    return {"files": [f.to_dict() for f in files]}


@app.delete("/v1/knowledge-bases/{kb_id}/files/{file_id}", dependencies=[Depends(_require_auth_if_configured)])
async def v1_delete_kb_file(kb_id: str, file_id: str):
    mgr = _get_kb_manager()
    deleted = mgr.delete_file(kb_id, file_id)
    if not deleted:
        raise_api_error(ErrorCode.RESOURCE_NOT_FOUND, param="file_id")
    return {"deleted": True}


@app.post("/v1/agents/{agent_id}/bind-kb", dependencies=[Depends(_require_auth_if_configured)])
async def v1_bind_agent_kb(agent_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    kb_id = body.get("kb_id", "")
    if not kb_id:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="kb_id")
    mgr = _get_kb_manager()
    ok = mgr.bind_agent(kb_id, agent_id)
    if not ok:
        raise_api_error(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, param="kb_id")
    logger.info("Bound agent %s to KB %s", agent_id, kb_id)
    return {"bound": True, "agent_id": agent_id, "kb_id": kb_id}


@app.post("/v1/agents/{agent_id}/unbind-kb", dependencies=[Depends(_require_auth_if_configured)])
async def v1_unbind_agent_kb(agent_id: str, request: Request):
    body = await request.json()
    kb_id = body.get("kb_id", "")
    if not kb_id:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="kb_id")
    mgr = _get_kb_manager()
    ok = mgr.unbind_agent(kb_id, agent_id)
    return {"unbound": ok, "agent_id": agent_id, "kb_id": kb_id}


# ── Fusion-RAG integration routes ─
# Importers: REST clients (curl, GUI)
# Affected API: POST /v1/knowledge-bases/{kb_id}/search|ask|scan, GET /v1/knowledge-bases/rag-status
# Data schemas: search {query,top_k,threshold,hybrid,hybrid_alpha,hybrid_method,rerank,
#   folder_prefix,filter,rewrite_mode}; ask {question,model,max_tokens,temperature,hybrid,
#   rerank,folder_prefix}; scan {path,recursive,file_patterns}
# User instruction: "fusion-rag 已经完成issue和pr，可以开展相关的工作落地"


@app.post("/v1/knowledge-bases/{kb_id}/search", dependencies=[Depends(_require_auth_if_configured)])
async def v1_search_kb(kb_id: str, request: Request):
    body = await request.json()
    query = body.get("query", "")
    if not query:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="query")
    mgr = _get_kb_manager()
    search_kwargs = {}
    for key in (
        "top_k",
        "threshold",
        "hybrid",
        "hybrid_alpha",
        "hybrid_method",
        "rerank",
        "folder_prefix",
        "rewrite_mode",
    ):
        if key in body:
            search_kwargs[key] = body[key]
    if "filter" in body:
        search_kwargs["filter"] = body["filter"]
    result = await mgr.search(kb_id=kb_id, query=query, **search_kwargs)
    logger.info(
        "KB search kb_id=%s query=%s count=%d",
        kb_id,
        query[:50],
        result.get("count", 0),
    )
    return result


@app.post("/v1/knowledge-bases/{kb_id}/ask", dependencies=[Depends(_require_auth_if_configured)])
async def v1_ask_kb(kb_id: str, request: Request):
    body = await request.json()
    question = body.get("question", "")
    if not question:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="question")
    mgr = _get_kb_manager()
    ask_kwargs = {}
    for key in (
        "model",
        "max_tokens",
        "temperature",
        "hybrid",
        "rerank",
        "folder_prefix",
    ):
        if key in body:
            ask_kwargs[key] = body[key]
    result = await mgr.ask(kb_id=kb_id, question=question, **ask_kwargs)
    logger.info("KB ask kb_id=%s question=%s", kb_id, question[:50])
    return result


@app.post("/v1/knowledge-bases/{kb_id}/scan", dependencies=[Depends(_require_auth_if_configured)])
async def v1_scan_kb(kb_id: str, request: Request):
    body = await request.json()
    path = body.get("path", "")
    if not path:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="path")
    mgr = _get_kb_manager()
    scan_kwargs = {}
    if "recursive" in body:
        scan_kwargs["recursive"] = body["recursive"]
    if "file_patterns" in body:
        scan_kwargs["file_patterns"] = body["file_patterns"]
    result = await mgr.scan_directory(kb_id=kb_id, path=path, **scan_kwargs)
    logger.info("KB scan kb_id=%s path=%s", kb_id, path)
    return result


# ── API Key endpoints ──


@app.get("/v1/api-keys")
async def v1_list_api_keys(request: Request):
    await _require_auth(request)
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    return {"data": mgr.list_keys()}


@app.post("/v1/api-keys")
async def v1_create_api_key(request: Request):
    await _require_auth(request)
    body = await request.json()
    name = body.get("name", "")
    if not name:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="name")
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    result = mgr.create(
        name=name,
        permissions=body.get("permissions"),
        allowed_agent_ids=body.get("allowed_agent_ids"),
        ip_whitelist=body.get("ip_whitelist"),
        expires_at=body.get("expires_at"),
    )
    _get_audit_logger().log_action(
        actor_id="api",
        action="apikey.create",
        resource_type="api_key",
        resource_id=result["key_id"],
        result="success",
    )
    return result


@app.delete("/v1/api-keys/{key_id}")
async def v1_revoke_api_key(key_id: str, request: Request):
    await _require_auth(request)
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    result = mgr.revoke(key_id)
    _get_audit_logger().log_action(
        actor_id="api",
        action="apikey.revoke",
        resource_type="api_key",
        resource_id=key_id,
        result="success" if result.get("revoked") else "failed",
    )
    return result


@app.post("/v1/api-keys/{key_id}/rotate")
async def v1_rotate_api_key(key_id: str, request: Request):
    await _require_auth(request)
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio" / "apikeys")
    result = mgr.rotate(key_id)
    _get_audit_logger().log_action(
        actor_id="api",
        action="apikey.rotate",
        resource_type="api_key",
        resource_id=key_id,
        result="success" if result.get("key_secret") else "failed",
    )
    return result


# ── Audit endpoints ──


@app.get("/v1/audit-logs")
async def v1_list_audit_logs(
    request: Request,
    page: int = 1,
    limit: int = 20,
    action: str = "",
    resource_type: str = "",
    actor_id: str = "",
):
    await _require_auth(request)
    al = _get_audit_logger()
    return al.query_logs(
        actor_id=actor_id or None,
        action=action or None,
        resource_type=resource_type or None,
        page=page,
        limit=limit,
    )


# ── Usage / Metrics endpoints ──


@app.get("/v1/usage/summary")
async def v1_usage_summary(
    request: Request,
    start_date: float = 0,
    end_date: float = 0,
    agent_id: str = "",
):
    await _optional_auth(request)
    tele = _get_telemetry()
    metrics = tele.metrics() if tele else {}
    me = _get_metrics_engine()
    sessions = me.query_sessions(status="", limit=1000) if me else []
    if start_date:
        sessions = [s for s in sessions if getattr(s, "started_at", 0) >= start_date]
    if end_date:
        sessions = [s for s in sessions if getattr(s, "started_at", 0) <= end_date]
    total_input = sum(getattr(s, "input_tokens", 0) for s in sessions)
    total_output = sum(getattr(s, "output_tokens", 0) for s in sessions)
    return {
        "total_sessions": len(sessions),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "telemetry": metrics,
    }


@app.get("/v1/agents/{agent_id}/logs")
async def v1_get_agent_logs(agent_id: str, page: int = 1, limit: int = 20):
    tracker = _get_status_tracker()
    history = tracker.get_history(agent_id, limit=limit * page)
    items = [h.to_dict() if hasattr(h, "to_dict") else h for h in history]
    return _paginate(items, page, limit)


# ── Connector endpoints ──


@app.get("/v1/connectors")
async def v1_list_connectors(request: Request):
    from agent_runtime.connectors import ConnectorManager

    mgr = ConnectorManager(Path.home() / ".fusion-agent-studio")
    return {"data": mgr.list_connectors()}


@app.post("/v1/connectors")
async def v1_create_connector(request: Request):
    await _require_auth(request)
    body = await request.json()
    from agent_runtime.connectors import ConnectorManager

    mgr = ConnectorManager(Path.home() / ".fusion-agent-studio")
    result = mgr.add(
        name=body.get("name", ""),
        conn_type=body.get("type", "api_key"),
        auth_config=body.get("auth_config", {}),
    )
    _get_audit_logger().log_action(
        actor_id="api",
        action="connector.create",
        resource_type="connector",
        resource_id=result.get("id", ""),
        result="success",
    )
    return result


@app.delete("/v1/connectors/{connector_id}")
async def v1_delete_connector(connector_id: str, request: Request):
    await _require_auth(request)
    from agent_runtime.connectors import ConnectorManager

    mgr = ConnectorManager(Path.home() / ".fusion-agent-studio")
    result = mgr.delete(connector_id)
    return result


# ── Legacy endpoints (no /v1 prefix, for backward compat) ──


@app.post("/graphs", response_model=GraphResponse, dependencies=[Depends(_require_auth_if_configured)])
async def create_graph(req: GraphCreateRequest):
    return await v1_create_graph(req)


@app.get("/graphs", response_model=list[GraphResponse])
async def list_graphs():
    graphs = _get_store().list_graphs()
    results = []
    for g in graphs:
        full = _get_store().load_graph(g["id"])
        if full:
            results.append(
                GraphResponse(
                    graph_id=full.id,
                    name=full.name,
                    description=full.description,
                    graph_data=full.to_dict(),
                )
            )
    return results


@app.get("/graphs/{graph_id}", response_model=GraphResponse)
async def get_graph(graph_id: str):
    return await v1_get_graph(graph_id)


@app.delete("/graphs/{graph_id}", dependencies=[Depends(_require_auth_if_configured)])
async def delete_graph(graph_id: str):
    return await v1_delete_graph(graph_id)


@app.post("/graphs/{graph_id}/execute", response_model=ExecutionResponse, dependencies=[Depends(_require_auth_if_configured)])
async def execute_graph(graph_id: str, req: GraphExecuteRequest):
    return await v1_execute_graph(graph_id, req)


@app.websocket("/ws/execute/{graph_id}")
async def ws_execute(websocket: WebSocket, graph_id: str):
    # 审计 P0-2: WS 握手前鉴权 (accept 后无法干净抛 HTTP 401).
    # 浏览器不能设自定义 header, 故读 query-string ?api_key=.
    ok, reason = _ws_auth_ok(websocket.query_params.get("api_key", ""))
    if not ok:
        await websocket.accept()
        await websocket.close(code=4401, reason=reason)
        logger.warning("WS execute rejected for graph %s: %s", graph_id, reason)
        return
    await websocket.accept()
    logger.info("WebSocket connected for graph %s", graph_id)
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        await websocket.send_json({"type": "error", "message": "Graph not found"})
        await websocket.close()
        return
    rt = _get_runtime()
    # 审计 P2-5/P2-6/P0-3: WS 直调 rt 绕过 daemon 注册/节流/预算.
    # 经 daemon 统一注册活跃执行 + 并发节流 + 透传 token_budget.
    daemon = _daemon
    _unreg = None
    _sem_ctx = None
    if daemon is not None:
        _, _unreg = daemon.register_execution("ws", graph_id)
        sem = daemon.graph_semaphore
        if sem is not None:
            _sem_ctx = sem
    try:
        input_text = ""
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
        if isinstance(init_msg, dict):
            input_text = init_msg.get("input", "")
        budget = daemon.token_budget_ref if daemon is not None else None

        async def _ws_stream():
            async for event in rt.execute_graph_stream(graph, input_text, token_budget=budget):
                ev_dict = (
                    event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                )
                await websocket.send_json(ev_dict)

        if _sem_ctx is not None:
            async with _sem_ctx:
                logger.info("WS execute %s acquired concurrency slot", graph_id)
                await _ws_stream()
        else:
            await _ws_stream()
        await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for graph %s", graph_id)
    except asyncio.TimeoutError:
        logger.warning("WebSocket init timeout for graph %s", graph_id)
        await websocket.send_json({"type": "error", "message": "Init timeout"})
    except Exception as e:
        logger.exception("WebSocket execution error: %s", graph_id)
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if _unreg is not None:
            _unreg()
        try:
            await websocket.close()
        except Exception:
            pass


@app.get(
    "/v1/graphs/{graph_id}/execute/stream",
    dependencies=[Depends(_require_auth_qs_or_raise)],
)
async def v1_execute_graph_stream(
    graph_id: str,
    input: str = "",
    thinking: bool = True,
    from_node: str = "",
    stop_after: str = "",
    only_node: str = "",
):
    """Server-Sent Events stream — pushes per-token TOKEN events as they arrive.

    Real streaming: connects execute_graph_stream (stream=True) so LLM nodes
    emit per-token TOKEN events. Each event is sent as an SSE ``data:`` line.
    Client connects with EventSource; no buffering at the boundary.
    """
    from agent_runtime.graph import AgentGraph, NodeConfig

    graph = _get_store().load_graph(graph_id)
    if graph is None:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="graph_id")
    # #386: downstream rerun executes the node and its reachable descendants.
    if from_node:
        sub = graph.downstream_subgraph(from_node)
        if sub is None:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="from_node")
        graph = sub
        logger.info("SSE downstream rerun graph=%s from=%s", graph_id, from_node)
    # #398 (audit-1020 6a): single-node test run — execute exactly one node
    # (Langflow "run component"). Implemented as a start-wired subgraph with
    # stop_after on the node so the stream ends after its first result.
    if only_node:
        if only_node not in graph.nodes:
            raise_api_error(ErrorCode.PARAM_FORMAT_ERROR, param="only_node")
        single = AgentGraph(id=graph.id, name=graph.name)
        single.add_node("start", NodeConfig(type="start"))
        single.add_node(only_node, graph.nodes[only_node])
        single.add_edge("start", only_node)
        single.start_node_id = "start"
        graph = single
        stop_after = only_node
        logger.info("SSE single-node run graph=%s node=%s", graph_id, only_node)
    rt = _get_runtime()
    # 审计 P2-5/P2-6/P0-3: SSE 直调 rt 绕过 daemon 注册/节流/预算, 与 WS 同补.
    daemon = _daemon
    budget = daemon.token_budget_ref if daemon is not None else None
    sem = daemon.graph_semaphore if daemon is not None else None

    async def event_generator():
        logger.info("SSE stream start for graph %s", graph_id)
        _unreg = None
        if daemon is not None:
            _, _unreg = daemon.register_execution("sse", graph_id)
        try:
            async def _sse_stream():
                agen = rt.execute_graph_stream(
                    graph, input, token_budget=budget, show_thinking=thinking
                )
                stop_types = {"result", "tool_result", "end", "error", "verify"}
                try:
                    async for event in agen:
                        ev_dict = (
                            event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                        )
                        yield f"data: {json.dumps(ev_dict, ensure_ascii=False)}\n\n"
                        if (
                            stop_after
                            and ev_dict.get("node_id") == stop_after
                            and ev_dict.get("type") in stop_types
                        ):
                            logger.info(
                                "SSE breakpoint hit graph=%s node=%s", graph_id, stop_after
                            )
                            break
                finally:
                    aclose = getattr(agen, "aclose", None)
                    if aclose is not None:
                        await aclose()

            if sem is not None:
                async with sem:
                    logger.info("SSE execute %s acquired concurrency slot", graph_id)
                    async for chunk in _sse_stream():
                        yield chunk
            else:
                async for chunk in _sse_stream():
                    yield chunk
        except Exception as e:
            logger.exception("SSE stream error for graph %s", graph_id)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if _unreg is not None:
                _unreg()
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        logger.info("SSE stream end for graph %s", graph_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get(
    "/v1/agents/{agent_id}/execute/stream",
    dependencies=[Depends(_require_auth_qs_or_raise)],
)
async def v1_execute_agent_stream(agent_id: str, input: str = "", thinking: bool = True):
    """#330: per-token SSE for agent execution — EventSource GET, auth via
    gateway-injected Bearer or ?api_key= (same posture as graph SSE).
    Reuses the ad-hoc graph built from the agent manifest."""
    from agent_runtime.dispatchers.agent import build_agent_adhoc_graph

    daemon = _daemon_or_503()
    graph, manifest = await build_agent_adhoc_graph(daemon, agent_id, input)
    if graph is None:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="agent_id")
    _get_store().save_graph(graph)
    rt = _get_runtime()
    budget = daemon.token_budget_ref if daemon is not None else None
    sem = daemon.graph_semaphore if daemon is not None else None
    agent_max_iter = manifest.max_iterations if manifest.max_iterations else None

    async def event_generator():
        logger.info("SSE agent stream start for %s", agent_id)
        _unreg = None
        if daemon is not None:
            try:
                _, _unreg = daemon.register_execution("sse_agent", agent_id)
            except Exception:
                _unreg = None
        try:

            async def _sse_stream():
                async for event in rt.execute_graph_stream(
                    graph,
                    input,
                    max_iterations=agent_max_iter,
                    token_budget=budget,
                    show_thinking=thinking,
                ):
                    ev_dict = (
                        event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                    )
                    yield f"data: {json.dumps(ev_dict, ensure_ascii=False)}\n\n"

            if sem is not None:
                async with sem:
                    logger.info("SSE agent execute %s acquired concurrency slot", agent_id)
                    async for chunk in _sse_stream():
                        yield chunk
            else:
                async for chunk in _sse_stream():
                    yield chunk
        except Exception as e:
            logger.exception("SSE agent stream error for %s", agent_id)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            if _unreg is not None:
                _unreg()
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        logger.info("SSE agent stream end for %s", agent_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/agents/published")
async def list_published_agents():
    return await v1_list_published_agents()


@app.get("/agents/{agent_id}/definition")
async def get_agent_definition(agent_id: str):
    return await v1_get_agent_definition(agent_id)


@app.get("/agents/{agent_id}/status")
async def get_agent_status(agent_id: str):
    return await v1_get_agent_status(agent_id)


@app.get("/agents/{agent_id}/history")
async def get_agent_history(agent_id: str, limit: int = 20):
    tracker = _get_status_tracker()
    history = tracker.get_history(agent_id, limit=limit)
    return {
        "agent_id": agent_id,
        "history": [h.to_dict() if hasattr(h, "to_dict") else h for h in history],
    }


@app.get("/agents/{agent_id}/artifacts")
async def list_agent_artifacts(agent_id: str):
    return await v1_list_agent_artifacts(agent_id)


@app.get("/agents/{agent_id}/preview")
async def get_agent_preview(agent_id: str):
    return await v1_get_agent_preview(agent_id)


@app.post("/agents/{agent_id}/test", dependencies=[Depends(_require_auth_if_configured)])
async def test_agent_with_project(
    agent_id: str, project_id: str = "", kb_id: str = "", message: str = "hello"
):
    return await v1_test_agent(agent_id, project_id, kb_id, message)


# ── /api/v1/* aliases ──
# Issue #100: fusion-projects (project_service) requests /api/v1/agents.
# Mirror the agent read endpoints under the /api/v1 prefix so external
# clients using the /api convention resolve without code changes upstream.
@app.get("/api/v1/agents")
async def api_v1_list_agents(
    page: int = 1, limit: int = 20, status: str = "", keyword: str = ""
):
    return await v1_list_agents(page=page, limit=limit, status=status, keyword=keyword)


@app.get("/api/v1/agents/published")
async def api_v1_list_published_agents():
    return await v1_list_published_agents()


@app.get("/api/v1/agents/{agent_id}/definition")
async def api_v1_get_agent_definition(agent_id: str):
    return await v1_get_agent_definition(agent_id)


@app.get("/api/v1/agents/{agent_id}/status")
async def api_v1_get_agent_status(agent_id: str):
    return await v1_get_agent_status(agent_id)


@app.get("/api/v1/agents/{agent_id}/history")
async def api_v1_get_agent_history(agent_id: str, limit: int = 20, page: int = 1):
    return await v1_get_agent_history(agent_id, limit=limit, page=page)


@app.get("/api/v1/agents/{agent_id}/artifacts")
async def api_v1_list_agent_artifacts(agent_id: str):
    return await v1_list_agent_artifacts(agent_id)


@app.get("/api/v1/agents/{agent_id}/preview")
async def api_v1_get_agent_preview(agent_id: str):
    return await v1_get_agent_preview(agent_id)


@app.post("/api/v1/agents/{agent_id}/test", dependencies=[Depends(_require_auth_if_configured)])
async def api_v1_test_agent(
    agent_id: str, project_id: str = "", kb_id: str = "", message: str = "hello"
):
    return await v1_test_agent(agent_id, project_id, kb_id, message)


# ── #407: shell ext-field endpoints under /api/v1 too (manifest declares the
# /api convention; searchProviders + notifications point at these) ──


@app.get("/api/v1/search")
async def api_v1_search(q: str = "", limit: int = 20):
    return await v1_search(q=q, limit=limit)


@app.get("/api/v1/notifications/runs")
async def api_v1_notifications_runs(limit: int = 32):
    return await v1_notifications_runs(limit=limit)


# ── #274: Chat↔FSB integration HTTP endpoints (env-gated FUSION_FSB_ENABLED) ──


class FSBBindRequest(BaseModel):
    workspace_id: str
    agent_id: str
    session_id: str = ""


class FSBUnbindRequest(BaseModel):
    workspace_id: str


class FSBChatRunRequest(BaseModel):
    workspace_id: str
    query: str
    input_data: dict = {}


class FSBNotifyRequest(BaseModel):
    workspaceId: str
    agentId: str = ""
    event: str = ""
    run: dict = {}


@app.post("/api/v1/chat/agent/bind", dependencies=[Depends(_require_auth_if_configured)])
async def api_v1_chat_agent_bind(req: FSBBindRequest):
    # #274: consumed by FSB to register workspace↔agent mapping on chat side.
    # Also calls FSB bind upstream when FSB enabled (round-trip both sides).
    from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled
    from agent_runtime.workspace_binder import get_workspace_binder

    binder = get_workspace_binder()
    binder.bind(req.workspace_id, req.agent_id, req.session_id or None)
    if req.session_id and _daemon is not None:
        engine = _daemon._get_chat_engine()
        session = engine.get_session(req.session_id)
        if session is not None:
            session.metadata["fsb_workspace_id"] = req.workspace_id
            session.metadata["fsb_agent_id"] = req.agent_id
    upstream = None
    if is_fsb_enabled():
        upstream = get_fsb_client().bind(req.workspace_id, req.agent_id)
    logger.info("chat.agent.bind ws=%s agent=%s fsb=%s", req.workspace_id, req.agent_id, is_fsb_enabled())
    return {"status": "ok", "bound": True, "upstream": upstream}


@app.post("/api/v1/chat/agent/unbind", dependencies=[Depends(_require_auth_if_configured)])
async def api_v1_chat_agent_unbind(req: FSBUnbindRequest):
    from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled
    from agent_runtime.workspace_binder import get_workspace_binder

    binder = get_workspace_binder()
    binder.unbind(req.workspace_id)
    upstream = None
    if is_fsb_enabled():
        upstream = get_fsb_client().unbind(req.workspace_id)
    logger.info("chat.agent.unbind ws=%s", req.workspace_id)
    return {"status": "ok", "bound": False, "upstream": upstream}


@app.post("/api/v1/chat/run", dependencies=[Depends(_require_auth_if_configured)])
async def api_v1_chat_run(req: FSBChatRunRequest):
    # #274: NL query -> FSB intent match -> workflow run. Fail-soft.
    from agent_runtime.fsb_client import get_fsb_client, is_fsb_enabled

    if not is_fsb_enabled():
        return {"status": "disabled", "matched": False}
    result = get_fsb_client().chat_run(req.workspace_id, req.query, req.input_data)
    if result is None:
        return {"status": "error", "matched": False, "message": "fsb unreachable"}
    return {"status": "ok", **result}


@app.post("/api/v1/chat/notify")
async def api_v1_chat_notify(req: FSBNotifyRequest):
    # #274: inbound — FSB run-completion hook. Pushes a "workflow completed"
    # assistant message into the bound chat session. Fail-soft (logged, 200).
    from agent_runtime.workspace_binder import get_workspace_binder

    binder = get_workspace_binder()
    binding = binder.get(req.workspaceId)
    if binding is None:
        logger.info("chat.notify ws=%s no binding (ignored)", req.workspaceId)
        return {"status": "ok", "delivered": False, "reason": "no binding"}
    session_id = binding.get("session_id")
    if not session_id or _daemon is None:
        logger.info("chat.notify ws=%s no session bound", req.workspaceId)
        return {"status": "ok", "delivered": False, "reason": "no session"}
    engine = _daemon._get_chat_engine()
    session = engine.get_session(session_id)
    if session is None:
        logger.warning("chat.notify ws=%s session=%s not found", req.workspaceId, session_id)
        return {"status": "ok", "delivered": False, "reason": "session not found"}
    # Synthesize a system/assistant message describing the run completion.
    run = req.run or {}
    run_id = run.get("id", "")
    workflow = run.get("workflow", {})
    wf_name = workflow.get("name", "") if isinstance(workflow, dict) else str(workflow)
    status = run.get("status", req.event)
    from agent_runtime.chat_engine import ChatMessage

    notify_text = f"✅ Workflow completed: {wf_name} (run {run_id}, status: {status})"
    session.add_message(ChatMessage(role="assistant", content=notify_text, mode="simple"))
    engine._persist_session(session)
    # Broadcast to live WS listeners on this session.
    if hasattr(_daemon, "_broadcast_event"):
        await _daemon._broadcast_event(
            "chat_notify",
            {"session_id": session_id, "workspace_id": req.workspaceId, "run": run, "message": notify_text},
        )
    logger.info("chat.notify delivered ws=%s session=%s run=%s", req.workspaceId, session_id, run_id)
    return {"status": "ok", "delivered": True, "session_id": session_id}


# ── #437 (E1): Agents console — native agents-platform REST surface +
# capability door + approval workbench, then the verbatim-port static
# console (web/console/) at /console. Registered BEFORE the web dist mount
# so the API routes win.
from agent_runtime.api_agents_platform import register_agents_platform

register_agents_platform(
    app,
    lambda method, params: _rpc(method, params),
    lambda: _daemon,
    _require_auth_if_configured,
)

_CONSOLE_DIR = Path(__file__).resolve().parent.parent / "web" / "console"
if _CONSOLE_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/console", StaticFiles(directory=str(_CONSOLE_DIR), html=True),
              name="agents-console")
    logger.info("agents console served at /console from %s", _CONSOLE_DIR)


# ── #330: allowlisted RPC bridge for the web module ──
# Single POST /v1/rpc endpoint dispatching GUI-facing RPC methods through the
# daemon handler registry (same resolution as /v1/runs). Deny-by-default
# allowlist lives in agent_runtime/rpc_bridge.py. Registered BEFORE the web
# dist mount so the API route wins.
from agent_runtime.rpc_bridge import register_rpc_bridge

register_rpc_bridge(
    app,
    _rpc,
    _require_auth_if_configured,
    audit=lambda **kw: _get_audit_logger().log_action(**kw),
)


# ── #327: web/ static UI bundle ──
# The built web/ package (bun run build inside web/) is served from the repo
# root at "/" so the gateway /shell/fusion-agent-studio/ proxy (which strips
# the module prefix) lands on the SPA. Mount LAST: every @app route above
# wins; only unmatched paths fall through to static files.
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _WEB_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="webui")
    logger.info("web UI dist served at / from %s", _WEB_DIST)
else:
    logger.info("web UI dist not found at %s — build web/ (bun install && bun run build)", _WEB_DIST)


def run_server(host: str = "127.0.0.1", port: int = 11442):
    import uvicorn

    # #490: standalone REST entrypoint shows the product name, not bare Python.
    from agent_runtime.proctitle import set_process_title

    set_process_title(f"fusion-agent-studio api :{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
