"""N17 protocol plane (#356): local MCP stdio server exporter.

Exposes a ToolRegistry as a standard MCP server (JSON-RPC 2.0 over stdio,
newline-delimited) so third-party MCP clients can call fusion tools
unmodified — e.g. the built-in fusion-mlx toolset over the gateway.

Fusion enhancements ride in x-fusion-* extension fields only; the wire
format stays stock MCP (zero-fork commitment).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, registry, server_name: str = "fusion-agent-studio"):
        self.registry = registry
        self.server_name = server_name
        self._initialized = False

    def _tool_schemas(self) -> list[dict]:
        out = []
        for meta in self.registry.list_tools(role="all"):
            tool = self.registry.get(meta["name"]) if self.registry.has(meta["name"]) else None
            params = getattr(tool, "parameters", None) or meta.get("parameters", {})
            schema = {
                "name": meta["name"],
                "description": meta.get("description", ""),
                "inputSchema": {"type": "object", "properties": params},
                "x-fusion-origin": "fusion-agent-studio",
                "x-fusion-readonly": bool(getattr(tool, "readonly", False)),
            }
            out.append(schema)
        return out

    async def _handle_request(self, req: dict) -> dict | None:
        method = req.get("method", "")
        req_id = req.get("id")
        if method == "initialize":
            self._initialized = True
            return self._result(req_id, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.server_name, "version": "1.0.0"},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._result(req_id, {})
        if method == "tools/list":
            return self._result(req_id, {"tools": self._tool_schemas()})
        if method == "tools/call":
            params = req.get("params") or {}
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            tool = self.registry.get(name) if self.registry.has(name) else None
            if tool is None:
                return self._error(req_id, -32602, f"unknown tool: {name}")
            try:
                text = await tool.execute(**arguments)
                return self._result(req_id, {
                    "content": [{"type": "text", "text": str(text)}],
                    "isError": str(text).startswith("Error:"),
                })
            except Exception as e:
                logger.warning("mcp tools/call failed tool=%s: %s", name, e)
                return self._result(req_id, {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                })
        return self._error(req_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(req_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def serve(self) -> None:
        asyncio.run(self.serve_stdio())

    async def serve_stdio(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        logger.info("mcp stdio server up name=%s tools=%d", self.server_name, len(self._tool_schemas()))
        while True:
            line = await reader.readline()
            if not line:
                logger.info("mcp stdio server stdin closed")
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                resp = self._error(None, -32700, "parse error")
            else:
                resp = await self._handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def create_mlx_registry(gateway_base: str = ""):
    """Built-in fusion-mlx MCP toolset: standard OpenAI-compatible chat over
    the local gateway + server status. Fully offline."""
    import os

    from tools.base import BaseTool

    base = gateway_base or os.environ.get("FUSION_MLX_MCP_BASE", "http://localhost:11432")
    headers = {}
    key = os.environ.get("FUSION_MLX_MCP_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    class MLXChatTool(BaseTool):
        def __init__(self):
            self.name = "mlx_chat"
            self.description = "Chat completion via the local fusion-mlx gateway (OpenAI-compatible)."
            self.parameters = {
                "model": {"type": "string", "description": "model name"},
                "messages": {"type": "array", "description": "OpenAI-format messages"},
                "max_tokens": {"type": "integer", "description": "max tokens (default 512)"},
            }
            self.readonly = False

        async def execute(self, **kwargs) -> str:
            import httpx

            payload = {
                "model": kwargs.get("model", "default"),
                "messages": kwargs.get("messages") or [],
                "max_tokens": int(kwargs.get("max_tokens", 512) or 512),
            }
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base}/v1/chat/completions", json=payload, headers=headers
                )
                resp.raise_for_status()
                data = resp.json()
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return text or json.dumps(data, ensure_ascii=False)

    class MLXStatusTool(BaseTool):
        def __init__(self):
            self.name = "mlx_status"
            self.description = "Local fusion-mlx server status (models, health)."
            self.parameters = {}
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                models = await client.get(f"{base}/v1/models", headers=headers)
            return json.dumps({"status_code": models.status_code, "models": models.json()}, ensure_ascii=False)

    from tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(MLXChatTool())
    reg.register(MLXStatusTool())
    return reg
