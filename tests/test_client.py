"""Tests for fusion-mlx HTTP client."""
from __future__ import annotations

import pytest

from server.fusion_mlx_client import FusionMLXClient, LLMResponse


class TestLLMResponse:
    def test_defaults(self):
        resp = LLMResponse(content="Hello")
        assert resp.content == "Hello"
        assert resp.tool_calls == []
        assert resp.finish_reason == "stop"
        assert resp.usage == {"prompt_tokens": 0, "completion_tokens": 0}

    def test_custom_values(self):
        resp = LLMResponse(
            content="Hi",
            tool_calls=[{"id": "1"}],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )
        assert resp.tool_calls == [{"id": "1"}]
        assert resp.finish_reason == "tool_calls"
        assert resp.usage["prompt_tokens"] == 10


class TestFusionMLXClient:
    def test_init_defaults(self):
        client = FusionMLXClient()
        assert client.base_url == "http://localhost:11434/v1"
        assert client.api_key == "local"
        assert client.timeout == 120.0

    def test_init_custom(self):
        client = FusionMLXClient(
            base_url="http://127.0.0.1:8080/v1",
            api_key="test-key",
            timeout=30.0,
        )
        assert client.base_url == "http://127.0.0.1:8080/v1"
        assert client.api_key == "test-key"
        assert client.timeout == 30.0

    def test_base_url_strips_trailing_slash(self):
        client = FusionMLXClient(base_url="http://localhost:8000/v1/")
        assert client.base_url == "http://localhost:8000/v1"

    def test_health_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        # No server running, should return False
        import asyncio
        result = asyncio.run(client.health())
        assert result is False

    def test_get_server_stats_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        import asyncio
        result = asyncio.run(client.get_server_stats())
        assert result == {}

    def test_client_property_lazy_init(self):
        client = FusionMLXClient()
        assert client._client is None
        c = client.client
        assert client._client is not None
        assert c is client.client  # Same instance

    async def test_close(self):
        client = FusionMLXClient()
        _ = client.client  # trigger lazy init
        await client.close()
        assert client._client is None

    async def test_close_no_client(self):
        client = FusionMLXClient()
        await client.close()  # Should not raise
        assert client._client is None