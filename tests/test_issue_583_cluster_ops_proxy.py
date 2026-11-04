"""Issue #583 — web surface gap: cluster ops / observability / autoscaler.

The daemon gains a same-origin proxy (/v1/cluster-ops/*) to the
cluster_server (:11457) so the web UI can run cluster task actions and
observability without a cross-origin hop. Unreachable cluster → 503
(fail visibly, no silent fallback).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

STUB_STATE = {"autoscaler": {"enabled": True, "min_nodes": 1}}


class _StubCluster(BaseHTTPRequestHandler):
    def _send(self, code, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        routes = {
            "/api/v1/cluster/stats": {"nodes_total": 3, "tasks_running": 2},
            "/api/nodes": {"nodes": [{"node_id": "n1", "status": "online"}]},
            "/api/v1/nodes/n1/metrics": {"node_id": "n1", "cpu": 0.4},
            "/api/v1/autoscaler/config": STUB_STATE["autoscaler"],
            "/api/v1/observability/alerts": {"alerts": [{"level": "warn"}]},
            "/api/v1/observability/suggestions": {"suggestions": [], "error": None},
            "/api/tasks": {"total": 1, "tasks": [{"task_id": "t1"}]},
            "/api/routing/summary": {"strategy": "score"},
            "/api/v1/observability/logs/export": b'{"task_id": "t1"}\n',
        }
        if self.path in routes:
            self._send(200, routes[self.path])
        else:
            self._send(404, {"detail": "not found"})

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        STUB_STATE["autoscaler"] = json.loads(self.rfile.read(length) or b"{}")
        self._send(200, STUB_STATE["autoscaler"])

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/tasks/submit":
            self._send(200, {"task_id": "t-new", "status": "running", **body})
        elif self.path == "/api/tasks/t1/cancel":
            self._send(200, {"task_id": "t1", "status": "cancelled"})
        elif self.path == "/api/tasks/t1/degrade":
            self._send(200, {"task_id": "t1", "status": "degraded"})
        elif self.path == "/api/tasks/t1/migrate":
            self._send(
                200,
                {"task_id": "t1", "status": "running",
                 "assigned_nodes": [body.get("target_node_id", "")]},
            )
        elif self.path == "/api/routing/strategy":
            self._send(200, {"strategy": body.get("strategy", "")})
        else:
            self._send(404, {"detail": "not found"})

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_cluster(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _StubCluster)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("FUSION_CLUSTER_OPS_URL", f"http://127.0.0.1:{port}")
    # The dev machine has real API keys, so _auth_configured() is true and
    # mutating routes enforce auth — pin a static env key for the test.
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-cluster-ops-test-583")
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture
def client():
    from agent_runtime import api_server

    return TestClient(api_server.app, raise_server_exceptions=False)


class TestClusterOpsProxy:
    def test_stats_passthrough(self, stub_cluster, client):
        r = client.get("/v1/cluster-ops/stats")
        assert r.status_code == 200
        assert r.json()["nodes_total"] == 3

    def test_nodes_and_metrics(self, stub_cluster, client):
        assert client.get("/v1/cluster-ops/nodes").json()["nodes"][0]["node_id"] == "n1"
        r = client.get("/v1/cluster-ops/nodes/n1/metrics")
        assert r.json()["cpu"] == 0.4

    def test_autoscaler_get_put(self, stub_cluster, client):
        r = client.get("/v1/cluster-ops/autoscaler/config")
        assert r.json()["enabled"] is True
        r = client.put(
            "/v1/cluster-ops/autoscaler/config",
            json={"enabled": False, "min_nodes": 2},
            headers={"x-api-key": "fk-cluster-ops-test-583"},
        )
        assert r.status_code == 200
        assert r.json()["min_nodes"] == 2

    def test_observability_alerts_suggestions(self, stub_cluster, client):
        assert client.get("/v1/cluster-ops/observability/alerts").json()["alerts"]
        assert client.get(
            "/v1/cluster-ops/observability/suggestions"
        ).json()["error"] is None

    def test_log_export_text_passthrough(self, stub_cluster, client):
        r = client.get("/v1/cluster-ops/observability/logs/export")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert '"task_id": "t1"' in r.text

    def test_task_actions(self, stub_cluster, client):
        assert client.get("/v1/cluster-ops/tasks").json()["total"] == 1
        r = client.post("/v1/cluster-ops/tasks", json={"name": "x"}, headers={"x-api-key": "fk-cluster-ops-test-583"})
        assert r.json()["task_id"] == "t-new"
        r = client.post("/v1/cluster-ops/tasks/t1/migrate", json={"target_node_id": "n2"}, headers={"x-api-key": "fk-cluster-ops-test-583"})
        assert r.json()["assigned_nodes"] == ["n2"]
        assert client.post("/v1/cluster-ops/tasks/t1/cancel", json={}, headers={"x-api-key": "fk-cluster-ops-test-583"}).json()["status"] == "cancelled"
        assert client.post("/v1/cluster-ops/tasks/t1/degrade", json={}, headers={"x-api-key": "fk-cluster-ops-test-583"}).json()["status"] == "degraded"

    def test_routing_summary_and_strategy(self, stub_cluster, client):
        assert client.get("/v1/cluster-ops/routing/summary").json()["strategy"] == "score"
        r = client.post("/v1/cluster-ops/routing/strategy", json={"strategy": "round-robin"}, headers={"x-api-key": "fk-cluster-ops-test-583"})
        assert r.json()["strategy"] == "round-robin"

    def test_cluster_unreachable_503(self, monkeypatch, client):
        monkeypatch.setenv("FUSION_CLUSTER_OPS_URL", "http://127.0.0.1:1")
        r = client.get("/v1/cluster-ops/stats")
        assert r.status_code == 503
        assert "unreachable" in r.json()["detail"]

    def test_upstream_error_passthrough(self, stub_cluster, client):
        r = client.get("/v1/cluster-ops/nodes/ghost/metrics")
        assert r.status_code == 404

    def test_default_url_when_unset(self, monkeypatch):
        from agent_runtime.api_cluster_ops import cluster_ops_url

        monkeypatch.delenv("FUSION_CLUSTER_OPS_URL", raising=False)
        assert cluster_ops_url() == "http://127.0.0.1:11457"

    def test_mutating_route_rejects_missing_key(self, stub_cluster, client):
        r = client.put("/v1/cluster-ops/autoscaler/config", json={"enabled": False})
        assert r.status_code == 401

    def test_reads_stay_open_without_key(self, stub_cluster, client):
        assert client.get("/v1/cluster-ops/tasks").status_code == 200
