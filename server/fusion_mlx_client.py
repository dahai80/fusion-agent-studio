"""FusionMLX HTTP client — Agent Studio's only interface to fusion-mlx.

This module is the sole bridge between Agent Studio and fusion-mlx.
It communicates exclusively through HTTP — no direct imports of
fusion-mlx's engine, pool, or MLX code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class LLMResponse:
    """Structured response from an LLM call."""

    content: str
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })


class FusionMLXClient:
    """HTTP client for fusion-mlx's OpenAI-compatible API.

    All LLM interactions go through this class. It never imports
    any fusion-mlx internal module — only communicates via HTTP.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "local",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream: bool = False,
        **kwargs,
    ) -> LLMResponse:
        """Call fusion-mlx's /v1/chat/completions endpoint.

        Args:
            model: Model name (e.g., "qwen3.5-9b").
            messages: Conversation messages in OpenAI format.
            tools: Optional tool definitions for function calling.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream: Enable streaming (not yet implemented).
            **kwargs: Additional parameters to pass to the API.

        Returns:
            LLMResponse with content, tool_calls, and usage.
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        message = choice.get("message", {})

        return LLMResponse(
            content=message.get("content", ""),
            tool_calls=message.get("tool_calls", []),
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from fusion-mlx."""
        resp = await self.client.get("/models")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    async def health(self) -> bool:
        """Check if fusion-mlx is healthy and reachable."""
        try:
            resp = await self.client.get("/models", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def get_server_stats(self) -> dict[str, Any]:
        """Get server statistics from fusion-mlx."""
        try:
            resp = await self.client.get("/stats", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def create_agent_session(
        self,
        model: str,
        system_prompt: str = "",
        tools: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Create an agent session via OpenClaw protocol."""
        payload = {"model": model, "system_prompt": system_prompt}
        if tools:
            payload["tools"] = tools
        resp = await self.client.post("/openclaw/agent/sessions", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def submit_tool_result(
        self, session_id: str, tool_call_id: str, result: str
    ) -> dict[str, Any]:
        """Submit a tool execution result back to the agent session."""
        payload = {
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "result": result,
        }
        resp = await self.client.post("/openclaw/agent/tool-results", json=payload)
        resp.raise_for_status()
        return resp.json()