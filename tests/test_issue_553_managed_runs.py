"""#553: managed SSE runs — run_started frame, seq frames, resume without
duplicates, cancel endpoint, registry reap/cap."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_runtime import api_server
from agent_runtime.api_server import app
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.run_registry import MAX_RUNS, ManagedRun, RunRegistry
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import StreamChunk
from tools.registry import ToolRegistry


class MockStreamClient:
    async def chat_stream(self, messages, model="", **kwargs):
        for chunk in [
            StreamChunk(delta_content="Hel", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="lo", delta_tool_calls=[], finish_reason=None),
            StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop"),
        ]:
            yield chunk

    async def chat(self, model, messages, tools=None, **kwargs):
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="Hello",
            tool_calls=[],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            finish_reason="stop",
        )

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _make_runtime():
    gateway = LLMGateway()
    gateway.set_default_client(MockStreamClient())
    gateway.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    return AgentRuntime(llm_gateway=gateway, tool_registry=ToolRegistry())


def _make_graph():
    g = AgentGraph(name="issue553", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("llm1", NodeConfig(type="llm", label="llm1", model="test-model"))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "end")
    return g


@pytest.fixture
def patched_api(tmp_path, monkeypatch):
    store = AgentStore(db_path=tmp_path / "store.db")
    graph = _make_graph()
    store.save_graph(graph)
    monkeypatch.setattr(api_server, "_store", store)
    monkeypatch.setattr(api_server, "_runtime", _make_runtime())
    monkeypatch.setattr(api_server, "_daemon", None)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    # fresh registry per test
    from agent_runtime.run_registry import RunRegistry

    monkeypatch.setattr(app.state, "run_registry", RunRegistry(), raising=False)
    return {"graph": graph}


def _parse_sse(body: str) -> list[dict]:
    lines = [l[len("data: ") :] for l in body.splitlines() if l.startswith("data: ")]
    return [json.loads(l) for l in lines]


class TestSSEManagedRun:

    def test_stream_yields_run_started_and_seq_frames(self, patched_api):
        client = TestClient(app)
        graph = patched_api["graph"]
        with client.stream(
            "GET", f"/v1/graphs/{graph.id}/execute/stream?input=hi"
        ) as resp:
            assert resp.status_code == 200
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
        frames = _parse_sse(body.decode("utf-8"))
        assert frames[0]["type"] == "run_started"
        assert frames[0]["run_id"].startswith("run-")
        seqs = [f["seq"] for f in frames if "seq" in f]
        assert seqs == sorted(seqs), "seq must be monotonically increasing"
        assert frames[-1]["type"] == "done"
        run = app.state.run_registry.get(frames[0]["run_id"])
        assert run is not None
        assert run.done is True

    def test_resume_replays_only_frames_past_last_seq(self, patched_api):
        client = TestClient(app)
        graph = patched_api["graph"]
        with client.stream(
            "GET", f"/v1/graphs/{graph.id}/execute/stream?input=hi"
        ) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
        frames = _parse_sse(body.decode("utf-8"))
        run_id = frames[0]["run_id"]

        # reconnect having last seen seq 1 — expect only seq >= 2, replay marked
        with client.stream(
            "GET",
            f"/v1/graphs/{graph.id}/execute/stream?input=hi&resume_run_id={run_id}&last_seq=1",
        ) as resp:
            body2 = b""
            for chunk in resp.iter_bytes():
                body2 += chunk
        frames2 = _parse_sse(body2.decode("utf-8"))
        assert frames2, "resume stream must yield frames"
        got_seqs = [f["seq"] for f in frames2 if "seq" in f]
        assert all(s > 1 for s in got_seqs), f"resume leaked old frames: {got_seqs}"
        assert all(f.get("replay") is True for f in frames2 if "seq" in f)
        assert frames2[-1]["type"] == "done"

    def test_resume_past_done_terminates(self, patched_api):
        client = TestClient(app)
        graph = patched_api["graph"]
        with client.stream(
            "GET", f"/v1/graphs/{graph.id}/execute/stream?input=hi"
        ) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
        frames = _parse_sse(body.decode("utf-8"))
        run_id = frames[0]["run_id"]
        last = frames[-1]["seq"]
        # last_seq already at the done frame — must end promptly, no hang
        with client.stream(
            "GET",
            f"/v1/graphs/{graph.id}/execute/stream?input=hi&resume_run_id={run_id}&last_seq={last}",
        ) as resp:
            body2 = b""
            for chunk in resp.iter_bytes():
                body2 += chunk
        assert _parse_sse(body2.decode("utf-8")) == []

    def test_resume_unknown_run_404(self, patched_api):
        client = TestClient(app)
        resp = client.get(
            "/v1/graphs/g1/execute/stream?input=hi&resume_run_id=run-nope"
        )
        assert resp.status_code == 404

    def test_cancel_endpoint(self, patched_api):
        client = TestClient(app)
        graph = patched_api["graph"]
        registry = app.state.run_registry
        run = registry.create(graph.id)
        # pretend the background task exists and is running
        run.done = False
        registry._runs[run.run_id] = run
        resp = client.post(
            f"/v1/graphs/{graph.id}/execute/stream/{run.run_id}/cancel"
        )
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        assert run.cancelled is True
        # unknown run
        resp2 = client.post(
            f"/v1/graphs/{graph.id}/execute/stream/run-missing/cancel"
        )
        assert resp2.status_code == 404

    def test_only_node_runs_through_managed_path(self, patched_api):
        client = TestClient(app)
        graph = patched_api["graph"]
        with client.stream(
            "GET",
            f"/v1/graphs/{graph.id}/execute/stream?input=hi&only_node=llm1",
        ) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
        frames = _parse_sse(body.decode("utf-8"))
        assert frames[0]["type"] == "run_started"
        node_ids = {f.get("node_id") for f in frames}
        assert "llm1" in node_ids
        assert "end" not in node_ids


class TestRunRegistry:

    def test_publish_assigns_seq_and_caps_buffer(self):
        from agent_runtime.run_registry import MAX_BUFFER

        run = ManagedRun("g")
        for i in range(MAX_BUFFER + 10):
            run.publish({"type": "x", "i": i})
        assert len(run.buffer) == MAX_BUFFER
        assert run.buffer[-1]["seq"] == MAX_BUFFER + 9

    def test_reap_ttl_and_hard_cap(self):
        registry = RunRegistry()
        old = registry.create("g")
        old.done = True
        old.created_at = time.time() - 10_000
        registry.reap()
        assert registry.get(old.run_id) is None
        for _ in range(MAX_RUNS + 5):
            r = registry.create("g")
            r.done = True
        registry.reap()
        assert len(registry._runs) <= MAX_RUNS

    def test_cancel_sets_flag_and_cancels_task(self):
        registry = RunRegistry()
        run = registry.create("g")

        async def _spin():
            await asyncio.sleep(60)

        loop = asyncio.new_event_loop()
        try:
            run.task = loop.create_task(_spin())
            loop.run_until_complete(asyncio.sleep(0))
            assert registry.cancel(run.run_id) is True
            assert run.cancelled is True
            loop.run_until_complete(asyncio.sleep(0))
            assert run.task.cancelled()
            assert registry.cancel("run-missing") is False
        finally:
            loop.close()

    def test_cancel_finished_run_is_safe(self):
        registry = RunRegistry()
        run = registry.create("g")
        run.done = True
        assert registry.cancel(run.run_id) is True
        assert run.cancelled is True
