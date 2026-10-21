from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.chat_engine import ChatMessage, ChatSession
from agent_runtime.context import AgentEvent, AgentEventType
from agent_runtime.daemon_server import DaemonServer
from agent_runtime.file_checkpoint import FileCheckpointStore
from agent_runtime.persistence import AgentStore
from agent_runtime.session_manager import (
    BackgroundSession,
    SessionManager,
    SessionStatus,
)
from agent_runtime.session_store import (
    FileSessionStore,
    SQLiteSessionStore,
    get_session_store,
    reset_session_store,
)


@pytest.fixture
def store(tmp_path):
    return AgentStore(str(tmp_path / "store.db"))


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


def _session(title="Session", messages=None):
    return ChatSession(
        title=title,
        messages=[ChatMessage(role=r, content=c) for r, c in (messages or [])],
    )


class TestSQLiteSessionStore:
    def test_put_get_roundtrip(self, store):
        session = _session("Alpha", [("user", "hi"), ("assistant", "hello")])
        backend = SQLiteSessionStore(store)
        backend.put(session.to_dict())
        loaded = backend.get(session.id)
        assert loaded is not None
        assert loaded["title"] == "Alpha"
        assert [m["content"] for m in loaded["messages"]] == ["hi", "hello"]

    def test_list_returns_saved_sessions(self, store):
        backend = SQLiteSessionStore(store)
        backend.put(_session("One").to_dict())
        backend.put(_session("Two").to_dict())
        titles = {row["title"] for row in backend.list()}
        assert {"One", "Two"} <= titles

    def test_delete(self, store):
        backend = SQLiteSessionStore(store)
        session = _session("Gone")
        backend.put(session.to_dict())
        assert backend.delete(session.id) is True
        assert backend.get(session.id) is None

    def test_fork_copies_messages_and_marks_parent(self, store):
        backend = SQLiteSessionStore(store)
        parent = _session("Parent", [("user", "keep me")])
        backend.put(parent.to_dict())
        forked = backend.fork(parent.id, title="Child")
        assert forked is not None
        assert forked["id"] != parent.id
        assert forked["title"] == "Child"
        assert forked["metadata"]["forked_from"] == parent.id
        assert [m["content"] for m in forked["messages"]] == ["keep me"]

    def test_fork_missing_session(self, store):
        assert SQLiteSessionStore(store).fork("nope") is None


