"""Issue #168: get_fallback_chain 死循环刷盘修复测试.

根因: route() fallback 分支不感知 exclude, 当 _models 空 + _default_model
非空时, get_fallback_chain 的 while True 永不 break → 无限循环刷
"No registered models" 日志 (实测 86GB / CPU 100%).

触发链: daemon _attach_mlx_client 时 _discover_mlx_model_id 失败 → _models 空
+ _default_model 残留 → skill.execute → chat() 主调用失败 →
get_fallback_chain 死循环.
"""

import time

import pytest

from agent_runtime.llm_gateway import LLMGateway


class TestFallbackChainNoInfiniteLoop:
    def test_empty_models_with_default_does_not_loop(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        assert len(gw._models) == 0
        assert gw._default_model == "Qwen3.5-9B-4bit"
        t0 = time.time()
        chain = gw.get_fallback_chain()
        elapsed = time.time() - t0
        assert elapsed < 1.0, f"get_fallback_chain took {elapsed:.3f}s, possible loop"
        assert len(chain) <= 2
        if chain:
            assert chain[0].name == "Qwen3.5-9B-4bit"

    def test_route_exclude_default_returns_none(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        result = gw.route(exclude={"Qwen3.5-9B-4bit"})
        assert result is None

    def test_route_no_exclude_returns_default(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        result = gw.route()
        assert result is not None
        assert result.name == "Qwen3.5-9B-4bit"

    def test_registered_models_chain_bounded(self):
        gw = LLMGateway(default_model="X")
        gw.register_default_local(name="Qwen3.5-9B-4bit")
        chain = gw.get_fallback_chain()
        assert len(chain) >= 1
        assert len(chain) <= len(gw._models) + 1


class TestSkillExecuteNoFlood:
    """集成级: skill.execute 走 chat() 失败时不死循环刷盘."""

    @pytest.mark.asyncio
    async def test_chat_failure_returns_error_not_loop(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")

        class _BoomClient:
            base_url = "http://localhost:11434"
            api_key = "test-key"

            async def chat(self, **kwargs):
                raise RuntimeError("mlx 503")

        gw.set_default_client(_BoomClient())
        t0 = time.time()
        resp = await gw.chat(messages=[{"role": "user", "content": "hi"}], model="default")
        elapsed = time.time() - t0
        assert elapsed < 2.0, f"chat() took {elapsed:.3f}s on failure, possible loop"
        assert resp.finish_reason == "error" or resp.content == ""
