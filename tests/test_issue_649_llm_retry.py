"""Tests for issue #649 - LLM call retry with backoff on transient 5xx."""

from __future__ import annotations

import json

import httpx
import pytest

from server.fusion_mlx_client import FusionMLXClient


def _client(handler) -> FusionMLXClient:
    c = FusionMLXClient(base_url="http://test:11432/v1", api_key="k")
    c._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=c.base_url
    )
    return c


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setenv("FUSION_LLM_RETRY_BACKOFF", "0")


def _ok_body():
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.asyncio
async def test_503_then_recovers_via_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json=_ok_body())

    c = _client(handler)
    resp = await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert calls["n"] == 3
    await c.close()


@pytest.mark.asyncio
async def test_persistent_500_fails_after_max_attempts():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    c = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 3
    await c.close()


@pytest.mark.asyncio
async def test_401_fails_fast_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    c = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert calls["n"] == 1
    await c.close()


@pytest.mark.asyncio
async def test_connection_reset_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json=_ok_body())

    c = _client(handler)
    resp = await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert calls["n"] == 2
    await c.close()


def test_retry_env_override(monkeypatch):
    from server.fusion_mlx_client import _llm_retry_config

    monkeypatch.setenv("FUSION_LLM_RETRY_MAX", "5")
    monkeypatch.setenv("FUSION_LLM_RETRY_BACKOFF", "1.5")
    assert _llm_retry_config() == (5, 1.5)
    monkeypatch.setenv("FUSION_LLM_RETRY_MAX", "bogus")
    assert _llm_retry_config()[0] >= 1


def test_4xx_not_retryable():
    from server.fusion_mlx_client import _is_retryable_llm_error

    req = httpx.Request("POST", "http://x/v1/chat/completions")
    e401 = httpx.HTTPStatusError("401", request=req, response=httpx.Response(401))
    e503 = httpx.HTTPStatusError("503", request=req, response=httpx.Response(503))
    assert not _is_retryable_llm_error(e401)
    assert _is_retryable_llm_error(e503)
    assert _is_retryable_llm_error(httpx.ConnectError("reset"))


@pytest.mark.asyncio
async def test_stream_open_503_retried_before_first_chunk():
    calls = {"n": 0}
    payload = {"choices": [{"delta": {"content": "hi"}}]}
    sse = "data: " + json.dumps(payload) + chr(10) * 2

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=sse)

    c = _client(handler)
    chunks = []
    async for ch in c.chat_stream(model="m", messages=[{"role": "user", "content": "hi"}]):
        chunks.append(ch)
    assert [ch.delta_content for ch in chunks] == ["hi"]
    assert calls["n"] == 2
    await c.close()