class TestFileSessionStore:
    def test_put_get_list_delete(self, tmp_path):
        backend = FileSessionStore(str(tmp_path / "fs"))
        session = _session("File", [("user", "x")])
        backend.put(session.to_dict())
        assert backend.get(session.id)["messages"][0]["content"] == "x"
        rows = backend.list()
        assert [r["id"] for r in rows] == [session.id]
        assert "messages" not in rows[0]
        assert backend.delete(session.id) is True
        assert backend.get(session.id) is None

    def test_cross_instance_resume(self, tmp_path):
        # #429: two store instances over the same dir see the same sessions —
        # the basis for resuming on a second host / process.
        base = str(tmp_path / "shared")
        first = FileSessionStore(base)
        session = _session("Shared", [("user", "hello")])
        first.put(session.to_dict())
        second = FileSessionStore(base)
        loaded = second.get(session.id)
        assert loaded is not None
        assert loaded["messages"][0]["content"] == "hello"

    def test_path_traversal_rejected(self, tmp_path):
        backend = FileSessionStore(str(tmp_path / "fs"))
        assert backend.get("../escape") is None
        with pytest.raises(ValueError):
            backend.put({"id": "../escape"})

    def test_fork(self, tmp_path):
        backend = FileSessionStore(str(tmp_path / "fs"))
        parent = _session("P", [("user", "q")])
        backend.put(parent.to_dict())
        forked = backend.fork(parent.id)
        assert forked["metadata"]["forked_from"] == parent.id
        assert forked["id"] != parent.id

    def test_get_session_store_file_backend(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_SESSION_STORE", "file")
        monkeypatch.setenv("FUSION_SESSION_STORE_DIR", str(tmp_path / "sess"))
        reset_session_store()
        backend = get_session_store()
        assert isinstance(backend, FileSessionStore)

    def test_get_session_store_defaults_to_sqlite(self, store):
        reset_session_store()
        assert isinstance(get_session_store(store), SQLiteSessionStore)


class TestSessionDispatcher:
    @pytest.mark.asyncio
    async def test_list_get_messages_rename(self, daemon):
        session = _session("RPC", [("user", "ping"), ("assistant", "pong")])
        daemon.store.save_chat_session(session)

        listed = await _run(daemon, "sessions.list", {})
        assert listed["status"] == "ok"
        assert session.id in [row["id"] for row in listed["sessions"]]

        got = await _run(daemon, "sessions.get", {"session_id": session.id})
        assert got["session"]["title"] == "RPC"
        assert "messages" not in got["session"]

        msgs = await _run(daemon, "sessions.messages", {"session_id": session.id})
        assert [m["content"] for m in msgs["messages"]] == ["ping", "pong"]

        renamed = await _run(
            daemon,
            "sessions.rename",
            {"session_id": session.id, "title": "New", "tag": "work", "summary": "s"},
        )
        assert renamed["session"]["title"] == "New"
        assert renamed["session"]["metadata"]["tags"] == ["work"]
        tags = await _run(daemon, "sessions.list", {"tag": "work"})
        assert [row["id"] for row in tags["sessions"]] == [session.id]

    @pytest.mark.asyncio
    async def test_missing_session_errors(self, daemon):
        assert (await _run(daemon, "sessions.get", {"session_id": "nope"}))["status"] == "error"
        assert (await _run(daemon, "sessions.messages", {"session_id": "nope"}))["status"] == "error"
        assert (await _run(daemon, "sessions.rename", {"session_id": "nope"}))["status"] == "error"
        assert (await _run(daemon, "sessions.fork", {"session_id": "nope"}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_fork_handler(self, daemon):
        session = _session("Fork me", [("user", "a")])
        daemon.store.save_chat_session(session)
        result = await _run(daemon, "sessions.fork", {"session_id": session.id})
        assert result["status"] == "ok"
        assert result["forked_from"] == session.id
        assert result["session"]["id"] != session.id

    @pytest.mark.asyncio
    async def test_store_info_reports_backend(self, daemon):
        result = await _run(daemon, "sessions.store_info", {})
        assert result["status"] == "ok"
        assert result["backend"] == "sqlite"

    @pytest.mark.asyncio
    async def test_resume_delegates_to_session_manager(self, daemon, monkeypatch):
        session = _session("Resume", [("user", "prior")])
        daemon.store.save_chat_session(session)
        captured = {}

        class FakeManager:
            async def fork(self, session_id, input_text="", summarize=False):
                captured["session_id"] = session_id
                captured["input"] = input_text
                return BackgroundSession(
                    forked_from=session_id,
                    input_text=input_text,
                    metadata={"fork_mode": "deep_copy"},
                )

        monkeypatch.setattr(daemon, "_get_session_manager", lambda: FakeManager())
        result = await _run(
            daemon,
            "sessions.resume",
            {"session_id": session.id, "input": "continue"},
        )
        assert result["status"] == "ok"
        assert captured == {"session_id": session.id, "input": "continue"}
        assert result["fork_mode"] == "deep_copy"
        assert result["state"] == "running"

    @pytest.mark.asyncio
    async def test_inject_input_unknown_session(self, daemon):
        daemon._session_manager = SessionManager(runtime=None, store=None)
        result = await _run(
            daemon, "sessions.inject_input", {"session_id": "bg_missing", "text": "x"}
        )
        assert result["status"] == "error"


class TestSessionInjectInput:
    def test_unknown_session_rejected(self):
        manager = SessionManager(runtime=None, store=None)
        assert manager.inject_input("nope", "hello")["ok"] is False

    def test_empty_text_rejected(self):
        manager = SessionManager(runtime=None, store=None)
        bg = BackgroundSession(status=SessionStatus.RUNNING)
        manager._sessions[bg.id] = bg
        assert manager.inject_input(bg.id, "")["ok"] is False

    def test_finished_session_rejected(self):
        manager = SessionManager(runtime=None, store=None)
        bg = BackgroundSession(status=SessionStatus.COMPLETED)
        manager._sessions[bg.id] = bg
        assert manager.inject_input(bg.id, "late")["ok"] is False

    @pytest.mark.asyncio
    async def test_injected_turns_consumed_after_turn_boundary(self):
        class FakeRuntime:
            def __init__(self):
                self.inputs = []
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def execute_graph(self, graph, input_text, ctx):
                self.inputs.append(input_text)
                self.started.set()
                await self.release.wait()
                yield AgentEvent(type=AgentEventType.THINK, content=f"done:{input_text}")

        runtime = FakeRuntime()
        manager = SessionManager(runtime=runtime, store=None)
        bg = await manager.fork("parent", "first")
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        queued = manager.inject_input(bg.id, "second")
        assert queued == {"ok": True, "session_id": bg.id, "pending": 1}
        runtime.release.set()
        await asyncio.wait_for(manager._tasks[bg.id], timeout=5)
        assert runtime.inputs == ["first", "second"]
        assert bg.status is SessionStatus.COMPLETED
        assert bg.output == "done:second"
        assert bg.injected == []


class TestFileCheckpoint:
    def test_snapshot_manifest_and_excludes(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        (root / "a.txt").write_text("original", encoding="utf-8")
        git_dir = root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref", encoding="utf-8")
        backend = FileCheckpointStore(str(tmp_path / "snap"))
        result = backend.snapshot(root=str(root), run_id="run-1", label="before")
        assert result["snapshot_id"].startswith("fc-")
        assert result["file_count"] == 1
        manifest = json.loads(
            (backend.snapshot_dir / result["snapshot_id"] / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert "a.txt" in manifest["files"]
        assert ".git/HEAD" not in manifest["files"]
        listed = backend.list(run_id="run-1")
        assert listed[0]["snapshot_id"] == result["snapshot_id"]

    def test_rollback_restores_and_removes_created(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        original = root / "a.txt"
        original.write_text("original", encoding="utf-8")
        kept = root / "kept.txt"
        kept.write_text("kept", encoding="utf-8")
        backend = FileCheckpointStore(str(tmp_path / "snap"))
        snap = backend.snapshot(root=str(root), label="checkpoint")

        original.write_text("mutated", encoding="utf-8")
        kept.unlink()
        (root / "new.txt").write_text("new", encoding="utf-8")

        result = backend.rollback(snap["snapshot_id"], root=str(root))
        assert result["ok"] is True
        assert result["restored"] == 2
        assert original.read_text(encoding="utf-8") == "original"
        assert kept.read_text(encoding="utf-8") == "kept"
        assert not (root / "new.txt").exists()

    def test_rollback_unknown_snapshot(self, tmp_path):
        backend = FileCheckpointStore(str(tmp_path / "snap"))
        result = backend.rollback("fc-doesnotexist")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_invalid_snapshot_id_rejected(self, tmp_path):
        backend = FileCheckpointStore(str(tmp_path / "snap"))
        result = backend.rollback("../escape")
        assert result["ok"] is False

    def test_delete(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        (root / "a.txt").write_text("x", encoding="utf-8")
        backend = FileCheckpointStore(str(tmp_path / "snap"))
        snap = backend.snapshot(root=str(root))
        assert backend.delete(snap["snapshot_id"]) is True
        assert backend.list() == []
        assert not (backend.snapshot_dir / snap["snapshot_id"]).exists()

    @pytest.mark.asyncio
    async def test_dispatcher_surface(self, daemon, tmp_path):
        root = tmp_path / "workspace"
        root.mkdir()
        target = root / "w.txt"
        target.write_text("v1", encoding="utf-8")

        snap = await _run(
            daemon,
            "file_checkpoint.snapshot",
            {"root": str(root), "run_id": "r1", "label": "l1"},
        )
        assert snap["status"] == "ok"
        assert snap["file_count"] == 1

        listed = await _run(daemon, "file_checkpoint.list", {"run_id": "r1"})
        assert listed["snapshots"][0]["snapshot_id"] == snap["snapshot_id"]

        target.write_text("v2", encoding="utf-8")
        rolled = await _run(
            daemon,
            "file_checkpoint.rollback",
            {"snapshot_id": snap["snapshot_id"], "root": str(root)},
        )
        assert rolled["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v1"

        deleted = await _run(
            daemon, "file_checkpoint.delete", {"snapshot_id": snap["snapshot_id"]}
        )
        assert deleted["deleted"] is True

        missing = await _run(
            daemon, "file_checkpoint.rollback", {"snapshot_id": "fc-missing"}
        )
        assert missing["status"] == "error"


class TestSessionRestRoutes:
    def test_routes_registered(self):
        from fastapi import FastAPI

        from agent_runtime.api_server import app

        assert isinstance(app, FastAPI)
        paths = {route.path for route in app.routes}
        for path in (
            "/v1/sessions",
            "/v1/sessions/{session_id}",
            "/v1/sessions/{session_id}/messages",
            "/v1/sessions/{session_id}/resume",
            "/v1/sessions/{session_id}/fork",
            "/v1/sessions/{session_id}/inject",
        ):
            assert path in paths, path
