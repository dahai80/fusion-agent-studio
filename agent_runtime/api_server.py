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
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent_runtime.errors import ErrorCode, raise_api_error
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("Fusion Agent Studio API starting")
    yield
    for sid, task in _active_sessions.items():
        if not task.done():
            task.cancel()
    logger.info("Fusion Agent Studio API shutting down")


app = FastAPI(title="Fusion Agent Studio API", version="0.3.22", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        _store = AgentStore()
    return _store


def _get_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        from tools import create_default_registry

        _runtime = AgentRuntime(tool_registry=create_default_registry())
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


async def _optional_auth(request: Request) -> dict | None:
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        return None
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio")
    client_ip = request.client.host if request.client else ""
    result = mgr.validate(api_key, client_ip=client_ip)
    if result.get("valid"):
        return result
    return None


async def _require_auth(request: Request) -> dict:
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise_api_error(ErrorCode.API_KEY_MISSING)
    result = await _optional_auth(request)
    if result is None:
        raise_api_error(ErrorCode.API_KEY_INVALID)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.3.0", "persistence": "sqlite"}


# ── /v1 versioned routes ──


@app.get("/v1/health")
async def v1_health():
    return {"status": "ok", "version": "0.3.0", "persistence": "sqlite"}


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


@app.post("/v1/graphs", response_model=GraphResponse)
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


@app.delete("/v1/graphs/{graph_id}")
async def v1_delete_graph(graph_id: str):
    deleted = _get_store().delete_graph(graph_id)
    if not deleted:
        raise_api_error(ErrorCode.AGENT_NOT_FOUND, param="graph_id")
    logger.info("Deleted graph %s", graph_id)
    return {"deleted": True}


@app.post("/v1/graphs/{graph_id}/execute", response_model=ExecutionResponse)
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


@app.post("/v1/agents/{agent_id}/test")
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


@app.post("/v1/agents/{agent_id}/duplicate")
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


@app.post("/v1/agents/{agent_id}/snapshot")
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


@app.post("/v1/agents/{agent_id}/versions/{version_id}/restore")
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


# ── Knowledge Base endpoints ──


@app.post("/v1/knowledge-bases")
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


@app.patch("/v1/knowledge-bases/{kb_id}")
async def v1_update_kb(kb_id: str, request: Request):
    body = await request.json()
    mgr = _get_kb_manager()
    kb = mgr.update_kb(kb_id, body)
    if kb is None:
        raise_api_error(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND, param="kb_id")
    logger.info("Updated KB %s", kb_id)
    return {"knowledge_base": kb.to_dict()}


@app.delete("/v1/knowledge-bases/{kb_id}")
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


@app.post("/v1/knowledge-bases/{kb_id}/files")
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


@app.delete("/v1/knowledge-bases/{kb_id}/files/{file_id}")
async def v1_delete_kb_file(kb_id: str, file_id: str):
    mgr = _get_kb_manager()
    deleted = mgr.delete_file(kb_id, file_id)
    if not deleted:
        raise_api_error(ErrorCode.RESOURCE_NOT_FOUND, param="file_id")
    return {"deleted": True}


@app.post("/v1/agents/{agent_id}/bind-kb")
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


@app.post("/v1/agents/{agent_id}/unbind-kb")
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


@app.post("/v1/knowledge-bases/{kb_id}/search")
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


@app.post("/v1/knowledge-bases/{kb_id}/ask")
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


@app.post("/v1/knowledge-bases/{kb_id}/scan")
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

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio")
    return {"data": mgr.list_keys()}


@app.post("/v1/api-keys")
async def v1_create_api_key(request: Request):
    await _require_auth(request)
    body = await request.json()
    name = body.get("name", "")
    if not name:
        raise_api_error(ErrorCode.PARAM_REQUIRED, param="name")
    from agent_runtime.apikey_manager import ApiKeyManager

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio")
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

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio")
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

    mgr = ApiKeyManager(Path.home() / ".fusion-agent-studio")
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


@app.post("/graphs", response_model=GraphResponse)
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


@app.delete("/graphs/{graph_id}")
async def delete_graph(graph_id: str):
    return await v1_delete_graph(graph_id)


@app.post("/graphs/{graph_id}/execute", response_model=ExecutionResponse)
async def execute_graph(graph_id: str, req: GraphExecuteRequest):
    return await v1_execute_graph(graph_id, req)


@app.websocket("/ws/execute/{graph_id}")
async def ws_execute(websocket: WebSocket, graph_id: str):
    await websocket.accept()
    logger.info("WebSocket connected for graph %s", graph_id)
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        await websocket.send_json({"type": "error", "message": "Graph not found"})
        await websocket.close()
        return
    rt = _get_runtime()
    try:
        input_text = ""
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
        if isinstance(init_msg, dict):
            input_text = init_msg.get("input", "")
        async for event in rt.execute_graph(graph, input_text):
            ev_dict = (
                event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
            )
            await websocket.send_json(ev_dict)
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
        try:
            await websocket.close()
        except Exception:
            pass


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


@app.post("/agents/{agent_id}/test")
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


@app.post("/api/v1/agents/{agent_id}/test")
async def api_v1_test_agent(
    agent_id: str, project_id: str = "", kb_id: str = "", message: str = "hello"
):
    return await v1_test_agent(agent_id, project_id, kb_id, message)


def run_server(host: str = "127.0.0.1", port: int = 11455):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
