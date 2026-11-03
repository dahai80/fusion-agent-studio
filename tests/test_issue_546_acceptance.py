"""Issue #546 — 11-03 acceptance run residuals (round 2):
  1. POST /v1/knowledge-bases/{kb_id}/ask reported 404 — route exists since
     v0.6.x; pin the OpenAPI contract so a route drift or proxy shadow fails
     here instead of surfacing only in acceptance runs.
  2. POST /v1/graphs 500 — AgentGraph.from_dict raised on malformed payloads
     outside any try; now 422 PARAM_FORMAT_ERROR.
  3. POST /v1/graphs/{id}/execute runtime error on start node — pre-exec
     structural validation now rejects with 422 before the runtime walks it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class _StubKBManager:
    def __init__(self):
        self.calls = []

    async def ask(self, kb_id, question, **kwargs):
        self.calls.append((kb_id, question))
        return {
            "answer": "",
            "kb_id": kb_id,
            "question": question,
            "rag_available": False,
        }


class _StubRuntime:
    def __init__(self):
        self.graphs = []

    async def execute_graph(self, graph, input_text):
        self.graphs.append(graph.id)
        yield {"type": "started", "graph_id": graph.id}
        yield {"type": "completed", "graph_id": graph.id}


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agent_runtime.api_server as api_server

    api_server._store = None
    stub_kb = _StubKBManager()
    stub_rt = _StubRuntime()
    monkeypatch.setattr(api_server, "_kb_manager", stub_kb)
    monkeypatch.setattr(api_server, "_runtime", stub_rt)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(api_server.app) as c:
        c._stub_kb = stub_kb
        c._stub_rt = stub_rt
        yield c
    api_server._store = None


VALID_GRAPH = {
    "start_node_id": "s",
    "nodes": [{"id": "s", "type": "start"}, {"id": "e", "type": "end"}],
    "edges": [{"source_id": "s", "target_id": "e"}],
}


def test_openapi_kb_and_graph_routes_present(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/v1/knowledge-bases/{kb_id}/ask" in paths
    assert "post" in paths["/v1/knowledge-bases/{kb_id}/ask"]
    assert "/v1/knowledge-bases/{kb_id}/search" in paths
    assert "/v1/knowledge-bases/{kb_id}/scan" in paths
    assert "/v1/graphs" in paths
    assert "post" in paths["/v1/graphs"]
    assert "/v1/graphs/{graph_id}/execute" in paths
    assert "post" in paths["/v1/graphs/{graph_id}/execute"]


def test_kb_ask_unknown_kb_degraded_not_404(client):
    resp = client.post(
        "/v1/knowledge-bases/no-such-kb/ask", json={"question": "what?"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rag_available"] is False
    assert body["kb_id"] == "no-such-kb"
    assert client._stub_kb.calls == [("no-such-kb", "what?")]


def test_kb_ask_missing_question_400(client):
    resp = client.post("/v1/knowledge-bases/kb1/ask", json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "PARAM_REQUIRED"


def test_graph_create_422_unparseable_nodes_scalar(client):
    # from_dict coerces a scalar nodes field to an empty graph; the create-time
    # structural check still rejects it (no start node) — 422 either way.
    resp = client.post("/v1/graphs", json={"graph_data": {"nodes": "nope"}})
    assert resp.status_code == 422
    err = resp.json()["detail"]["error"]
    assert err["code"] == "PARAM_FORMAT_ERROR"
    assert "Graph invalid" in err["message"]


def test_graph_create_422_unparseable_node_entry(client):
    resp = client.post("/v1/graphs", json={"graph_data": {"nodes": [42]}})
    assert resp.status_code == 422
    assert "unparseable" in resp.json()["detail"]["error"]["message"]


def test_execute_422_no_start_node(client, tmp_path):
    import agent_runtime.api_server as api_server
    from agent_runtime.graph import AgentGraph

    graph = AgentGraph.from_dict(
        {"nodes": [{"id": "llm1", "type": "llm"}], "edges": []}
    )
    api_server._get_store().save_graph(graph)

    resp = client.post(
        f"/v1/graphs/{graph.id}/execute",
        json={"graph_id": graph.id, "input_text": "hi"},
    )
    assert resp.status_code == 422
    err = resp.json()["detail"]["error"]
    assert err["code"] == "PARAM_FORMAT_ERROR"
    assert "Graph has no start node" in err["message"]
    assert client._stub_rt.graphs == []


def test_execute_422_dangling_edge(client):
    import agent_runtime.api_server as api_server
    from agent_runtime.graph import AgentGraph

    graph = AgentGraph.from_dict(
        {
            "start_node_id": "s",
            "nodes": [{"id": "s", "type": "start"}],
            "edges": [{"source_id": "s", "target_id": "ghost"}],
        }
    )
    api_server._get_store().save_graph(graph)

    resp = client.post(
        f"/v1/graphs/{graph.id}/execute",
        json={"graph_id": graph.id, "input_text": "hi"},
    )
    assert resp.status_code == 422
    assert "not found in nodes" in resp.json()["detail"]["error"]["message"]


def test_execute_valid_graph_runs(client):
    import agent_runtime.api_server as api_server
    from agent_runtime.graph import AgentGraph

    graph = AgentGraph.from_dict(VALID_GRAPH)
    api_server._get_store().save_graph(graph)

    resp = client.post(
        f"/v1/graphs/{graph.id}/execute",
        json={"graph_id": graph.id, "input_text": "hi"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert client._stub_rt.graphs == [graph.id]
