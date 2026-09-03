"""Tests for issue #274 — Chat<->FSB integration.

Covers: env-gate (FUSION_FSB_ENABLED), WorkspaceBinder round-trip, FSBClient
fail-soft HTTP (mocked httpx), 4 chat.* RPC handlers, 4 HTTP endpoints incl.
inbound notify hook. Env-gate off = no-op behavior unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent_runtime import api_server
from agent_runtime.fsb_client import FSBClient, is_fsb_enabled
from agent_runtime.workspace_binder import WorkspaceBinder, get_workspace_binder

# ── WorkspaceBinder ──


class TestWorkspaceBinder:
    def test_bind_unbind_round_trip(self):
        b = WorkspaceBinder()
        assert b.get("ws1") is None
        b.bind("ws1", "agentA", "sess1")
        got = b.get("ws1")
        assert got == {"agent_id": "agentA", "session_id": "sess1"}
        b.unbind("ws1")
        assert b.get("ws1") is None

    def test_bind_without_session(self):
        b = WorkspaceBinder()
        b.bind("ws2", "agentB")
        got = b.get("ws2")
        assert got == {"agent_id": "agentB", "session_id": None}

    def test_bind_ignored_for_empty_ids(self):
        b = WorkspaceBinder()
        b.bind("", "agentA")
        b.bind("ws3", "")
        assert b.list() == []

    def test_find_by_agent(self):
        b = WorkspaceBinder()
        b.bind("ws1", "agentA", "s1")
        b.bind("ws2", "agentB")
        assert b.find_by_agent("agentA") == "ws1"
        assert b.find_by_agent("agentB") == "ws2"
        assert b.find_by_agent("agentC") is None

    def test_list_returns_copies(self):
        b = WorkspaceBinder()
        b.bind("ws1", "agentA", "s1")
        lst = b.list()
        assert len(lst) == 1
        assert lst[0]["workspace_id"] == "ws1"
        lst.clear()
        assert len(b.list()) == 1

    def test_singleton(self):
        assert get_workspace_binder() is get_workspace_binder()


# ── FSBClient env-gate ──


class TestFSBEnvGate:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        assert is_fsb_enabled() is False

    def test_enabled_when_one(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        assert is_fsb_enabled() is True

    def test_disabled_other_values(self, monkeypatch):
        for v in ("0", "", "true", "yes"):
            monkeypatch.setenv("FUSION_FSB_ENABLED", v)
            assert is_fsb_enabled() is False

    def test_bind_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        c = FSBClient()
        assert c.bind("ws1", "agentA") is None
        assert c.unbind("ws1") is None
        assert c.chat_run("ws1", "hi") is None


# ── FSBClient HTTP fail-soft (mocked httpx) ──


def _resp(status_code: int = 200, payload: Any = None):
    r = MagicMock()
    r.status_code = status_code
    r.text = "errbody"
    r.json.return_value = payload or {}
    return r


class TestFSBClientHttp:
    def test_bind_success(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", return_value=_resp(200, {"ok": True})):
            assert c.bind("ws1", "agentA") == {"ok": True}

    def test_bind_http_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", return_value=_resp(500)):
            assert c.bind("ws1", "agentA") is None

    def test_bind_network_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", side_effect=ConnectionError("boom")):
            assert c.bind("ws1", "agentA") is None

    def test_chat_run_matched(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch(
            "httpx.post",
            return_value=_resp(200, {"matched": True, "run": {"id": "r1"}}),
        ):
            res = c.chat_run("ws1", "send invoice")
            assert res == {"matched": True, "run": {"id": "r1"}}

    def test_chat_run_404_no_match(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", return_value=_resp(404)):
            res = c.chat_run("ws1", "???")
            assert res == {"matched": False, "status": 404}

    def test_chat_run_network_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", side_effect=ConnectionError("down")):
            assert c.chat_run("ws1", "hi") is None

    def test_unbind_success(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        c = FSBClient(base_url="http://test", timeout=1)
        with patch("httpx.post", return_value=_resp(200, {"ok": True})):
            assert c.unbind("ws1") == {"ok": True}


# ── Chat RPC handlers (ChatDispatcher) ──


class TestChatFSBRpc:
    @pytest.fixture(autouse=True)
    def _fresh_binder(self, monkeypatch):
        monkeypatch.setattr(
            "agent_runtime.workspace_binder._singleton", None
        )
        yield

    def _make_dispatcher(self):
        from agent_runtime.dispatchers.chat import ChatDispatcher

        daemon = MagicMock()
        d = ChatDispatcher(daemon)
        return d, daemon

    async def test_fsb_status_disabled_default(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_status({})
        assert res["enabled"] is False
        assert res["bindings"] == []

    async def test_fsb_bind_missing_params(self):
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_bind({"workspace_id": "", "agent_id": ""})
        assert res["status"] == "error"

    async def test_fsb_bind_disabled_returns_disabled(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_bind(
            {"workspace_id": "ws1", "agent_id": "a1", "session_id": "s1"}
        )
        assert res["status"] == "disabled"
        assert res["bound"] is True

    async def test_fsb_bind_stamps_session_metadata(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        d, daemon = self._make_dispatcher()
        session = MagicMock()
        session.metadata = {}
        engine = MagicMock()
        engine.get_session.return_value = session
        daemon._get_chat_engine.return_value = engine
        res = await d._handle_fsb_bind(
            {"workspace_id": "ws1", "agent_id": "a1", "session_id": "s1"}
        )
        assert res["status"] == "disabled"
        assert session.metadata["fsb_workspace_id"] == "ws1"
        assert session.metadata["fsb_agent_id"] == "a1"

    async def test_fsb_unbind_missing_param(self):
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_unbind({"workspace_id": ""})
        assert res["status"] == "error"

    async def test_fsb_unbind_disabled(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        d, _ = self._make_dispatcher()
        await d._handle_fsb_bind(
            {"workspace_id": "ws1", "agent_id": "a1", "session_id": "s1"}
        )
        res = await d._handle_fsb_unbind({"workspace_id": "ws1"})
        assert res["status"] == "disabled"
        assert res["bound"] is False

    async def test_fsb_run_disabled(self, monkeypatch):
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_run({"workspace_id": "ws1", "query": "hi"})
        assert res["status"] == "disabled"
        assert res["matched"] is False

    async def test_fsb_run_missing_params(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        d, _ = self._make_dispatcher()
        res = await d._handle_fsb_run({"workspace_id": "", "query": ""})
        assert res["status"] == "error"

    async def test_fsb_run_unreachable(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        d, _ = self._make_dispatcher()
        client = MagicMock()
        client.chat_run.return_value = None
        with patch(
            "agent_runtime.fsb_client.get_fsb_client", return_value=client
        ):
            res = await d._handle_fsb_run(
                {"workspace_id": "ws1", "query": "send invoice"}
            )
        assert res["status"] == "error"
        assert res["matched"] is False
        assert "unreachable" in res["message"]

    async def test_fsb_run_matched(self, monkeypatch):
        monkeypatch.setenv("FUSION_FSB_ENABLED", "1")
        d, _ = self._make_dispatcher()
        client = MagicMock()
        client.chat_run.return_value = {"matched": True, "run": {"id": "r1"}}
        with patch(
            "agent_runtime.fsb_client.get_fsb_client", return_value=client
        ):
            res = await d._handle_fsb_run(
                {"workspace_id": "ws1", "query": "send invoice"}
            )
        assert res["status"] == "ok"
        assert res["matched"] is True


# ── HTTP endpoints ──


class TestFSBHttpEndpoints:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "agent_runtime.workspace_binder._singleton", None
        )
        monkeypatch.setattr(api_server, "_daemon", None)
        monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)
        self.client = self._client()
        yield

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(api_server.app)

    def test_bind_endpoint(self):
        resp = self.client.post(
            "/api/v1/chat/agent/bind",
            json={"workspace_id": "ws1", "agent_id": "a1", "session_id": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["bound"] is True

    def test_unbind_endpoint(self):
        self.client.post(
            "/api/v1/chat/agent/bind",
            json={"workspace_id": "ws1", "agent_id": "a1"},
        )
        resp = self.client.post(
            "/api/v1/chat/agent/unbind",
            json={"workspace_id": "ws1"},
        )
        assert resp.status_code == 200
        assert resp.json()["bound"] is False

    def test_chat_run_disabled(self):
        resp = self.client.post(
            "/api/v1/chat/run",
            json={"workspace_id": "ws1", "query": "hi"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"
        assert data["matched"] is False

    def test_notify_no_binding(self):
        resp = self.client.post(
            "/api/v1/chat/notify",
            json={"workspaceId": "unknown", "run": {"id": "r1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["delivered"] is False
        assert data["reason"] == "no binding"

    def test_notify_no_session(self):
        self.client.post(
            "/api/v1/chat/agent/bind",
            json={"workspace_id": "ws1", "agent_id": "a1"},
        )
        resp = self.client.post(
            "/api/v1/chat/notify",
            json={"workspaceId": "ws1", "run": {"id": "r1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["delivered"] is False
        assert data["reason"] == "no session"


# ── Notify delivers to a live session ──


class TestFSBNotifyDelivery:
    def test_notify_appends_assistant_message(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            "agent_runtime.workspace_binder._singleton", None
        )
        monkeypatch.delenv("FUSION_FSB_ENABLED", raising=False)

        from agent_runtime.chat_engine import ChatEngine
        from agent_runtime.persistence import AgentStore

        store = AgentStore(db_path=str(tmp_path / "s.db"))
        engine = ChatEngine(runtime=None, store=store)
        session = engine.create_session(
            mode="simple", title="t", graph_id="", metadata={}
        )

        daemon = MagicMock()
        daemon._get_chat_engine.return_value = engine

        async def _broadcast(event_type, data):
            pass

        daemon._broadcast_event = _broadcast
        monkeypatch.setattr(api_server, "_daemon", daemon)

        binder = get_workspace_binder()
        binder.bind("ws1", "agentA", session.id)

        client = TestClient(api_server.app)
        resp = client.post(
            "/api/v1/chat/notify",
            json={
                "workspaceId": "ws1",
                "run": {
                    "id": "r99",
                    "status": "completed",
                    "workflow": {"name": "InvoiceFlow"},
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["delivered"] is True

        got = engine.get_session(session.id)
        assert got is not None
        assert any(
            "InvoiceFlow" in str(m.content) and "r99" in str(m.content)
            for m in got.messages
        )
