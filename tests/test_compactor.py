from __future__ import annotations


from agent_runtime.compactor import Compactor, CompactionConfig
from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry


class BigTool(BaseTool):
    name = "big_tool"
    description = "returns a large result"
    parameters = {"input": {"type": "string", "description": "in"}}

    async def execute(self, **kwargs) -> str:
        return "X" * 5000


class MockMLXClient:
    def __init__(self):
        self.call_count = 0
        self.responses = []

    def add_response(self, content, tool_calls=None):
        self.responses.append({"content": content, "tool_calls": tool_calls or []})

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse
        if not self.responses:
            return LLMResponse(content="done", tool_calls=[])
        resp = self.responses.pop(0)
        return LLMResponse(content=resp["content"], tool_calls=resp["tool_calls"])


def _msgs():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "yo"},
    ]


def test_estimate_tokens_and_thresholds():
    c = Compactor(CompactionConfig(context_window=100, warning_buffer=30, error_buffer=50, manual_buffer=10))
    assert c.estimate_tokens([{"role": "user", "content": "a" * 400}]) == 100
    assert c.should_compact([{"role": "user", "content": "a" * 40}]) == "none"
    assert c.should_compact([{"role": "user", "content": "a" * 400}]) == "error"


def test_microcompact_truncates_tool_results():
    c = Compactor(CompactionConfig(tool_result_head=10, tool_result_tail=10))
    big = {"role": "tool", "content": "A" * 500, "tool_call_id": "t1"}
    out = c._microcompact([big])
    assert "truncated" in out[0]["content"]
    assert len(out[0]["content"]) < 500


def test_smart_truncate_keeps_recent_rounds():
    c = Compactor(CompactionConfig(keep_recent_rounds=1))
    out = c._smart_truncate(_msgs())
    assert any(m.get("role") == "system" and "Compacted" in m.get("content", "") for m in out)
    assert out[-1]["content"] == "yo"


def test_hard_compact_keeps_only_last_round():
    c = Compactor(CompactionConfig(keep_recent_rounds=1))
    out = c._hard_compact(_msgs())
    contents = [m.get("content", "") for m in out]
    assert any("hard-compact" in x for x in contents)
    assert out[-1]["content"] == "yo"


def test_compact_pipeline_reduces_tokens():
    c = Compactor(CompactionConfig(
        context_window=20, warning_buffer=0, error_buffer=0, keep_recent_rounds=1,
    ))
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "U" * 200},
        {"role": "assistant", "content": "A" * 200},
        {"role": "user", "content": "U2" * 200},
        {"role": "assistant", "content": "final"},
    ]
    before = c.estimate_tokens(msgs)
    out = c.compact(msgs, level="error")
    after = c.estimate_tokens(out)
    assert after < before
    assert out[-1]["content"] == "final"


def test_reactive_strip_drops_oldest():
    c = Compactor()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "old2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "keep1"},
        {"role": "assistant", "content": "keep2"},
    ]
    out = c.reactive_strip(msgs)
    assert out[0]["content"] == "sys"
    assert out[-1]["content"] == "keep2"
    assert any("Compacted" in m.get("content", "") for m in out)


async def test_agent_loop_applies_compaction():
    registry = ToolRegistry()
    registry.register(BigTool())
    client = MockMLXClient()
    client.add_response("check", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "big_tool", "arguments": '{"input": "x"}'},
    }])
    client.add_response("done")

    graph = AgentGraph(name="Compact Loop")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(
        type="llm", label="LLM", model="m",
        loop_mode="agent", max_loop_iterations=5,
    ))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    runtime.compactor = Compactor(CompactionConfig(
        context_window=200, warning_buffer=100, error_buffer=50,
        manual_buffer=10, keep_recent_rounds=1,
        tool_result_head=20, tool_result_tail=20,
    ))
    events = []
    async for event in runtime.execute_graph(graph, "go"):
        events.append(event)

    assert client.call_count == 2
    assert any(e.type == AgentEventType.END for e in events)


class _FakeMemoryEngine:
    def __init__(self):
        self.summaries = []

    def store_summary(self, summary, scope, original_count):
        self.summaries.append({
            "summary": summary, "scope": scope, "original_count": original_count,
        })
        return "fake-id"


class TestCompactorPersistence:
    def test_reactive_strip_persists_summary(self):
        me = _FakeMemoryEngine()
        c = Compactor(memory_engine=me)
        c.reactive_strip(_msgs())
        assert len(me.summaries) == 1
        assert me.summaries[0]["scope"] == "compaction"
        assert me.summaries[0]["original_count"] > 0

    def test_hard_compact_persists_summary(self):
        me = _FakeMemoryEngine()
        cfg = CompactionConfig(
            context_window=100, warning_buffer=10, error_buffer=5,
            manual_buffer=20, keep_recent_rounds=1,
        )
        c = Compactor(config=cfg, memory_engine=me)
        big = "word " * 120
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
            {"role": "user", "content": big},
            {"role": "assistant", "content": big},
        ]
        c.compact(msgs, level="error")
        assert len(me.summaries) >= 1
        assert all(s["scope"] == "compaction" for s in me.summaries)

    def test_no_memory_engine_is_noop(self):
        c = Compactor()
        c.reactive_strip(_msgs())
        assert c.memory_engine is None


class TestGatewayReactive413:
    async def test_reactive_retry_on_context_too_long(self):
        from agent_runtime.llm_gateway import LLMGateway, GatewayResponse, ModelConfig
        gw = LLMGateway(compactor=Compactor())
        gw.register_model(ModelConfig(name="m1", priority=1))
        calls = {"n": 0}

        async def fake_call(config, messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("This model's maximum context length is 8192 tokens")
            return GatewayResponse(content="ok", model=config.name, finish_reason="stop")

        gw._call_model_async = fake_call
        resp = await gw.chat([{"role": "user", "content": "hi"}], model="m1")
        assert calls["n"] == 2
        assert resp.finish_reason == "stop"

    async def test_non_context_error_no_retry(self):
        from agent_runtime.llm_gateway import LLMGateway, ModelConfig
        gw = LLMGateway(compactor=Compactor())
        gw.register_model(ModelConfig(name="m1", priority=1))
        calls = {"n": 0}

        async def fake_call(config, messages, **kwargs):
            calls["n"] += 1
            raise Exception("connection refused")

        gw._call_model_async = fake_call
        resp = await gw.chat([{"role": "user", "content": "hi"}], model="m1")
        assert calls["n"] == 1
        assert resp.finish_reason == "error"
