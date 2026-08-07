"""HTTP request tool — make HTTP requests to external APIs."""
from __future__ import annotations

from .base import BaseTool


class HttpRequestTool(BaseTool):
    name = "http_request"
    description = "Make HTTP requests to external APIs (GET, POST, PUT, DELETE, PATCH)"
    parameters = {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "description": "HTTP method"},
        "url": {"type": "string", "description": "Request URL"},
        "headers": {"type": "object", "description": "HTTP headers (JSON object)", "default": {}},
        "body": {"type": "string", "description": "Request body (for POST/PUT/PATCH)", "default": ""},
        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
    }

    async def execute(self, **kwargs) -> str:
        import httpx
        method = kwargs.get("method", "GET").upper()
        url = kwargs.get("url", "")
        headers = kwargs.get("headers", {}) or {}
        body = kwargs.get("body", None)
        timeout = int(kwargs.get("timeout", 30))
        if not url:
            return "Error: url is required"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.request(method, url, headers=headers, content=body)
                text = resp.text[:10000]
                if len(resp.text) > 10000:
                    text += f"\n... (truncated, total {len(resp.text)} bytes)"
                return f"Status: {resp.status_code}\nHeaders: {dict(resp.headers)}\n\nBody:\n{text}"
        except httpx.TimeoutException:
            return f"Error: Request timed out after {timeout}s"
        except httpx.ConnectError as e:
            return f"Error: Connection failed: {e}"
        except Exception as e:
            return f"Error: {e}"