"""Issue #543 — 11-03 acceptance residuals: rag ask contract, graph 422,
session error contract, key bootstrap.

Pins:
  1. fusion_rag_client paths stay /kb/bases/{id}/{search,ask} (contract test
     — the /kb prefix is nested in fusion-rag routes.py; without this pin a
     prefix drift 404s silently through knowledge_base's rag_available=False).
  2. REST graph creation rejects structurally-invalid graphs with 422
     ("Graph has no start node") instead of 500 at run time.
  3. /v1/sessions/{id}/messages on a run session id returns
     SESSION_TYPE_MISMATCH (expected=chat_session, actual=run); unknown id
     returns SESSION_NOT_FOUND — not the misleading AGENT_NOT_FOUND.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from server.fusion_rag_client import FusionRAGClient


def _rag_with_capture():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/kb/status":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/kb/bases":
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        if request.url.path.endswith("/ask"):
            return httpx.Response(200, json={"answer": "a", "sources": [], "confidence": 0.9})
        return httpx.Response(404, json={})

    c = FusionRAGClient(base_url="http://test")
    c._client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
    return c, seen


@pytest.mark.asyncio
async def test_rag_contract_paths():
    c, seen = _rag_with_capture()
    try:
        assert await c.health() is True
        await c.status()
        await c.list_bases()
        await c.search("kb1", "q")
        await c.ask("kb1", "q")
    finally:
        await c.close()
    assert ("GET", "/health") in seen
    assert ("GET", "/kb/status") in seen
    assert ("GET", "/kb/bases") in seen
    assert ("POST", "/kb/bases/kb1/search") in seen
    assert ("POST", "/kb/bases/kb1/ask") in seen


@pytest.mark.asyncio
async def test_rag_ask_contract_via_knowledge_base(tmp_path):
    # end-to-end through the KnowledgeBaseManager delegation: rag available
    # + ask returns the grounded answer with rag_available=True.
    from agent_runtime.knowledge_base import KnowledgeBaseManager

    c, _ = _rag_with_capture()
    kb = KnowledgeBaseManager(base_path=str(tmp_path / "kb"))
    kb._rag_client = c
    result = await kb.ask("kb1", "what?")
    assert result["rag_available"] is True
    assert result["answer"] == "a"
    assert result["confidence"] == 0.9
    await c.close()


class _StubDaemon:
    def __init__(self, tmp_path):
        self.agents_dir = tmp_path / "agents"
        from agent_runtime.persistence import AgentStore

        self.store = AgentStore(db_path=str(tmp_path / "store.db"))


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agent_runtime.api_server as api_server

    async def _rpc_stub(method, params):
        if method == "task.get":
            if params.get("task_id") == "run_123":
                return {"task": {"task_id": "run_123", "status": "running"}}
            return {"status": "error", "message": f"Task not found: {params.get('task_id')}"}
        if method in ("sessions.get", "sessions.messages", "sessions.resume", "sessions.fork", "sessions.rename"):
            return {"status": "error", "message": f"session not found: {params.get('session_id')}"}
        raise AssertionError(f"unexpected rpc {method}")

    api_server._store = None
    monkeypatch.setattr(api_server, "_daemon", _StubDaemon(tmp_path), raising=False)
    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    # local-trusted mode so POST /v1/graphs skips the api-key dependency
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(api_server.app) as c:
        yield c
    api_server._store = None


def test_graph_create_422_missing_start(client):
    resp = client.post("/v1/graphs", json={"graph_data": {"nodes": [{"id": "llm1", "type": "llm"}]}})
    assert resp.status_code == 422
    err = resp.json()["detail"]["error"]
    assert err["code"] == "PARAM_FORMAT_ERROR"
    assert "Graph has no start node" in err["message"]


def test_graph_create_422_dangling_edge(client):
    resp = client.post(
        "/v1/graphs",
        json={
            "graph_data": {
                "start_node_id": "s",
                "nodes": [{"id": "s", "type": "start"}],
                "edges": [{"source_id": "s", "target_id": "ghost"}],
            }
        },
    )
    assert resp.status_code == 422
    assert "not found in nodes" in resp.json()["detail"]["error"]["message"]


def test_graph_create_ok_still_saved(client):
    resp = client.post(
        "/v1/graphs",
        json={
            "graph_data": {
                "start_node_id": "s",
                "nodes": [{"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
                "edges": [{"source_id": "s", "target_id": "e"}],
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["graph_id"]


def test_session_messages_unknown_id_session_not_found(client):
    resp = client.get("/v1/sessions/ghost/messages")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "SESSION_NOT_FOUND"


def test_session_messages_run_id_type_mismatch(client):
    resp = client.get("/v1/sessions/run_123/messages")
    assert resp.status_code == 400
    err = resp.json()["detail"]["error"]
    assert err["code"] == "SESSION_TYPE_MISMATCH"
    assert "expected=chat_session" in err["message"]
    assert "actual=run" in err["message"]


def test_session_get_run_id_type_mismatch(client):
    resp = client.get("/v1/sessions/run_123")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "SESSION_TYPE_MISMATCH"
