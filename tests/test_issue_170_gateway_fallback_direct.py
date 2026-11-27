"""Issue #470: no more silent direct fusion-mlx fallback on gateway-path chat failure.

History: #170 once added _mlx_direct_client — when the gateway (:11432) failed to
forward upstream (502), the client fell back to fusion-mlx 11434 directly. After
fusion-gateway#301 landed, the gateway itself owns a local-to-cloud overflow
policy (bounded + audited); a silent client-side cloud-to-local retry bypasses
routing policy/audit/cost caps, so #470 removed that fallback and gateway errors
propagate to the caller.

The original #170 empty-output scenario is now handled by the gateway-side
overflow (see the "Local-first cloud overflow" section of the fusion-gateway
README).
"""

import pytest

from agent_runtime.llm_gateway import LLMGateway


class _GatewayBoomClient:
    # Simulated gateway: auth passes but upstream forwarding fails (502).
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
    # Simulated direct fusion-mlx client: returns normally. After #470 injection is a
    # no-op, never called.
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

        resp = await gw.chat(messages=[{"role": "user", "content": "say pong"}], model="default")
        # #470: gateway errors propagate to the caller, no silent cloud-to-local retry.
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

        resp = await gw.chat(messages=[{"role": "user", "content": "say pong"}], model="default")
        assert resp.finish_reason == "stop"
        assert "gateway" in resp.content
        assert resp.fallback_from == ""
