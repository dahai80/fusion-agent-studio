"""#330: allowlisted RPC-over-HTTP bridge (POST /v1/rpc) for the web module.

Contract:
- Allowlisted methods dispatch through daemon._get_handler (same as /v1/runs).
- Non-allowlisted and unknown methods are rejected 400 BEFORE handler
  resolution — the daemon is never consulted for them.
- Auth posture matches other mutating endpoints: 401 without key when
  FUSION_AGENT_STUDIO_API_KEY is set; x-api-key and Authorization: Bearer both
  accepted (the gateway /shell proxy injects Bearer).
- Dispatcher handlers return {"status": "error", ...} dicts with HTTP 200 —
  the bridge passes them through verbatim (web client checks .status).
- build_agent_adhoc_graph extraction (from agent.execute_stream) preserves
  the ad-hoc start→llm→tools graph shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

STATIC_KEY = "fk-static-test-330"


class _StubDaemon:
    def __init__(self):
        self.calls = []

    def _get_handler(self, method):
        known = {
            "memory.recall": self._handle_recall,
            "memory.store": self._handle_store,
            "agent.list": self._handle_agent_list,
            "daemon.shutdown": self._handle_shutdown,
            "chat.stream": self._handle_shutdown,
        }
        return known.get(method)

    async def _handle_recall(self, params):
        self.calls.append("memory.recall")
        return {"results": [{"content": "hello-330"}], "count": 1}

    async def _handle_store(self, params):
        self.calls.append("memory.store")
        return {"status": "error", "message": "store exploded"}

    async def _handle_agent_list(self, params):
        self.calls.append("agent.list")
        return {"agents": [], "total": 0}

    async def _handle_shutdown(self, params):
        self.calls.append("FORBIDDEN")
        return {"status": "ok"}


@pytest.fixture
def static_key_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", STATIC_KEY)
    fake_home = tmp_path / "home"
    (fake_home / ".fusion-agent-studio").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return STATIC_KEY


@pytest.fixture
def stub_daemon():
    from agent_runtime import api_server

    daemon = _StubDaemon()
    api_server.set_daemon(daemon)
    yield daemon
    api_server.set_daemon(None)


def _client():
    from fastapi.testclient import TestClient

    from agent_runtime.api_server import app

    return TestClient(app)


class TestRpcBridge:
    def test_allowlisted_method_dispatches(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "memory.recall", "params": {"query": "hello"}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["content"] == "hello-330"
        assert stub_daemon.calls == ["memory.recall"]

    def test_non_allowlisted_method_rejected(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "daemon.shutdown", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]["error"]["message"]
        # Handler must never be reached for rejected methods.
        assert stub_daemon.calls == []

    def test_ws_only_stream_method_rejected(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "chat.stream", "params": {}},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert resp.status_code == 400
        assert stub_daemon.calls == []

    def test_unknown_method_rejected(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "totally.bogus", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 400

    def test_missing_method_400(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc", json={"params": {}}, headers={"x-api-key": STATIC_KEY}
        )
        assert resp.status_code == 400

    def test_bearer_auth_accepted(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "agent.list", "params": {}},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_no_key_401(self, static_key_env, stub_daemon):
        resp = _client().post("/v1/rpc", json={"method": "agent.list", "params": {}})
        assert resp.status_code == 401

    def test_wrong_key_401(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "agent.list", "params": {}},
            headers={"x-api-key": "fk-wrong"},
        )
        assert resp.status_code == 401

    def test_dispatcher_error_passthrough_http_200(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "memory.store", "params": {"content": "x"}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["message"] == "store exploded"

    def test_non_dict_params_defaults_empty(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "agent.list", "params": "not-a-dict"},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200

    def test_no_daemon_error(self, static_key_env):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "agent.list", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        # Same posture as /v1/runs without a daemon: 5xx error envelope
        # (INTERNAL_ERROR maps to 500 via raise_api_error's status map).
        assert resp.status_code in (500, 503), resp.text


class TestAdhocGraphExtraction:
    async def test_build_agent_adhoc_graph_shape(self, tmp_path):
        from agent_runtime.dispatchers.agent import build_agent_adhoc_graph

        agent_dir = tmp_path / "agent-330"
        pkg_dir = agent_dir / ".fusion-agent"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "name": "shape-agent",
                    "system_prompt": "you are helpful",
                    "model": "test-model",
                    "tools": ["alpha", "beta"],
                }
            )
        )

        class FakeDaemon:
            def _agent_dir(self, agent_id):
                return agent_dir

            async def _inject_knowledge_context(self, *a, **k):
                return ""

            def _get_style_manager(self):
                class _SM:
                    def apply(self, prompt, style):
                        return {"system_prompt": prompt}

                return _SM()

        graph, manifest = await build_agent_adhoc_graph(FakeDaemon(), "agent-330", "hi")
        assert graph is not None and manifest is not None
        nodes = set(graph.nodes.keys())
        assert nodes == {"start", "llm-1", "tool-1", "tool-2"}
        edges = {(e.source_id, e.target_id) for e in graph.edges}
        assert ("start", "llm-1") in edges
        assert ("llm-1", "tool-1") in edges
        assert ("tool-1", "tool-2") in edges
        llm = graph.nodes["llm-1"]
        assert llm.model == "test-model"
        assert llm.system_prompt == "you are helpful"

    async def test_build_agent_adhoc_graph_missing_agent(self, tmp_path):
        from agent_runtime.dispatchers.agent import build_agent_adhoc_graph

        class FakeDaemon:
            def _agent_dir(self, agent_id):
                return tmp_path / "missing-agent"

            async def _inject_knowledge_context(self, *a, **k):
                return ""

            def _get_style_manager(self):
                class _SM:
                    def apply(self, prompt, style):
                        return {"system_prompt": prompt}

                return _SM()

        graph, manifest = await build_agent_adhoc_graph(FakeDaemon(), "nope", "hi")
        assert graph is None and manifest is None
