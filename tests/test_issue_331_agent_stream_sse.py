"""#331: per-token SSE for agent execution — GET /v1/agents/{id}/execute/stream.

Contract:
- 200 text/event-stream; frames are `data: {json}\\n\\n` AgentEvent dicts;
  terminal frame is {"type": "done"}; token events present (true streaming).
- Reuses build_agent_adhoc_graph (extracted from agent.execute_stream) —
  same ad-hoc graph shape as the RPC path.
- Auth: ?api_key= or Bearer accepted; 401 when key configured and absent.
- Unknown agent -> 404 AGENT_NOT_FOUND. No daemon -> 500/503.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime.api_server import app
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse, StreamChunk
from tools.registry import ToolRegistry

STATIC_KEY = "fk-static-test-331"


class MockStreamClient:
    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in [
            StreamChunk(delta_content="Hel", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="lo", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop"),
        ]:
            yield chunk

    async def chat(self, model, messages, tools=None, **kwargs):
        return LLMResponse(
            content="Hello",
            tool_calls=[],
            usage={"prompt_tokens": 5, "completion_tokens": 2},
            finish_reason="stop",
        )

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _make_runtime():
    gateway = LLMGateway()
    gateway.set_default_client(MockStreamClient())
    gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    return AgentRuntime(llm_gateway=gateway, tool_registry=ToolRegistry())


class FakeDaemon:
    def __init__(self, agent_dir, rt, store):
        self._dir = agent_dir
        self._rt = rt
        self.store = store

    def _agent_dir(self, agent_id):
        return self._dir

    def _get_runtime(self):
        return self._rt

    def _inject_knowledge_context(self, *a, **k):
        async def _nope(*a, **k):
            return ""
        return _nope()

    def _get_style_manager(self):
        class _SM:
            def apply(self, prompt, style):
                return {"system_prompt": prompt}
        return _SM()

    def register_execution(self, scope, key_id):
        return f"{scope}:{key_id}", lambda: None

    @property
    def graph_semaphore(self):
        return None

    @property
    def token_budget_ref(self):
        return None


def _make_agent_dir(tmp_path, name="sse-agent"):
    agent_dir = tmp_path / name
    pkg_dir = agent_dir / ".fusion-agent"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "manifest.json").write_text(json.dumps({
        "name": name,
        "system_prompt": "you are helpful",
        "model": "test-model",
        "tools": [],
    }))
    return agent_dir


@pytest.fixture
def static_key_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", STATIC_KEY)
    fake_home = tmp_path / "home"
    (fake_home / ".fusion-agent-studio").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return STATIC_KEY


@pytest.fixture
def sse_daemon(static_key_env, tmp_path):
    from agent_runtime import api_server

    rt = _make_runtime()
    store = AgentStore(db_path=tmp_path / "store.db")
    daemon = FakeDaemon(_make_agent_dir(tmp_path), rt, store)
    api_server.set_daemon(daemon)
    yield daemon
    api_server.set_daemon(None)


def _parse_sse(body: str):
    frames = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
    return frames


class TestAgentStreamSSE:

    def test_stream_ok_token_and_done(self, sse_daemon):
        client = TestClient(app)
        with client.stream(
            "GET",
            "/v1/agents/sse-agent/execute/stream",
            params={"input": "hi", "api_key": STATIC_KEY},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(chunk for chunk in resp.iter_text())
        frames = _parse_sse(body)
        types = [f.get("type") for f in frames]
        assert "token" in types, f"no token frame: {types}"
        assert frames[-1].get("type") == "done"

    def test_stream_bearer_auth(self, sse_daemon):
        client = TestClient(app)
        resp = client.get(
            "/v1/agents/sse-agent/execute/stream",
            params={"input": "hi"},
            headers={"Authorization": f"Bearer {STATIC_KEY}"},
        )
        assert resp.status_code == 200

    def test_stream_no_auth_401(self, sse_daemon):
        client = TestClient(app)
        resp = client.get(
            "/v1/agents/sse-agent/execute/stream",
            params={"input": "hi"},
        )
        assert resp.status_code == 401

    def test_stream_wrong_key_401(self, sse_daemon):
        client = TestClient(app)
        resp = client.get(
            "/v1/agents/sse-agent/execute/stream",
            params={"input": "hi", "api_key": "wrong"},
        )
        assert resp.status_code == 401

    def test_stream_unknown_agent(self, sse_daemon, tmp_path):
        from agent_runtime import api_server

        daemon = FakeDaemon(
            tmp_path / "no-such-agent", sse_daemon._rt, sse_daemon.store
        )
        api_server.set_daemon(daemon)
        try:
            client = TestClient(app)
            resp = client.get(
                "/v1/agents/ghost/execute/stream",
                params={"input": "hi", "api_key": STATIC_KEY},
            )
            assert resp.status_code == 404
        finally:
            api_server.set_daemon(sse_daemon)

    def test_stream_no_daemon(self, static_key_env):
        from agent_runtime import api_server

        api_server.set_daemon(None)
        client = TestClient(app)
        resp = client.get(
            "/v1/agents/sse-agent/execute/stream",
            params={"input": "hi", "api_key": STATIC_KEY},
        )
        assert resp.status_code in (500, 503)
