"""#674: POST /v1/runs with unknown graph_id must fail fast, not pend forever.

Graph-bound runs are pre-claimed (owner_agent="api") so the task executor
never dequeues them — the only execution path is the inline graph.execute_async
kick in the submit handler. When that kick raises (e.g. "Graph not found"),
the handler must mark the task failed, symmetric with the spec-override path
and the agent_id orphan fast-fail.
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
        self._seq = 0
        self.status_calls: list[dict] = []

    def _get_handler(self, method: str):
        if method == "task.submit":
            return self._task_submit
        if method == "task.get":
            return self._task_get
        if method == "task.status":
            return self._task_status
        if method == "graph.execute_async":
            return self._graph_execute
        return None

    async def _task_submit(self, params: dict) -> dict:
        self._seq += 1
        task = Task(
            task_id=params.get("task_id", "") or f"run-{int(time.time()*1000)}-{self._seq}",
            title=params.get("title", ""),
            description=params.get("description", ""),
            agent_id=params.get("agent_id", ""),
            graph_id=params.get("graph_id", ""),
            trigger=params.get("trigger", "immediate"),
            input=params.get("input", ""),
            status="pending",
            owner_agent=params.get("owner_agent", ""),
        )
        task = self._store.submit(task)
        return {"status": "ok", "task": task.to_dict(), "deduped": False}

    async def _task_get(self, params: dict) -> dict:
        task = self._store.get(params.get("task_id", ""))
        if not task:
            return {"status": "error", "message": "not found"}
        return {"task": task.to_dict()}

    async def _task_status(self, params: dict) -> dict:
        self.status_calls.append(params)
        ok = self._store.update_status(
            params.get("task_id", ""),
            params.get("status", ""),
            last_error=params.get("last_error", ""),
        )
        if not ok:
            return {"status": "error", "message": "task not found"}
        return {"status": "ok"}

    async def _graph_execute(self, params: dict) -> dict:
        raise ValueError(f"Graph not found: {params.get('graph_id', '')}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".fusion-agent-studio").mkdir()
    import agent_runtime.api_server as api_mod

    monkeypatch.setattr(api_mod, "_auth_configured", lambda: False)
    store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    daemon = _FakeDaemon(store)
    set_daemon(daemon)
    yield TestClient(app), daemon
    set_daemon(None)


def test_unknown_graph_id_fails_fast(client):
    c, daemon = client
    r = c.post("/v1/runs", json={"goal": "p2", "graph_id": "no-such-graph-xyz"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert run_id
    # pre-claimed by the submit path (owner_agent="api")
    assert daemon._store.get(run_id).owner_agent == "api"
    g = c.get(f"/v1/runs/{run_id}")
    assert g.status_code == 200
    body = g.json()
    assert body["status"] == "failed"
    assert "graph not found" in body["error"].lower()
    assert "no-such-graph-xyz" in body["error"]
    assert daemon.status_calls and daemon.status_calls[0]["status"] == "failed"


def test_known_graph_still_dispatches(client, monkeypatch):
    c, daemon = client

    async def _ok_execute(params: dict) -> dict:
        tid = params.get("task_id", "")
        daemon._store.update_status(tid, "completed", last_result={"events": 1})
        return {"status": "completed", "task_id": tid}

    daemon._get_handler_orig = daemon._get_handler

    def _handler(method):
        if method == "graph.execute_async":
            return _ok_execute
        return daemon._get_handler_orig(method)

    monkeypatch.setattr(daemon, "_get_handler", _handler)
    r = c.post("/v1/runs", json={"goal": "happy", "graph_id": "g-ok"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    g = c.get(f"/v1/runs/{run_id}")
    assert g.json()["status"] == "completed"
    assert daemon.status_calls == []
