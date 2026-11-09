"""Tests for issue #657 - retry/failure logs must never carry empty err=."""

from __future__ import annotations

import logging

import httpx
import pytest

from server.fusion_mlx_client import (
    FusionMLXClient,
    _is_retryable_llm_error,
    _llm_err_detail,
    _mask_secrets,
)


def _client(handler) -> FusionMLXClient:
    c = FusionMLXClient(base_url="http://test:11432/v1", api_key="k")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=c.base_url)
    return c


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setenv("FUSION_LLM_RETRY_BACKOFF", "0")


def _ok_body():
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _log_lines(caplog):
    return [r.getMessage() for r in caplog.records if "fusion_mlx_client" in r.name]


class TestAuthErrorVisible:
    async def test_401_body_lands_in_error_log(self, caplog):
        # #657 incident: gateway auth_error body was invisible (err= blank).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": "Missing API key", "type": "auth_error"}},
            )

        with caplog.at_level(logging.ERROR, logger="server.fusion_mlx_client"):
            c = _client(handler)
            with pytest.raises(httpx.HTTPStatusError):
                await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
            await c.close()

        lines = _log_lines(caplog)
        assert lines, "final failure must be logged even on 4xx fail-fast"
        joined = chr(10).join(lines)
        assert "401" in joined
        assert "Missing API key" in joined
        assert "err=<unknown>" not in joined

    async def test_401_stream_open_body_lands_in_error_log(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": "Missing API key", "type": "auth_error"}},
            )

        with caplog.at_level(logging.ERROR, logger="server.fusion_mlx_client"):
            c = _client(handler)
            gen = c.chat_stream(model="m", messages=[{"role": "user", "content": "hi"}])
            with pytest.raises(httpx.HTTPStatusError):
                await gen.__anext__()
            await c.close()

        joined = chr(10).join(_log_lines(caplog))
        assert "stream open failed" in joined
        assert "401" in joined and "Missing API key" in joined


class TestEmptyErrImpossible:
    def test_empty_str_exception_yields_repr_or_unknown(self):
        e = httpx.ReadTimeout("")
        assert _llm_err_detail(e) != ""
        assert _llm_err_detail(e) == "<unknown>" or "ReadTimeout" in _llm_err_detail(e)

    def test_http_error_carries_status_and_body(self):
        req = httpx.Request("POST", "http://gw/v1/chat/completions")
        resp = httpx.Response(401, json={"error": {"message": "Missing API key"}}, request=req)
        detail = _llm_err_detail(httpx.HTTPStatusError("", request=req, response=resp))
        assert detail.startswith("HTTP 401")
        assert "Missing API key" in detail

    async def test_retry_log_never_blank_err(self, caplog):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("")

        with caplog.at_level(logging.WARNING, logger="server.fusion_mlx_client"):
            c = _client(handler)
            with pytest.raises(httpx.ReadTimeout):
                await c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
            await c.close()

        assert calls["n"] == 3
        for line in _log_lines(caplog):
            if "err=" in line:
                tail = line.split("err=", 1)[1]
                assert tail.split(" ")[0] not in ("", "next_in"), f"blank err= in: {line}"


class TestSecretMasking:
    def test_bearer_and_api_key_masked(self):
        text = 'auth Bearer sk-abcdef12345678 api_key=secret123456 {"api-key": "tok12345678"}'
        masked = _mask_secrets(text)
        assert "sk-abcdef12345678" not in masked
        assert "secret123456" not in masked
        assert "tok12345678" not in masked
        assert "<masked>" in masked

    def test_normal_body_untouched(self):
        assert _mask_secrets("Missing API key") == "Missing API key"

    def test_retryable_classifier_unchanged(self):
        assert _is_retryable_llm_error(httpx.ReadTimeout("")) is True
        req = httpx.Request("POST", "http://gw/")
        resp401 = httpx.Response(401, request=req)
        assert _is_retryable_llm_error(httpx.HTTPStatusError("", request=req, response=resp401)) is False
