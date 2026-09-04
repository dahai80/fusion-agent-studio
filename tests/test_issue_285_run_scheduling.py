"""#285: test-agent scheduling REST contract for external automated test platforms.

Exercises /v1/runs endpoints (submit/get/result/cancel/list) which wrap the
existing task.* RPC handlers on the daemon. Uses a fake daemon with a real
TaskStore (temp db) so the full submit/get/cancel state machine is covered.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_runtime.api_server import app, set_daemon
from agent_runtime.task_store import Task, TaskStore


class _FakeDaemon:
    def __init__(self, store: TaskStore):
        self._store = store
        self.executed: list[dict] = []

    def _get_handler(self, method: str):
        if method == "task.submit":
            return self._task_submit
        if method == "task.get":
            return self._task_get
        if method == "task.list":
            return self._task_list
        if method == "task.cancel":
            return self._task_cancel
        if method == "graph.execute":
            return self._graph_execute
        return None

    async def _task_submit(self, params: dict) -> dict:
        task = Task(
            task_id=params.get("task_id", "") or f"run-{int(time.time()*1000)}",
            title=params.get("title", ""),
            description=params.get("description", ""),
            agent_id=params.get("agent_id", ""),
            graph_id=params.get("graph_id", ""),
            trigger=params.get("trigger", "immediate"),
            input=params.get("input", ""),
            status="pending",
            priority=int(params.get("priority", 0) or 0),
            max_retries=int(params.get("max_retries", 0) or 0),
            idempotency_key=params.get("idempotency_key", ""),
        )
        task = self._store.submit(task)
        return {"status": "ok", "task": task.to_dict(), "deduped": False}

    async def _task_get(self, params: dict) -> dict:
        task = self._store.get(params.get("task_id", ""))
        if not task:
            return {"status": "error", "message": "not found"}
        return {"task": task.to_dict()}

    async def _task_list(self, params: dict) -> dict:
        tasks = self._store.list(
            status=params.get("status", ""),
            agent_id=params.get("agent_id", ""),
            limit=int(params.get("limit", 100) or 100),
        )
        return {"tasks": tasks, "total": len(tasks)}

    async def _task_cancel(self, params: dict) -> dict:
        ok = self._store.cancel(params.get("task_id", ""))
        if not ok:
            return {"status": "error", "message": "not cancelable"}
        return {"status": "ok"}

    async def _graph_execute(self, params: dict) -> dict:
        self.executed.append(params)
        tid = params.get("task_id", "")
        if tid:
            self._store.add_artifacts(tid, ["a1"])
            self._store.update_status(tid, "completed", last_result={"session_id": "s1", "events": 3, "artifact_ids": ["a1"]})
        return {"session_id": "s1", "events": [], "status": "completed", "task_id": tid, "artifact_ids": ["a1"], "tool_errors": []}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolate from the user's real ~/.fusion-agent-studio api keys + db
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".fusion-agent-studio").mkdir()
    import agent_runtime.api_server as api_mod

    monkeypatch.setattr(api_mod, "_auth_configured", lambda: False)
    db = tmp_path / "tasks.db"
    store = TaskStore(db_path=str(db))
    daemon = _FakeDaemon(store)
    set_daemon(daemon)
    yield TestClient(app), daemon
    set_daemon(None)


def test_submit_run_returns_run_id(client):
    c, _ = client
    resp = c.post("/v1/runs", json={"goal": "smoke test the agent", "timeout": 30})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "run_id" in data and data["run_id"]
    assert data["status"] == "pending"


def test_submit_run_missing_goal_400(client):
    c, _ = client
    resp = c.post("/v1/runs", json={"timeout": 30})
    assert resp.status_code == 400


def test_get_run_status(client):
    c, _ = client
    r = c.post("/v1/runs", json={"goal": "check status flow"})
    run_id = r.json()["run_id"]
    resp = c.get(f"/v1/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["status"] in ("pending", "running", "completed")
    assert "progress" in data


def test_get_run_unknown_404(client):
    c, _ = client
    resp = c.get("/v1/runs/nonexistent-run")
    assert resp.status_code == 404


def test_get_run_result(client):
    c, daemon = client
    r = c.post("/v1/runs", json={"goal": "produce result", "graph_id": "g1"})
    run_id = r.json()["run_id"]
    # graph.execute hook marks it completed with last_result
    resp = c.get(f"/v1/runs/{run_id}/result")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["status"] == "completed"
    assert "steps" in data and "artifacts" in data
    assert "a1" in data["artifacts"]
    assert daemon.executed  # graph.execute was invoked


def test_cancel_run(client):
    c, _ = client
    r = c.post("/v1/runs", json={"goal": "cancel me"})
    run_id = r.json()["run_id"]
    resp = c.post(f"/v1/runs/{run_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"
    # subsequent get shows canceled
    g = c.get(f"/v1/runs/{run_id}").json()
    assert g["status"] == "canceled"


def test_list_runs(client):
    c, _ = client
    c.post("/v1/runs", json={"goal": "run one"})
    c.post("/v1/runs", json={"goal": "run two"})
    resp = c.get("/v1/runs")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] >= 2
    assert isinstance(data["runs"], list)


def test_runs_503_without_daemon(monkeypatch):
    import agent_runtime.api_server as api_mod

    monkeypatch.setattr(api_mod, "_auth_configured", lambda: False)
    set_daemon(None)
    c = TestClient(app)
    resp = c.post("/v1/runs", json={"goal": "no daemon"})
    assert resp.status_code in (500, 503), resp.text
