"""Issue #541 — long-context prefix-cache: stable prompt head + hit-rate visibility.

fusion-mlx serves a block-aware prefix cache. An agent session only hits it if
the assembled message list keeps a byte-identical head across turns. Two sites
in the LLM node executor used to append volatile per-turn artifact context
DIRECTLY into messages[0], churning the whole prefix every turn.

This test pins:
  1. messages[0] (stable system head) is byte-identical across LLM calls even
     when the artifact context snapshot changes between calls.
  2. Volatile artifact content lands in its own system message AFTER the head,
     never inside messages[0].
  3. Conversation history comes after the volatile system blocks.
  4. Per-call prefix-cache hit rides the llm.call span attributes
     (prefix_cache_hit_tokens / prefix_cache_hit_rate) when the engine reports
     prompt_tokens_details.cached_tokens.
  5. Low hit rate on a long session logs a WARNING (operator signal).
  6. mlx pool state surfaces the fusion-mlx prefix-cache aggregate.
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.telemetry import TelemetryEngine
from server.fusion_mlx_client import LLMResponse
from tools.registry import ToolRegistry


class CapturingClient:
    def __init__(self, usage=None):
        self.calls = []
        self.usage = usage or {}

    async def chat(self, model, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        return LLMResponse(content="ok", tool_calls=[], usage=dict(self.usage))


class VaryingArtifactManager:
    def __init__(self):
        self.n = 0

    def get_active_artifacts_context(self, agent_id, limit=10):
        return ""

    def get_active_artifacts_context_budget_aware(self, agent_id, context_window=32768):
        self.n += 1
        return {
            "context_text": f"VOLATILE-SNAPSHOT-{self.n}",
            "mode": "preview",
            "utilization": 0.5,
            "artifact_count": 1,
        }


def _llm_chain_graph(system_prompt):
    graph = AgentGraph(name="prefix-cache")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "llm1",
        NodeConfig(type="llm", label="LLM1", model="m", system_prompt=system_prompt),
    )
    graph.add_node(
        "llm2",
        NodeConfig(type="llm", label="LLM2", model="m", system_prompt=system_prompt),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm1")
    graph.add_edge("llm1", "llm2")
    graph.add_edge("llm2", "end")
    return graph


async def _run_two_calls(usage=None):
    client = CapturingClient(usage=usage)
    telemetry = TelemetryEngine()
    runtime = AgentRuntime(
        mlx_client=client,
        tool_registry=ToolRegistry(),
        artifact_manager=VaryingArtifactManager(),
        telemetry_engine=telemetry,
    )
    graph = _llm_chain_graph("You are a stable head. RULES-ARE-CONSTANT.")
    input_text = "hello"
    async for _ in runtime.execute_graph(graph, input_text):
        pass
    return client, telemetry


@pytest.mark.asyncio
async def test_stable_head_byte_identical_across_calls():
    client, _ = await _run_two_calls()
    assert len(client.calls) == 2
    head1 = client.calls[0][0]
    head2 = client.calls[1][0]
    assert head1["role"] == "system"
    assert head2["role"] == "system"
    assert head1["content"] == head2["content"]
    assert "RULES-ARE-CONSTANT" in head1["content"]


@pytest.mark.asyncio
async def test_volatile_snapshot_not_in_head():
    client, _ = await _run_two_calls()
    for i, messages in enumerate(client.calls):
        assert f"VOLATILE-SNAPSHOT-{i + 1}" not in messages[0]["content"]
        roles = [m["role"] for m in messages]
        assert "system" in roles[1:], "volatile snapshot must be its own system message"
        joined = "\n".join(m["content"] for m in messages[1:] if m["role"] == "system")
        assert f"VOLATILE-SNAPSHOT-{i + 1}" in joined


@pytest.mark.asyncio
async def test_history_after_volatile_blocks():
    client, _ = await _run_two_calls()
    messages = client.calls[1]
    system_idx = [i for i, m in enumerate(messages) if m["role"] == "system"]
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
    assert user_idx and max(system_idx) < max(user_idx)


@pytest.mark.asyncio
async def test_prefix_cache_hit_rides_span_attributes():
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 4000},
    }
    client, telemetry = await _run_two_calls(usage=usage)
    spans = [s.to_dict() for s in telemetry._spans.values()]
    llm_spans = [s for s in spans if s.get("name") == "llm.call"]
    assert llm_spans, "llm.call spans must be recorded"
    assert all(s["attributes"].get("prefix_cache_hit_tokens") == 4000 for s in llm_spans)
    assert all(
        abs(s["attributes"].get("prefix_cache_hit_rate", 0) - 0.8) < 0.001 for s in llm_spans
    )


@pytest.mark.asyncio
async def test_no_cache_details_no_span_attrs():
    usage = {"prompt_tokens": 5000, "completion_tokens": 10}
    client, telemetry = await _run_two_calls(usage=usage)
    spans = [s.to_dict() for s in telemetry._spans.values() if s.name == "llm.call"]
    assert spans
    assert all("prefix_cache_hit_tokens" not in s["attributes"] for s in spans)


@pytest.mark.asyncio
async def test_low_hit_rate_warning_logged(caplog):
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    with caplog.at_level(logging.WARNING, logger="agent_runtime._runtime_nodes"):
        await _run_two_calls(usage=usage)
    warnings = [r for r in caplog.records if "low prefix-cache hit rate" in r.getMessage()]
    assert warnings, "long session with <50% hit rate must warn"
    assert "prompt ordering contract" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_short_context_no_warning(caplog):
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    with caplog.at_level(logging.WARNING, logger="agent_runtime._runtime_nodes"):
        await _run_two_calls(usage=usage)
    assert not [r for r in caplog.records if "low prefix-cache hit rate" in r.getMessage()]


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    responses: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        return type(self).responses.get(url, _FakeResp(404, {}))


def _seed(monkeypatch, models_payload, stats_payload):
    _FakeAsyncClient.responses = {
        "http://127.0.0.1:11434/admin/api/models": _FakeResp(200, models_payload),
        "http://127.0.0.1:11434/admin/api/stats": _FakeResp(200, stats_payload),
    }
    import httpx as _real_httpx

    monkeypatch.setattr(_real_httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.asyncio
async def test_pool_state_surfaces_prefix_cache(monkeypatch, tmp_path):
    from agent_runtime.daemon_server import DaemonServer

    _seed(
        monkeypatch,
        {"models": [{"id": "qwen-2.5", "loaded": True, "estimated_size": 1}]},
        {
            "active_models": {"model_memory_used": 1, "model_memory_max": 10},
            "prefix_cache": {
                "hits": 30,
                "misses": 10,
                "hit_rate": 0.75,
                "tokens_saved": 48000,
                "latency_p50_ms": 0.4,
                "latency_p99_ms": 2.1,
                "latency_avg_ms": 0.7,
                "latency_samples": 40,
            },
        },
    )
    d = DaemonServer(
        socket_path=str(tmp_path / "t.sock"),
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "t.db"),
    )
    pool = await d._fetch_mlx_pool_state()
    pc = pool["prefix_cache"]
    assert pc["hit_rate"] == 0.75
    assert pc["tokens_saved"] == 48000
    assert pc["latency_p50_ms"] == 0.4
    assert pool["loaded"][0]["partial_tokens_skipped"] == 0


@pytest.mark.asyncio
async def test_pool_state_prefix_cache_none_when_absent(monkeypatch, tmp_path):
    from agent_runtime.daemon_server import DaemonServer

    _seed(
        monkeypatch,
        {"models": []},
        {"active_models": {"model_memory_used": 0, "model_memory_max": 0}},
    )
    d = DaemonServer(
        socket_path=str(tmp_path / "t.sock"),
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "t.db"),
    )
    pool = await d._fetch_mlx_pool_state()
    assert pool["prefix_cache"] is None
