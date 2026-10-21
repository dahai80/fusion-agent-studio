import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_runtime import cli
from agent_runtime.api_server import app, set_daemon
from agent_runtime.sandbox_router import SandboxTarget, parse_sandbox
from agent_runtime.task_store import Task, TaskStore


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"x" if payload is not None else b""
        self.text = text or str(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response

    async def aclose(self):
        pass


class FakeSSE:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self.lines


def test_parse_sandbox_variants():
    assert parse_sandbox("").is_local
    assert parse_sandbox("local").raw == "local"
    target = parse_sandbox("multi-node:mini-01")
    assert target.mode == "multi-node"
    assert target.node == "mini-01"
    with pytest.raises(ValueError):
        parse_sandbox("multi-node:")
    with pytest.raises(ValueError):
        parse_sandbox("cloud")


@pytest.mark.asyncio
async def test_delegate_run_posts_cluster_task(monkeypatch):
    from agent_runtime import sandbox_router

    monkeypatch.setenv("FUSION_CLUSTER_URL", "http://cluster.local:11457")
    fake = FakeAsyncClient(FakeResponse(200, {"task_id": "task-abc", "assigned_nodes": ["mini-01"]}))
    target = SandboxTarget(raw="multi-node:mini-01", mode="multi-node", node="mini-01")
    result = await sandbox_router.delegate_run(target, "run-1", "agent-1", "", "goal", client=fake)
    assert result == {
        "ok": True,
        "node": "mini-01",
        "cluster_task_id": "task-abc",
        "assigned_nodes": ["mini-01"],
    }
    call = fake.calls[0]
    assert call["url"] == "http://cluster.local:11457/api/tasks/submit"
    assert call["headers"]["X-Fusion-Target-Node"] == "mini-01"
    assert call["json"]["name"].startswith("run-1:")


@pytest.mark.asyncio
async def test_delegate_run_surfaces_cluster_errors(monkeypatch):
    from agent_runtime import sandbox_router

    fake = FakeAsyncClient(FakeResponse(503, None, "down"))
    target = parse_sandbox("multi-node:mini-02")
    result = await sandbox_router.delegate_run(target, "run-2", "", "graph-1", "goal", client=fake)
    assert not result["ok"]
    assert "503" in result["error"]


def test_task_store_persists_sandbox(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    store.submit(Task(task_id="t1", title="x", sandbox="multi-node:mini-03"))
    store.submit(Task(task_id="t2", title="y"))
    assert store.get("t1").sandbox == "multi-node:mini-03"
    assert store.get("t2").sandbox == "local"
    store.close()


def test_task_store_migrates_old_schema(tmp_path):
    from agent_runtime.task_store import _TASK_COLUMNS

    db = tmp_path / "old.db"
    old_columns = [c for c in _TASK_COLUMNS if c != "sandbox"]
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks ("
        + ", ".join(f"{c} TEXT" for c in old_columns)
        + ", PRIMARY KEY (task_id))"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, title, status, created_at, updated_at) "
        "VALUES ('old', 'legacy', 'completed', 1, 1)"
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    store = TaskStore(str(db))
    task = store.get("old")
    assert task is not None
    assert task.sandbox == "local"
    store.close()


class _FakeDaemon:
    def __init__(self, store: TaskStore):
        self._store = store
        self.seq = 0

    def _get_handler(self, method):
        return getattr(self, f"_h_{method.replace('.', '_')}", None)

    async def _h_task_submit(self, params):
        self.seq += 1
        task = Task(
            task_id=params.get("task_id", "") or f"run-{self.seq}",
            title=params.get("title", ""),
            agent_id=params.get("agent_id", ""),
            graph_id=params.get("graph_id", ""),
            input=params.get("input", ""),
            status="pending",
            sandbox=params.get("sandbox", "local"),
        )
        task = self._store.submit(task)
        return {"status": "ok", "task": task.to_dict(), "deduped": False}

    async def _h_task_get(self, params):
        task = self._store.get(params.get("task_id", ""))
        if not task:
            return {"status": "error", "message": "not found"}
        return {"task": task.to_dict()}

    async def _h_task_status(self, params):
        self._store.update_status(
            params["task_id"],
            params["status"],
            last_error=params.get("last_error", ""),
        )
        return {"status": "ok"}

    async def _h_graph_execute_async(self, params):
        return {"status": "ok"}

    async def _h_task_events(self, params):
        return {
            "status": "ok",
            "spans": {"nodes": [{"node_id": "start", "type": "start"}]},
        }


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    from agent_runtime import api_server as api_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(api_mod, "_auth_configured", lambda: False)
    store = TaskStore(str(tmp_path / "tasks.db"))
    daemon = _FakeDaemon(store)
    set_daemon(daemon)
    yield TestClient(app)
    store.close()
    set_daemon(None)


def test_submit_run_records_local_sandbox(api_client):
    res = api_client.post("/v1/runs", json={"goal": "hello", "agent_id": "a1"})
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    detail = api_client.get(f"/v1/runs/{run_id}").json()
    assert detail["status"] == "pending"


def test_submit_run_rejects_unknown_sandbox(api_client):
    res = api_client.post("/v1/runs", json={"goal": "hello", "sandbox": "cloud"})
    assert res.status_code == 400


def test_submit_run_delegates_to_cluster(api_client, monkeypatch):
    async def fake_delegate(target, task_id, agent_id, graph_id, goal, client=None):
        return {"ok": True, "node": target.node, "cluster_task_id": "ct-1", "assigned_nodes": []}

    monkeypatch.setattr("agent_runtime.sandbox_router.delegate_run", fake_delegate)
    res = api_client.post(
        "/v1/runs",
        json={"goal": "hello", "graph_id": "g1", "sandbox": "multi-node:mini-01"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "running"
    assert body["sandbox"]["node"] == "mini-01"


def test_submit_run_cluster_failure_marks_run_failed(api_client, monkeypatch):
    async def fake_delegate(target, task_id, agent_id, graph_id, goal, client=None):
        return {"ok": False, "node": target.node, "error": "cluster router unreachable"}

    monkeypatch.setattr("agent_runtime.sandbox_router.delegate_run", fake_delegate)
    res = api_client.post(
        "/v1/runs", json={"goal": "hello", "graph_id": "g1", "sandbox": "multi-node:mini-01"}
    )
    assert res.status_code == 500
    assert "cluster router unreachable" in res.text


def test_run_stream_endpoint_tails_status(api_client):
    run_id = api_client.post("/v1/runs", json={"goal": "hello"}).json()["run_id"]
    with api_client.stream("GET", f"/v1/runs/{run_id}/stream?interval=0.1&timeout=1") as res:
        body = "".join(res.iter_text())
    assert "'type': 'status'" in body or '"type": "status"' in body


def test_cli_client_request_retries(monkeypatch):
    calls = []

    def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(502, None, "bad gateway")
        return FakeResponse(200, {"run_id": "r1"})

    monkeypatch.setattr(cli.httpx, "request", fake_request)
    client = cli.AgentStudioClient(base_url="http://x", retries=1)
    assert client.get_run("r1")["run_id"] == "r1"
    assert len(calls) == 2


def test_cli_stream_parses_sse(monkeypatch):
    lines = [
        "event: span",
        'data: {"type":"span","span":{"node_id":"start"}}',
        "",
        'data: {"type":"done","status":"completed"}',
        "",
    ]

    def fake_stream(method, url, headers=None, timeout=None):
        assert "api_key=k" in url
        return FakeSSE(lines)

    monkeypatch.setattr(cli.httpx, "stream", fake_stream)
    client = cli.AgentStudioClient(base_url="http://x", api_key="k")
    frames = list(client.stream_run("r1"))
    assert [f["type"] for f in frames] == ["span", "done"]
    assert frames[0]["event"] == "span"


def test_cli_submit_prints_run_and_skips_stream(monkeypatch, capsys):
    calls = []

    def fake_request(self, method, path, payload=None):
        calls.append((method, path, payload))
        return {"run_id": "r9", "status": "pending"}

    monkeypatch.setattr(cli.AgentStudioClient, "request", fake_request)
    rc = cli.main(["submit", "agent:writer", "-i", "do it", "--no-stream"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run_id=r9" in out
    assert calls[0][1] == "/v1/runs"
    assert calls[0][2]["agent_id"] == "writer"


def test_cli_submit_streams_events(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.AgentStudioClient, "request", lambda self, m, p, b=None: {"run_id": "r9", "status": "pending"}
    )
    monkeypatch.setattr(
        cli.AgentStudioClient,
        "stream_run",
        lambda self, rid: iter(
            [
                {"type": "status", "status": "running"},
                {"type": "span", "span": {"node_id": "start", "status": "ok", "duration_ms": 2}},
                {"type": "done", "status": "completed"},
            ]
        ),
    )
    rc = cli.main(["submit", "graph:g1", "-i", "go"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run_id=r9" in out
    assert "[running]" in out
    assert "start ok 2ms" in out
    assert "done status=completed" in out


def test_cli_resume_and_list(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.AgentStudioClient,
        "request",
        lambda self, m, p, b=None: {"status": "ok", "sessions": [{"id": "s1", "title": "T", "mode": "chat"}]}
        if p.startswith("/v1/sessions?")
        else {"status": "ok", "background_session_id": "bg1"},
    )
    assert cli.main(["list", "sessions"]) == 0
    assert "s1" in capsys.readouterr().out
    assert cli.main(["resume", "s1", "-i", "continue"]) == 0
    assert "bg1" in capsys.readouterr().out
