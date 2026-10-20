from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.task_store import Task


def _daemon() -> DaemonServer:
    return DaemonServer(socket_path="/tmp/_nonexistent_406.sock")


class TestAggregateRunSpans:
    def test_per_node_counts_and_failure(self):
        spans = [
            {"type": "start", "timestamp": 10.0},
            {"type": "token", "node_id": "a", "timestamp": 11.0},
            {"type": "token", "node_id": "a", "timestamp": 12.0},
            {"type": "tool_call", "node_id": "a", "timestamp": 12.5},
            {"type": "thinking_token", "node_id": "b", "timestamp": 13.0},
            {"type": "retry", "node_id": "b", "timestamp": 13.5},
            {"type": "error", "node_id": "b", "content": "boom", "timestamp": 14.0},
            {"type": "end", "timestamp": 15.0},
            {"type": "run_end", "status": "failed", "stop_reason": "error", "timestamp": 15.2},
        ]
        out = _daemon()._aggregate_run_spans(spans)
        assert out["run_status"] == "failed"
        assert out["stop_reason"] == "error"
        assert out["run_start"] == 10.0
        assert out["run_end"] == 15.2
        nodes = {n["node_id"]: n for n in out["nodes"]}
        assert set(nodes) == {"a", "b"}
        assert nodes["a"]["token_events"] == 2
        assert nodes["a"]["tool_calls"] == 1
        assert nodes["a"]["status"] == "success"
        assert nodes["b"]["token_events"] == 1
        assert nodes["b"]["status"] == "failed"
        assert nodes["b"]["error"] == "boom"
        assert nodes["a"]["start"] == 11.0
        assert nodes["a"]["end"] == 12.5

    def test_usage_tokens_counted_without_token_events(self):
        spans = [
            {
                "type": "think",
                "node_id": "llm1",
                "timestamp": 5.0,
                "metadata": {"usage": {"prompt_tokens": 100, "completion_tokens": 2, "total_tokens": 102}},
            },
        ]
        out = _daemon()._aggregate_run_spans(spans)
        assert out["nodes"][0]["token_events"] == 0
        assert out["nodes"][0]["tokens"] == 102
        assert out["nodes"][0]["status"] == "success"

    def test_streaming_tokens_not_double_counted(self):
        spans = [
            {"type": "token", "node_id": "a", "timestamp": 1.0},
            {"type": "token", "node_id": "a", "timestamp": 2.0},
            {"type": "think", "node_id": "a", "timestamp": 3.0, "metadata": {"usage": {"total_tokens": 50}}},
        ]
        node = _daemon()._aggregate_run_spans(spans)["nodes"][0]
        assert node["token_events"] == 2
        assert node["tokens"] == 2

    def test_empty_spans_defaults(self):
        out = _daemon()._aggregate_run_spans([])
        assert out["nodes"] == []
        assert out["run_status"] == "completed"
        assert out["run_start"] == 0
        assert out["run_end"] == 0


class TestSpanStore:
    def test_append_read_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        d = _daemon()
        d._append_run_span("exec1", "default", {"type": "token", "node_id": "n1"})
        d._append_run_span("exec1", "default", {"type": "end", "node_id": "n1"})
        spans = d._read_run_spans("exec1", "default")
        assert [s["type"] for s in spans] == ["token", "end"]
        assert all("ts" in s for s in spans)
        path = d._run_spans_path("exec1", "default")
        assert path is not None and path.exists()

    def test_empty_execution_id_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        d = _daemon()
        assert d._run_spans_path("", "default") is None
        d._append_run_span("", "default", {"type": "token"})
        assert not (tmp_path / "out").exists()

    def test_team_isolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        d = _daemon()
        d._append_run_span("exec2", "teamA", {"type": "token", "node_id": "n"})
        assert d._read_run_spans("exec2", "teamA")
        assert d._read_run_spans("exec2", "teamB") == []


