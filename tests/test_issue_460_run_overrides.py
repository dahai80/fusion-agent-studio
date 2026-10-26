"""Tests for issue #460 — per-run agent overrides on POST /v1/runs.

Importers: pytest runner
API: POST /v1/runs accepts `spec` (or top-level tools/model/max_turns/
     budget_tokens); spec runs ride the SubAgentRunner path with TaskStore
     writeback via the subagent.run_task RPC.
Data schemas: SubAgentSpec fields (tools/model/max_turns/budget_tokens)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from agent_runtime import api_server

    calls = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        if method == "task.submit":
            return {
                "task": {"task_id": "run-460", "status": "pending"},
                "deduped": False,
            }
        if method == "subagent.run_task":
            return {"status": "ok", "task_id": params.get("task_id"), "name": "spec-name"}
        if method == "graph.execute_async":
            return {"status": "ok"}
        if method == "task.status":
            return {"status": "ok"}
        return {"status": "ok"}

    monkeypatch.setattr(api_server, "_rpc", fake_rpc)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    # keep the run.overrides audit write out of the real audit.db
    fake_audit = type("A", (), {"log_action": staticmethod(lambda **kw: None)})()
    monkeypatch.setattr(api_server, "_get_audit_logger", lambda: fake_audit)
    return TestClient(api_server.app), calls


def test_spec_runs_via_subagent_path(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "do the thing",
        "agent_id": "agent-x",
        "spec": {"tools": ["read_file"], "model": "qwen", "max_turns": 3, "budget_tokens": 500},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-460"
    assert body["spec_applied"] is True
    methods = [m for m, _ in calls]
    assert "subagent.run_task" in methods
    assert "graph.execute_async" not in methods
    spec_call = dict(calls[methods.index("subagent.run_task")][1])
    assert spec_call["task_id"] == "run-460"
    assert spec_call["goal"] == "do the thing"
    assert spec_call["spec"]["tools"] == ["read_file"]
    assert spec_call["spec"]["max_turns"] == 3


def test_top_level_overrides_merged_into_spec(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "tools": ["shell_exec"],
        "max_turns": 5,
    })
    assert resp.status_code == 200
    assert resp.json()["spec_applied"] is True
    methods = [m for m, _ in calls]
    spec = dict(calls[methods.index("subagent.run_task")][1])["spec"]
    assert spec["tools"] == ["shell_exec"]
    assert spec["max_turns"] == 5


def test_no_spec_keeps_graph_path(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={"goal": "g", "graph_id": "graph-1"})
    assert resp.status_code == 200
    assert "spec_applied" not in resp.json()
    methods = [m for m, _ in calls]
    assert "graph.execute_async" in methods
    assert "subagent.run_task" not in methods


def test_unknown_spec_key_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={"goal": "g", "spec": {"nonsense": 1}})
    assert resp.status_code == 400
    assert "nonsense" in resp.text


def test_bad_max_turns_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={"goal": "g", "spec": {"max_turns": 0}})
    assert resp.status_code == 400
    resp = tc.post("/v1/runs", json={"goal": "g", "spec": {"max_turns": "abc"}})
    assert resp.status_code == 400


def test_spec_not_dict_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={"goal": "g", "spec": ["nope"]})
    assert resp.status_code == 400


def test_deduped_hit_does_not_redispatch(client):
    from agent_runtime import api_server

    tc, calls = client

    async def deduped_submit(method, params):
        calls.append((method, params))
        if method == "task.submit":
            return {"task": {"task_id": "run-old", "status": "completed"}, "deduped": True}
        return {"status": "ok"}

    api_server._rpc = deduped_submit
    resp = tc.post("/v1/runs", json={"goal": "g", "spec": {"max_turns": 2}})
    assert resp.status_code == 200
    methods = [m for m, _ in calls]
    assert "subagent.run_task" not in methods


class _Report:
    def __init__(self, ok=True):
        self.ok = ok
        self.report = "final answer"
        self.turns = 2
        self.tokens_used = 42
        self.agent_id = "sub-1"
        self.partial = False
        self.error = "" if ok else "boom"
        self.duration_ms = 5


class _Runner:
    def __init__(self, report):
        self._report = report
        self.spec = None

    async def run(self, spec):
        self.spec = spec
        return self._report


class _Runtime:
    def __init__(self, runner):
        self.subagent_runner = runner


class _Daemon:
    def __init__(self, runtime, store):
        self._runtime = runtime
        self._store = store

    def _get_runtime(self):
        return self._runtime

    def _get_task_store(self):
        return self._store


class TestSubagentRunTask:
    @pytest.fixture()
    def store(self):
        from agent_runtime.task_store import Task, TaskStore

        ts = TaskStore()
        ts.submit(Task(task_id="t1", title="t", input="goal text"))
        return ts

    @pytest.mark.asyncio
    async def test_run_task_writes_back_completed(self, store):
        from agent_runtime.dispatchers.subagent import SubAgentDispatcher

        runner = _Runner(_Report(ok=True))
        disp = SubAgentDispatcher(_Daemon(_Runtime(runner), store))
        res = await disp._handle_subagent_run_task({"task_id": "t1", "goal": "goal text", "spec": {"max_turns": 4}})
        assert res["status"] == "ok"
        await list(disp._run_task_bg_tasks)[0]
        task = store.get("t1")
        assert task.status == "completed"
        assert task.last_result["report"] == "final answer"
        assert task.last_result["turns"] == 2
        assert task.last_result["tokens_used"] == 42
        assert runner.spec.max_turns == 4
        assert runner.spec.task == "goal text"

    @pytest.mark.asyncio
    async def test_run_task_writes_back_failed(self, store):
        from agent_runtime.dispatchers.subagent import SubAgentDispatcher

        runner = _Runner(_Report(ok=False))
        disp = SubAgentDispatcher(_Daemon(_Runtime(runner), store))
        res = await disp._handle_subagent_run_task({"task_id": "t1", "goal": "g"})
        assert res["status"] == "ok"
        await list(disp._run_task_bg_tasks)[0]
        task = store.get("t1")
        assert task.status == "failed"
        assert task.last_error == "boom"

    @pytest.mark.asyncio
    async def test_run_task_requires_task_id(self, store):
        from agent_runtime.dispatchers.subagent import SubAgentDispatcher

        disp = SubAgentDispatcher(_Daemon(_Runtime(_Runner(_Report())), store))
        res = await disp._handle_subagent_run_task({})
        assert res["status"] == "error"
        assert "task_id" in res["message"]
