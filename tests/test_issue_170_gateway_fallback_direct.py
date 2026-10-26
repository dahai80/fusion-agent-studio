"""Issue #470: gateway 路径 chat 失败时不再直连 fusion-mlx 静默兜底.

历史: #170 曾加 _mlx_direct_client — gateway (:11432) 转发上游失败 (502) 时
客户端直连 fusion-mlx 11434 兜底. fusion-gateway#301 落地后 gateway 自身拥有
local→cloud overflow 策略 (有界+审计), 客户端静默 cloud→local 重试绕过
路由策略/审计/成本上限, 故 #470 删除该兜底, gateway 错误直接上抛.

原 #170 空输出场景由 gateway 侧 overflow 承接 (见 fusion-gateway README
"Local-first cloud overflow" 节).
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
    # 模拟直连 fusion-mlx: 正常返回. #470 后注入是 no-op, 永不被调用.
    base_url = "http://127.0.0.1:11434"
    api_key = "dahai168"

    async def chat(self, **kwargs):
        return _MockResp("pong from mlx direct", "stop")


class TestGatewayErrorsSurface:
    @pytest.mark.asyncio
    async def test_gateway_fail_surfaces_error_no_direct_retry(self):
        gw = LLMGateway(default_model="Qwen3.5-9B-4bit")
        gw.set_default_client(_GatewayBoomClient())
        gw.set_mlx_direct_client(_MlxDirectOkClient())  # no-op since #470
        gw.register_default_local(name="Qwen3.5-9B-4bit")

        resp = await gw.chat(
            messages=[{"role": "user", "content": "say pong"}], model="default"
        )
        # #470: gateway 错误上抛给调用方, 无 cloud→local 静默重试.
        assert resp.finish_reason == "error"
        assert "502" in str(resp.usage.get("error", ""))
        assert resp.fallback_from == ""

    @pytest.mark.asyncio
    async def test_gateway_ok_unchanged(self):
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
