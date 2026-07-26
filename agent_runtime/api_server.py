"""API server — FastAPI + WebSocket for Fusion Agent Studio.

Provides REST endpoints for graph management, execution, and monitoring.
WebSocket for real-time event streaming during graph execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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


app = FastAPI(title="Fusion Agent Studio API", version="0.1.0", lifespan=lifespan)

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


from agent_runtime.persistence import AgentStore

_store: AgentStore | None = None
_runtime: AgentRuntime | None = None
_active_sessions: dict[str, asyncio.Task] = {}


def _get_store() -> AgentStore:
    global _store
    if _store is None:
        _store = AgentStore()
    return _store


def _get_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime()
    return _runtime


@app.get("/health")
async def health():
    store = _get_store()
    return {"status": "ok", "version": "0.1.0", "persistence": "sqlite"}


@app.post("/graphs", response_model=GraphResponse)
async def create_graph(req: GraphCreateRequest):
    from agent_runtime.graph import AgentGraph
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


@app.get("/graphs", response_model=list[GraphResponse])
async def list_graphs():
    graphs = _get_store().list_graphs()
    results = []
    for g in graphs:
        full = _get_store().load_graph(g["id"])
        if full:
            results.append(GraphResponse(
                graph_id=full.id,
                name=full.name,
                description=full.description,
                graph_data=full.to_dict(),
            ))
    return results


@app.get("/graphs/{graph_id}", response_model=GraphResponse)
async def get_graph(graph_id: str):
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Graph not found")
    return GraphResponse(
        graph_id=graph.id,
        name=graph.name,
        description=graph.description,
        graph_data=graph.to_dict(),
    )


@app.delete("/graphs/{graph_id}")
async def delete_graph(graph_id: str):
    deleted = _get_store().delete_graph(graph_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Graph not found")
    logger.info("Deleted graph %s", graph_id)
    return {"deleted": True}


@app.post("/graphs/{graph_id}/execute", response_model=ExecutionResponse)
async def execute_graph(graph_id: str, req: GraphExecuteRequest):
    graph = _get_store().load_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Graph not found")

    rt = _get_runtime()
    session_id = req.session_id or str(uuid.uuid4())[:8]
    events = []

    try:
        async for event in rt.execute_graph(graph, req.input_text):
            ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
            events.append(ev_dict)
    except Exception as e:
        logger.exception("Graph execution failed: %s", graph_id)
        return ExecutionResponse(session_id=session_id, events=events, status="error")

    logger.info("Graph %s executed: %d events", graph_id, len(events))
    return ExecutionResponse(session_id=session_id, events=events, status="completed")


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
            ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
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


def run_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_server()