class TestTaskEventsHandler:
    async def test_unknown_task(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("FUSION_TASK_DB", str(tmp_path / "tasks.db"))
        d = _daemon()
        handler = d._get_handler("task.events")
        assert handler is not None
        out = await handler({"task_id": "missing"})
        assert out["status"] == "error"

    async def test_without_execution_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("FUSION_TASK_DB", str(tmp_path / "tasks.db"))
        d = _daemon()
        store = d._get_task_store()
        store.submit(Task(task_id="t-noexec", title="x", status="completed"))
        out = await d._get_handler("task.events")({"task_id": "t-noexec"})
        assert out["status"] == "ok"
        assert out["spans"]["nodes"] == []
        assert out["spans"]["run_status"] == "completed"

    async def test_with_spans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("FUSION_TASK_DB", str(tmp_path / "tasks.db"))
        d = _daemon()
        store = d._get_task_store()
        store.submit(
            Task(
                task_id="t-span",
                title="x",
                status="completed",
                team="default",
                last_result={"execution_id": "exec-span"},
            )
        )
        d._append_run_span("exec-span", "default", {"type": "token", "node_id": "n1"})
        d._append_run_span(
            "exec-span", "default", {"type": "run_end", "status": "completed", "stop_reason": "end"}
        )
        out = await d._get_handler("task.events")({"task_id": "t-span"})
        assert out["status"] == "ok"
        assert out["execution_id"] == "exec-span"
        assert [n["node_id"] for n in out["nodes"]] == ["n1"]
        assert out["nodes"][0]["token_events"] == 1
        assert out["stop_reason"] == "end"


class TestShellExtManifest:
    def test_manifest_ext_fields(self):
        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "web" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        pages = manifest.get("pages")
        assert isinstance(pages, list) and pages
        routes = {p["route"] for p in pages}
        assert {"/", "/runs", "/canvas", "/agents"} <= routes
        for p in pages:
            assert p["route"].startswith("/")
            assert p.get("title") and p.get("title_en")
        search = manifest.get("searchProviders")
        assert search and search[0]["endpoint"].startswith("/shell/fusion-agent-studio/")
        notes = manifest.get("notifications")
        assert notes and notes[0]["endpoint"].startswith("/shell/fusion-agent-studio/")


class TestShellExtEndpoints:
    def test_search_requires_query(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        res = client.get("/v1/search", params={"q": ""})
        assert res.status_code == 200
        assert res.json() == {"items": []}

    def test_search_shape(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        res = client.get("/v1/search", params={"q": "no-such-graph-zzz", "limit": 5})
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body["items"], list)
        for item in body["items"]:
            assert item["route"].startswith("/")
            assert item["kind"] in {"workflow", "run"}

    def test_notifications_shape(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        res = client.get("/api/v1/notifications/runs", params={"limit": 5})
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == len(body["items"])
        for item in body["items"]:
            assert item["id"]
            assert item["route"].startswith("/")
            assert item["level"] in {"info", "warning", "error"}

    def test_run_events_unknown_404(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from agent_runtime import api_server

        monkeypatch.setenv("FUSION_TASK_DB", str(tmp_path / "t.db"))
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        monkeypatch.setattr(api_server, "_daemon", _daemon())
        monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
        client = TestClient(api_server.app)
        res = client.get("/v1/runs/no-such-run-zzz/events")
        assert res.status_code == 404

    def test_run_events_payload(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from agent_runtime import api_server

        monkeypatch.setenv("FUSION_TASK_DB", str(tmp_path / "t.db"))
        monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
        d = _daemon()
        d._get_task_store().submit(
            Task(
                task_id="run-events-1",
                title="waterfall",
                status="completed",
                last_result={"execution_id": "exec-events-1"},
            )
        )
        d._append_run_span("exec-events-1", "default", {"type": "token", "node_id": "n1"})
        d._append_run_span("exec-events-1", "default", {"type": "tool_call", "node_id": "n1"})
        d._append_run_span(
            "exec-events-1",
            "default",
            {"type": "run_end", "status": "completed", "stop_reason": "end"},
        )
        monkeypatch.setattr(api_server, "_daemon", d)
        monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
        res = TestClient(api_server.app).get("/api/v1/runs/run-events-1/events")
        assert res.status_code == 200
        body = res.json()
        assert body["execution_id"] == "exec-events-1"
        assert body["nodes"][0]["node_id"] == "n1"
        assert body["nodes"][0]["token_events"] == 1
        assert body["nodes"][0]["tool_calls"] == 1
        assert body["stop_reason"] == "end"


class TestRpcPrefix:
    def test_rpc_path_carries_api_base(self):
        # #405: the embedded bridge fetches paths at gateway origin root; a
        # bare /v1/rpc lands on the SPA catch-all (HTML). Source-level guard.
        src = (
            Path(__file__).resolve().parents[1] / "web" / "src" / "api.ts"
        ).read_text(encoding="utf-8")
        assert "postJson<T & { status?: string; message?: string }>(`${B()}/v1/rpc`" in src
        assert "postJson<T & { status?: string; message?: string }>('/v1/rpc'" not in src


@pytest.mark.parametrize("path", ["/v1/search", "/v1/notifications/runs"])
def test_api_v1_aliases(path):
    from fastapi.testclient import TestClient

    from agent_runtime.api_server import app

    client = TestClient(app)
    res = client.get(f"/api{path}")
    assert res.status_code == 200
