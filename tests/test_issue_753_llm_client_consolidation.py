"""Issue #753: consolidate intra-repo LLM client duplication.

llm_gateway.LLMGateway is the routing layer (fallback chain, circuit
breaker, stats, semaphore); server.fusion_mlx_client.FusionMLXClient is
the single HTTP transport (deadline, retry, streaming). These tests pin
the consolidation contract:

- default endpoint resolved from one source (server.fusion_mlx_client.default_base_url)
- gateway delegates all HTTP to shared cached FusionMLXClient transports
- legacy sync execute() is a thin bridge onto chat() (old _call_model sync
  path removed; it never awaited the async client call, so every real call
  degraded to the caller's stub fallback)
"""

import pytest

import agent_runtime.llm_gateway as llm_gateway_module
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.memory_engine import MemoryEngine
from server.fusion_mlx_client import FusionMLXClient, default_base_url


class _MockResp:
    def __init__(self, content="pong"):
        self.content = content
        self.tool_calls = []
        self.finish_reason = "stop"
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1}


class _OkClient:
    base_url = "http://127.0.0.1:11432/v1"
    api_key = "k"

    async def chat(self, **kwargs):
        return _MockResp("pong from mock client")


class TestDefaultBaseUrlSingleSource:
    def test_env_gateway_url_wins(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_URL", "http://gw.example:11432/v1")
        assert default_base_url() == "http://gw.example:11432/v1"

    def test_env_mlx_port_fallback(self, monkeypatch):
        monkeypatch.delenv("FUSION_GATEWAY_URL", raising=False)
        monkeypatch.setenv("FUSION_MLX_PORT", "11500")
        assert default_base_url() == "http://localhost:11500/v1"

    def test_client_default_matches_helper(self, monkeypatch):
        monkeypatch.delenv("FUSION_GATEWAY_URL", raising=False)
        client = FusionMLXClient()
        assert client.base_url == default_base_url().rstrip("/")

    def test_gateway_default_url_single_source(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_URL", "http://gw.example:11432/v1")
        import importlib

        mod = importlib.reload(llm_gateway_module)
        try:
            assert mod.DEFAULT_LOCAL_BASE_URL == "http://gw.example:11432/v1"
        finally:
            importlib.reload(llm_gateway_module)


class TestSharedTransports:
    def test_same_endpoint_reuses_client(self):
        gw = LLMGateway()
        c1 = gw._get_transport("http://a:11432/v1", "key1")
        c2 = gw._get_transport("http://a:11432/v1", "key1")
        assert c1 is c2
        assert isinstance(c1, FusionMLXClient)

    def test_different_endpoint_gets_own_client(self):
        gw = LLMGateway()
        c1 = gw._get_transport("http://a:11432/v1", "key1")
        c2 = gw._get_transport("http://b:11432/v1", "key1")
        assert c1 is not c2

    @pytest.mark.asyncio
    async def test_remote_model_chat_uses_cached_transport(self):
        gw = LLMGateway()
        gw.register_model(
            llm_gateway_module.ModelConfig(
                name="remote-1",
                provider="openai",
                base_url="http://remote:8080/v1",
                api_key="rk",
            )
        )
        created = []

        class _RemoteClient:
            async def chat(self, **kwargs):
                return _MockResp("remote pong")

        class _Factory:
            def __call__(self, base_url, api_key):
                created.append((base_url, api_key))
                return _RemoteClient()

        gw._get_transport = _Factory()
        resp = await gw.chat(messages=[{"role": "user", "content": "hi"}], model="remote-1")
        assert resp.finish_reason == "stop"
        assert resp.content == "remote pong"
        await gw.chat(messages=[{"role": "user", "content": "hi"}], model="remote-1")
        assert len(created) == 2
        assert created[0] == ("http://remote:8080/v1", "rk")


class TestExecuteThinBridge:
    @pytest.mark.asyncio
    async def test_execute_in_running_loop_errors(self):
        gw = LLMGateway()
        gw.set_default_client(_OkClient())
        result = gw.execute(messages=[{"role": "user", "content": "hi"}])
        assert "error" in result

    def test_execute_success_returns_dict(self):
        gw = LLMGateway()
        gw.set_default_client(_OkClient())
        result = gw.execute(messages=[{"role": "user", "content": "hi"}])
        assert result["content"] == "pong from mock client"
        assert result["finish_reason"] == "stop"

    def test_execute_failure_surfaced_in_usage(self):
        class _Boom:
            base_url = "http://127.0.0.1:11432/v1"
            api_key = "k"

            async def chat(self, **kwargs):
                raise RuntimeError("boom")

        gw = LLMGateway()
        gw.set_default_client(_Boom())
        result = gw.execute(messages=[{"role": "user", "content": "hi"}])
        assert "boom" in str(result.get("error", ""))
        assert result.get("content", "") == ""

    def test_legacy_sync_call_model_removed(self):
        gw = LLMGateway()
        assert not hasattr(gw, "_call_model")


class TestMemorySummarizeRealPath:
    def test_llm_summarize_uses_gateway_execute(self, tmp_path):
        gw = LLMGateway()
        gw.set_default_client(_OkClient())
        engine = MemoryEngine(db_path=tmp_path / "mem.db", gateway=gw)
        summary = engine._llm_summarize(["fact one", "fact two"], scope="work")
        assert summary == "pong from mock client"

    def test_llm_summarize_falls_back_on_failure(self, tmp_path):
        class _Boom:
            base_url = "http://127.0.0.1:11432/v1"
            api_key = "k"

            async def chat(self, **kwargs):
                raise RuntimeError("down")

        gw = LLMGateway()
        gw.set_default_client(_Boom())
        engine = MemoryEngine(db_path=tmp_path / "mem.db", gateway=gw)
        summary = engine._llm_summarize(["fact one"], scope="work")
        assert "Auto-summary" in summary


class TestStreamingSharedTransport:
    @pytest.mark.asyncio
    async def test_chat_stream_remote_uses_transport_cache(self):
        gw = LLMGateway()
        gw.register_model(
            llm_gateway_module.ModelConfig(
                name="remote-s",
                provider="openai",
                base_url="http://remote:8080/v1",
                api_key="rk",
            )
        )

        class _Chunk:
            delta_content = "x"
            delta_tool_calls = []
            finish_reason = "stop"
            usage = {}
            delta_reasoning = ""

        class _StreamClient:
            def __init__(self):
                self.calls = 0

            def chat_stream(self, **kwargs):
                self.calls += 1

                async def _gen():
                    yield _Chunk()

                return _gen()

        stream_client = _StreamClient()
        gw._transports[("http://remote:8080/v1", "rk")] = stream_client

        chunks = []
        async for chunk in gw.chat_stream(
            messages=[{"role": "user", "content": "hi"}], model="remote-s"
        ):
            chunks.append(chunk)
        assert len(chunks) == 1
        assert chunks[0]["delta_content"] == "x"
        assert stream_client.calls == 1
