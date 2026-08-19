"""Issue #170: gateway 路径 chat 失败时直连 fusion-mlx 兜底测试.

根因: gateway (:11432) 路由 cloud glm52 误判 (output_input_ratio_exceeded)
→ Qwen 模型名 400 → gateway 502. daemon _default_client 指 gateway, 全链失败空输出.

修复: LLMGateway 加 _mlx_direct_client, _call_model_async local 路径 gateway
client 失败时直连 fusion-mlx 11434 兜底. daemon _attach_mlx_client gateway
路径注入直连 client.
"""

import pytest

from agent_runtime.llm_gateway import LLMGateway


class _GatewayBoomClient:
    # 模拟 gateway: 鉴权过但转发上游失败 (502).
    base_url = "http://127.0.0.1:11432"
    api_key = "fg-admin-key"

    async def chat(self, **kwargs):
        raise RuntimeError("502 Bad Gateway")


class _MockResp:
    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.tool_calls = []
        self.finish_reason = finish_reason
        self.usage = {}


class _MlxDirectOkClient:
    # 模拟直连 fusion-mlx: 正常返回.
    base_url = "http://127.0.0.1:11434"
    api_key = "dahai168"

    async def chat(self, **kwargs):
        return _MockResp("pong from mlx direct", "stop")


class TestGatewayFallbackDirect:
    @pytest.mark.asyncio
    async def test_gateway_fail_falls_back_to_direct(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        gw.set_default_client(_GatewayBoomClient())
        gw.set_mlx_direct_client(_MlxDirectOkClient())
        gw.register_default_local(name="Qwen3.5-9B-4bit")

        resp = await gw.chat(
            messages=[{"role": "user", "content": "say pong"}], model="default"
        )
        assert resp.finish_reason == "stop"
        assert "pong from mlx direct" in resp.content
        assert resp.fallback_from == "mlx-direct"

    @pytest.mark.asyncio
    async def test_no_direct_client_propagates_error(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        gw.set_default_client(_GatewayBoomClient())
        gw.register_default_local(name="Qwen3.5-9B-4bit")

        resp = await gw.chat(
            messages=[{"role": "user", "content": "say pong"}], model="default"
        )
        assert resp.finish_reason == "error"

    @pytest.mark.asyncio
    async def test_direct_client_also_fails_returns_error(self):
        class _BothBoom:
            base_url = "http://127.0.0.1:11434"
            api_key = "dahai168"

            async def chat(self, **kwargs):
                raise RuntimeError("mlx down")

        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        gw.set_default_client(_GatewayBoomClient())
        gw.set_mlx_direct_client(_BothBoom())
        gw.register_default_local(name="Qwen3.5-9B-4bit")

        resp = await gw.chat(
            messages=[{"role": "user", "content": "say pong"}], model="default"
        )
        assert resp.finish_reason == "error"

    @pytest.mark.asyncio
    async def test_gateway_ok_skips_direct(self):
        class _GatewayOk:
            base_url = "http://127.0.0.1:11432"
            api_key = "fg-admin-key"

            async def chat(self, **kwargs):
                return _MockResp("pong from gateway", "stop")

        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        gw.set_default_client(_GatewayOk())
        gw.set_mlx_direct_client(_MlxDirectOkClient())
        gw.register_default_local(name="Qwen3.5-9B-4bit")

        resp = await gw.chat(
            messages=[{"role": "user", "content": "say pong"}], model="default"
        )
        assert resp.finish_reason == "stop"
        assert "gateway" in resp.content
        assert resp.fallback_from == ""
