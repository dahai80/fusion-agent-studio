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

from .protocol_plane import write_extensions

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
            }
            schema.update(write_extensions({
                "origin": "fusion-agent-studio",
                "readonly": bool(getattr(tool, "readonly", False)),
            }))
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


def create_rag_registry(base_url: str = ""):
    """#481: fusion-rag MCP toolset — knowledge-base search/ask/list over the
    local fusion-rag service (:11436)."""
    import os

    from tools.base import BaseTool
    from tools.registry import ToolRegistry

    base = base_url or os.environ.get("FUSION_RAG_MCP_BASE", "http://localhost:11436")

    class RAGSearchTool(BaseTool):
        def __init__(self):
            self.name = "kb_search"
            self.description = "Vector search over a fusion-rag knowledge base."
            self.parameters = {
                "kb_id": {"type": "string", "description": "knowledge base id"},
                "query": {"type": "string", "description": "search query"},
                "top_k": {"type": "integer", "description": "max results (default 5)"},
            }
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            payload = {
                "query": kwargs.get("query", ""),
                "top_k": int(kwargs.get("top_k", 5) or 5),
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{base}/kb/bases/{kwargs.get('kb_id', '')}/search", json=payload
                )
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    class RAGAskTool(BaseTool):
        def __init__(self):
            self.name = "kb_ask"
            self.description = "RAG answer (with sources) over a fusion-rag knowledge base."
            self.parameters = {
                "kb_id": {"type": "string", "description": "knowledge base id"},
                "question": {"type": "string", "description": "question to answer"},
            }
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            payload = {"question": kwargs.get("question", ""), "max_tokens": 1024}
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base}/kb/bases/{kwargs.get('kb_id', '')}/ask", json=payload
                )
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    class RAGListBasesTool(BaseTool):
        def __init__(self):
            self.name = "kb_list_bases"
            self.description = "List fusion-rag knowledge bases."
            self.parameters = {}
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base}/kb/bases")
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    reg = ToolRegistry()
    reg.register(RAGSearchTool())
    reg.register(RAGAskTool())
    reg.register(RAGListBasesTool())
    return reg


def create_model_hub_registry(base_url: str = ""):
    """#481: fusion-model-hub MCP toolset — model catalog list/search over
    the local model-hub service (:11444)."""
    import os

    from tools.base import BaseTool
    from tools.registry import ToolRegistry

    base = (
        base_url
        or os.environ.get("FUSION_MODEL_HUB_MCP_BASE", "http://localhost:11444")
    ).rstrip("/")

    class ModelListTool(BaseTool):
        def __init__(self):
            self.name = "modelhub_list"
            self.description = "List models in the local fusion model hub."
            self.parameters = {}
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/api/v1/models")
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    class ModelSearchTool(BaseTool):
        def __init__(self):
            self.name = "modelhub_search"
            self.description = "Search the local fusion model hub by name."
            self.parameters = {
                "name": {"type": "string", "description": "model name substring"},
            }
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{base}/api/v1/models/search",
                    params={"name": kwargs.get("name", "")},
                )
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    reg = ToolRegistry()
    reg.register(ModelListTool())
    reg.register(ModelSearchTool())
    return reg


def create_comfyui_registry(base_url: str = ""):
    """#481: fusion-comfyui MCP toolset — ComfyUI standard HTTP API
    (system stats + node definitions) over :11445."""
    import os

    from tools.base import BaseTool
    from tools.registry import ToolRegistry

    base = base_url or os.environ.get("FUSION_COMFYUI_MCP_BASE", "http://localhost:11445")

    class ComfyStatusTool(BaseTool):
        def __init__(self):
            self.name = "comfy_status"
            self.description = "ComfyUI system stats (version, devices, VRAM)."
            self.parameters = {}
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/system_stats")
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    class ComfyNodesTool(BaseTool):
        def __init__(self):
            self.name = "comfy_nodes"
            self.description = "ComfyUI node definitions (object_info)."
            self.parameters = {}
            self.readonly = True

        async def execute(self, **kwargs) -> str:
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{base}/object_info")
                resp.raise_for_status()
                return json.dumps(resp.json(), ensure_ascii=False)

    reg = ToolRegistry()
    reg.register(ComfyStatusTool())
    reg.register(ComfyNodesTool())
    return reg


REGISTRY_FACTORIES = {
    "mlx": create_mlx_registry,
    "rag": create_rag_registry,
    "model-hub": create_model_hub_registry,
    "comfyui": create_comfyui_registry,
}


def main(argv: list[str] | None = None) -> int:
    """#481: CLI launch entrypoint — `python -m agent_runtime.mcp_server`
    or the `fusion-mcp` console script. Serves the selected local registry
    over stdio so any standard MCP client can connect."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="fusion-mcp",
        description="Serve a local fusion toolset as a standard MCP stdio server.",
    )
    parser.add_argument(
        "--registry",
        choices=sorted(REGISTRY_FACTORIES),
        default="mlx",
        help="which local toolset to expose (default: mlx)",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="override the backing service base URL (else per-registry env default)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="MCP server name reported at initialize (default: fusion-<registry>)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # #490: long-running stdio server shows the product name, not bare Python.
    from agent_runtime.proctitle import set_process_title

    set_process_title(f"fusion-agent-studio mcp {args.registry}")
    registry = REGISTRY_FACTORIES[args.registry](args.base_url)
    server = MCPServer(registry, server_name=args.name or f"fusion-{args.registry}")
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
