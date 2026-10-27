import asyncio
from types import SimpleNamespace

import pytest

from agent_runtime.compactor import CompactionConfig, Compactor
from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.llm_gateway import GatewayResponse, LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime


def _big_messages(n_rounds=10, chars=200):
    messages = [{"role": "system", "content": "you are a test agent"}]
    for i in range(n_rounds):
        messages.append({"role": "user", "content": f"question {i} " + "x" * chars})
        messages.append({"role": "assistant", "content": f"answer {i} " + "y" * chars})
    return messages


class _StubCompactor:
    def __init__(self, strip_result):
        self._strip_result = strip_result
        self.strip_calls = 0

    def reactive_strip(self, messages):
        self.strip_calls += 1
        return self._strip_result


class TestIssue478GatewayWiring:
    @pytest.mark.asyncio
    async def test_main_runtime_compactor_uses_gateway_llm(self, monkeypatch):
        monkeypatch.setenv("FUSION_COMPACT_MLX", "1")
        gw = LLMGateway()
        rt = AgentRuntime(llm_gateway=gw)
        assert rt.compactor.llm_gateway is rt.llm_gateway

        calls = []

        async def fake_chat(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="bullet summary of prior context")

        monkeypatch.setattr(gw, "chat", fake_chat)
        # Shrink the window so the fixture corpus trips the warning threshold
        # (default 32k window never compacts a 1k-token fixture).
        rt.compactor.config = CompactionConfig(context_window=4000)
        out = await rt.compactor.compact_async(_big_messages(), level="warning")
        assert calls, "compaction-triggered summarize call must hit gateway LLM"
        assert len(out) < len(_big_messages())
        assert "bullet summary" in out[0]["content"] or any(
            "bullet summary" in m["content"] for m in out
        )


class TestIssue478GenerationRetryGate:
    def _gateway_with_model(self, monkeypatch, first_exc, strip_result):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="m478", context_length=64))
        stub = _StubCompactor(strip_result)
        gw.set_compactor(stub)
        attempts = []

        async def fake_call(config, messages, **kwargs):
            attempts.append(list(messages))
            if len(attempts) == 1:
                raise first_exc
            return GatewayResponse(content="retried-ok", model=config.name)

        monkeypatch.setattr(gw, "_call_model_async", fake_call)
        return gw, stub, attempts

    @pytest.mark.asyncio
    async def test_noop_strip_blocks_retry_original_error_stays(self, monkeypatch):
        messages = _big_messages(n_rounds=2)
        gw, stub, attempts = self._gateway_with_model(
            monkeypatch,
            RuntimeError("maximum context length is 4096 tokens"),
            strip_result=list(messages),
        )
        result = await gw.chat(messages, model="m478")
        assert stub.strip_calls == 1
        assert len(attempts) == 1, "no-op strip must not trigger a doomed retry"
        assert result.finish_reason == "error"
        assert "maximum context length" in result.usage.get("error", "")
        assert gw._compact_generation == 0

    @pytest.mark.asyncio
    async def test_real_strip_advances_generation_and_retries(self, monkeypatch):
        messages = _big_messages(n_rounds=2)
        pruned = [{"role": "system", "content": "[Compacted]"}] + messages[-2:]
        gw, stub, attempts = self._gateway_with_model(
            monkeypatch,
            RuntimeError("maximum context length is 4096 tokens"),
            strip_result=pruned,
        )
        result = await gw.chat(messages, model="m478")
        assert len(attempts) == 2
        assert attempts[1] == pruned
        assert result.content == "retried-ok"
        assert gw._compact_generation == 1


class TestIssue478CompactAfterHook:
    def _compactor_with_recorder(self):
        comp = Compactor(config=CompactionConfig(context_window=4000))
        comp.hooks = HookEngine(auto_register_decorators=False)
        seen = []

        def recorder(payload):
            seen.append(payload)

        comp.hooks.register(HookConfig(event="COMPACT_AFTER", callback=recorder))
        return comp, seen

    @pytest.mark.asyncio
    async def test_compact_after_fires_on_success(self):
        comp, seen = self._compactor_with_recorder()
        out = comp.compact(_big_messages(), level="warning")
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(out) < len(_big_messages())
        assert seen and seen[-1]["success"] is True
        assert seen[-1]["after_msgs"] == len(out)

    @pytest.mark.asyncio
    async def test_compact_after_fires_on_failure(self, monkeypatch):
        comp, seen = self._compactor_with_recorder()

        def boom(messages, summarize=None):
            raise RuntimeError("summarizer exploded")

        monkeypatch.setattr(comp, "_smart_truncate", boom)
        with pytest.raises(RuntimeError, match="summarizer exploded"):
            comp.compact(_big_messages(), level="warning")
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen and seen[-1]["success"] is False
        assert "summarizer exploded" in seen[-1]["error"]

    @pytest.mark.asyncio
    async def test_compact_async_fires_after_hook(self):
        comp, seen = self._compactor_with_recorder()
        await comp.compact_async(_big_messages(), level="warning")
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen and seen[-1]["success"] is True
