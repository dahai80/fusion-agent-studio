"""FusionMLX HTTP client — Agent Studio's only interface to fusion-mlx.

This module is the sole bridge between Agent Studio and fusion-mlx.
It communicates exclusively through HTTP — no direct imports of
fusion-mlx's engine, pool, or MLX code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)


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


@dataclass
class StreamChunk:
    """A single chunk from a streaming LLM response."""

    delta_content: str = ""
    delta_tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None


@dataclass
class MCPTool:
    """An MCP tool definition discovered from a server."""

    name: str
    description: str
    input_schema: dict = field(default_factory=dict)


@dataclass
class MCPResource:
    """An MCP resource discovered from a server."""

    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


class FusionMLXClient:
    """HTTP client for fusion-mlx's OpenAI-compatible API.

    All LLM interactions go through this class. It never imports
    any fusion-mlx internal module — only communicates via HTTP.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
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

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completions from fusion-mlx.

        Yields StreamChunk objects as tokens arrive.
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)

        async with self.client.stream(
            "POST", "/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse SSE chunk: %s", data_str[:100])
                    continue

                choice = data.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason")

                chunk = StreamChunk(
                    delta_content=delta.get("content", ""),
                    delta_tool_calls=delta.get("tool_calls", []),
                    finish_reason=finish_reason,
                )
                yield chunk

    async def get_agent_session(self, session_id: str) -> dict[str, Any]:
        """Get an existing agent session's state via OpenClaw protocol."""
        resp = await self.client.get(f"/openclaw/agent/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()

    async def list_agent_sessions(self) -> list[dict[str, Any]]:
        """List all active agent sessions."""
        resp = await self.client.get("/openclaw/agent/sessions")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("sessions", [])

    async def poll_agent_session(
        self, session_id: str, timeout: float = 30.0, interval: float = 1.0
    ) -> dict[str, Any]:
        """Poll an agent session until it reaches a terminal state.

        Args:
            session_id: The session to poll.
            timeout: Maximum seconds to wait.
            interval: Seconds between polls.

        Returns:
            Final session state dict.
        """
        import asyncio

        terminal_states = {"completed", "failed", "cancelled"}
        elapsed = 0.0

        while elapsed < timeout:
            session = await self.get_agent_session(session_id)
            status = session.get("status", "")
            if status in terminal_states:
                return session
            await asyncio.sleep(interval)
            elapsed += interval

        logger.warning("Session %s polling timed out after %.1fs", session_id, timeout)
        return await self.get_agent_session(session_id)

    async def mcp_list_tools(self, server_name: str = "") -> list[MCPTool]:
        """List available tools from an MCP server via fusion-mlx.

        Args:
            server_name: Optional MCP server name. If empty, lists from all.
        """
        path = "/mcp/tools"
        params = {}
        if server_name:
            params["server"] = server_name

        resp = await self.client.get(path, params=params)
        resp.raise_for_status()
        data = resp.json()

        tools = []
        tool_list = data if isinstance(data, list) else data.get("tools", [])
        for t in tool_list:
            tools.append(MCPTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("input_schema", t.get("parameters", {})),
            ))
        return tools

    async def mcp_call_tool(
        self, tool_name: str, arguments: dict[str, Any], server_name: str = ""
    ) -> dict[str, Any]:
        """Call an MCP tool through fusion-mlx.

        Args:
            tool_name: The tool to invoke.
            arguments: Tool input arguments.
            server_name: Optional MCP server name.
        """
        payload = {
            "tool_name": tool_name,
            "arguments": arguments,
        }
        if server_name:
            payload["server"] = server_name

        resp = await self.client.post("/mcp/tools/call", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def embeddings(
        self,
        model: str,
        input: str | list[str],
        **kwargs,
    ) -> list[list[float]]:
        """Call fusion-mlx's /v1/embeddings endpoint.

        Args:
            model: Embedding model name.
            input: Text or list of texts to embed.
            **kwargs: Additional parameters.

        Returns:
            List of embedding vectors.
        """
        payload = {"model": model, "input": input}
        payload.update(kwargs)

        resp = await self.client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()

        embeddings = []
        for item in data.get("data", []):
            embeddings.append(item.get("embedding", []))
        return embeddings

    async def mcp_list_resources(self, server_name: str = "") -> list[MCPResource]:
        """List available resources from an MCP server via fusion-mlx."""
        path = "/mcp/resources"
        params = {}
        if server_name:
            params["server"] = server_name

        resp = await self.client.get(path, params=params)
        resp.raise_for_status()
        data = resp.json()

        resources = []
        res_list = data if isinstance(data, list) else data.get("resources", [])
        for r in res_list:
            resources.append(MCPResource(
                uri=r.get("uri", ""),
                name=r.get("name", ""),
                description=r.get("description", ""),
                mime_type=r.get("mime_type", ""),
            ))
        return resources

    async def mcp_read_resource(
        self, uri: str, server_name: str = ""
    ) -> dict[str, Any]:
        """Read an MCP resource through fusion-mlx.

        Args:
            uri: The resource URI to read.
            server_name: Optional MCP server name.
        """
        payload = {"uri": uri}
        if server_name:
            payload["server"] = server_name

        resp = await self.client.post("/mcp/resources/read", json=payload)
        resp.raise_for_status()
        return resp.json()