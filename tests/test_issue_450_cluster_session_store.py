"""Issue #450 — cluster SessionStore backend + cluster router default URL.

fusion-multi-node #84 shipped the shared session/checkpoint store on the
master (/api/v1/session-store, Bearer-authed). agent-studio needs a
`cluster` backend so a session checkpointed on node A resumes on node B,
and DEFAULT_CLUSTER_URL must point at the registered master port 11452.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_runtime.session_store import (
    ClusterSessionStore,
    get_session_store,
    reset_session_store,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_session_store()
    yield
    reset_session_store()


class _FakeMaster(BaseHTTPRequestHandler):
    state = {"sessions": {}, "auth_headers": [], "calls": []}

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        _FakeMaster.state["calls"].append(
            {"method": self.command, "path": self.path, "body": body}
        )
        _FakeMaster.state["auth_headers"].append(
            self.headers.get("Authorization", "")
        )
        return body

    def do_GET(self):  # noqa: N802
        self._record()
        if self.path.startswith("/api/v1/session-store?") or self.path == "/api/v1/session-store":
            sessions = list(_FakeMaster.state["sessions"].values())
            self._send(200, {"status": "ok", "sessions": sessions, "total": len(sessions)})
            return
        sid = self.path.rsplit("/", 1)[-1]
        session = _FakeMaster.state["sessions"].get(sid)
        if session is None:
            self._send(404, {"detail": "not found"})
        else:
            self._send(200, {"status": "ok", "session": session})

    def do_PUT(self):  # noqa: N802
        body = self._record()
        sid = self.path.rsplit("/", 1)[-1]
        _FakeMaster.state["sessions"][sid] = body
        self._send(200, {"status": "ok", "session": body})

    def do_DELETE(self):  # noqa: N802
        self._record()
        sid = self.path.rsplit("/", 1)[-1]
        if sid not in _FakeMaster.state["sessions"]:
            self._send(404, {"detail": "not found"})
            return
        del _FakeMaster.state["sessions"][sid]
        self._send(200, {"status": "ok", "deleted": sid})

    def do_POST(self):  # noqa: N802
        self._record()
        if self.path.endswith("/fork"):
            sid = self.path.rsplit("/", 2)[-2]
            parent = _FakeMaster.state["sessions"].get(sid)
            if parent is None:
                self._send(404, {"detail": "not found"})
                return
            forked = dict(parent)
            forked["id"] = "chat-forked"
            forked["metadata"] = {**(parent.get("metadata") or {}), "forked_from": sid}
            _FakeMaster.state["sessions"][forked["id"]] = forked
            self._send(200, {"status": "ok", "session": forked})

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_master():
    _FakeMaster.state = {"sessions": {}, "auth_headers": [], "calls": []}
    server = HTTPServer(("127.0.0.1", 0), _FakeMaster)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestDefaultClusterUrl:

    def test_default_is_registered_master_port(self, monkeypatch):
        from agent_runtime import sandbox_router

        monkeypatch.delenv("FUSION_CLUSTER_URL", raising=False)
        assert sandbox_router.DEFAULT_CLUSTER_URL == "http://127.0.0.1:11452"
        assert sandbox_router.cluster_url() == "http://127.0.0.1:11452"

    def test_env_override_still_wins(self, monkeypatch):
        from agent_runtime import sandbox_router

        monkeypatch.setenv("FUSION_CLUSTER_URL", "http://cluster.lan:11452")
        assert sandbox_router.cluster_url() == "http://cluster.lan:11452"


class TestBackendSelection:

    def test_cluster_backend_selected(self, monkeypatch, fake_master):
        monkeypatch.setenv("FUSION_SESSION_STORE", "cluster")
        monkeypatch.setenv("FUSION_CLUSTER_URL", fake_master)
        store = get_session_store()
        assert isinstance(store, ClusterSessionStore)
        assert store.base_url == fake_master

    def test_sqlite_default_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_SESSION_STORE", raising=False)
        store = get_session_store()
        assert type(store).__name__ == "SQLiteSessionStore"

    def test_unknown_backend_falls_back(self, monkeypatch):
        monkeypatch.setenv("FUSION_SESSION_STORE", "redis")
        store = get_session_store()
        assert type(store).__name__ == "SQLiteSessionStore"


class TestClusterStoreRoundtrip:

    def test_put_get_list_delete(self, fake_master):
        store = ClusterSessionStore(base_url=fake_master)
        session = {"id": "chat-a", "title": "node A checkpoint",
                   "messages": [{"role": "user", "content": "hi"}]}
        stored = store.put(session)
        assert stored["id"] == "chat-a"
        assert "updated_at" in stored
        fetched = store.get("chat-a")
        assert fetched is not None
        assert fetched["title"] == "node A checkpoint"
        rows = store.list()
        assert [r["id"] for r in rows] == ["chat-a"]
        assert store.delete("chat-a") is True
        assert store.get("chat-a") is None
        assert store.delete("chat-a") is False

    def test_fork(self, fake_master):
        store = ClusterSessionStore(base_url=fake_master)
        store.put({"id": "chat-b", "title": "parent", "messages": []})
        forked = store.fork("chat-b")
        assert forked is not None
        assert forked["id"] == "chat-forked"
        assert forked["metadata"]["forked_from"] == "chat-b"
        assert store.fork("missing") is None

    def test_bearer_token_sent(self, fake_master):
        store = ClusterSessionStore(base_url=fake_master, token="tok-123")
        store.list()
        assert _FakeMaster.state["auth_headers"][-1] == "Bearer tok-123"

    def test_unreachable_raises(self):
        store = ClusterSessionStore(base_url="http://127.0.0.1:1")
        with pytest.raises(RuntimeError, match="unreachable"):
            store.list()


class TestDelegateRunAuth:

    async def test_delegate_run_sends_bearer(self, monkeypatch):
        from agent_runtime import sandbox_router
        from agent_runtime.sandbox_router import SandboxTarget
        from tests.test_issue_432_cli import FakeAsyncClient, FakeResponse

        monkeypatch.setenv("FUSION_CLUSTER_TOKEN", "cluster-tok")
        fake = FakeAsyncClient(FakeResponse(200, {"task_id": "t1"}))
        target = SandboxTarget(raw="multi-node:n1", mode="multi-node", node="n1")
        result = await sandbox_router.delegate_run(target, "run-1", "a", "", "g", client=fake)
        assert result["ok"] is True
        assert fake.calls[0]["headers"]["Authorization"] == "Bearer cluster-tok"

    async def test_delegate_run_no_token_no_header(self, monkeypatch):
        from agent_runtime import sandbox_router
        from agent_runtime.sandbox_router import SandboxTarget
        from tests.test_issue_432_cli import FakeAsyncClient, FakeResponse

        monkeypatch.delenv("FUSION_CLUSTER_TOKEN", raising=False)
        fake = FakeAsyncClient(FakeResponse(200, {"task_id": "t1"}))
        target = SandboxTarget(raw="multi-node:n1", mode="multi-node", node="n1")
        await sandbox_router.delegate_run(target, "run-1", "a", "", "g", client=fake)
        assert "Authorization" not in fake.calls[0]["headers"]
