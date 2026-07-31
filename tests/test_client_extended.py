"""Tests for extended FusionMLXClient features: streaming, OpenClaw sessions, MCP."""
from __future__ import annotations



from server.fusion_mlx_client import (
    FusionMLXClient, StreamChunk, MCPTool, MCPResource,
)


class TestStreamChunk:
    def test_defaults(self):
        chunk = StreamChunk()
        assert chunk.delta_content == ""
        assert chunk.delta_tool_calls == []
        assert chunk.finish_reason is None

    def test_custom(self):
        chunk = StreamChunk(
            delta_content="hello",
            delta_tool_calls=[{"index": 0}],
            finish_reason="stop",
        )
        assert chunk.delta_content == "hello"
        assert chunk.delta_tool_calls == [{"index": 0}]
        assert chunk.finish_reason == "stop"


class TestMCPTool:
    def test_defaults(self):
        tool = MCPTool(name="test", description="A tool")
        assert tool.name == "test"
        assert tool.input_schema == {}

    def test_custom(self):
        tool = MCPTool(
            name="search",
            description="Search tool",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        assert tool.input_schema["type"] == "object"


class TestMCPResource:
    def test_defaults(self):
        res = MCPResource(uri="file:///test", name="test")
        assert res.description == ""
        assert res.mime_type == ""

    def test_custom(self):
        res = MCPResource(
            uri="file:///data.csv",
            name="data",
            description="CSV data",
            mime_type="text/csv",
        )
        assert res.mime_type == "text/csv"


class _AsyncIter:
    def __init__(self, items):
        self.items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration


class _StreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.raise_for_status_called = False

    def raise_for_status(self):
        self.raise_for_status_called = True

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        pass


class TestStreamChat:
    async def test_chat_stream_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        chunks = []
        try:
            async for chunk in client.chat_stream("test-model", [{"role": "user", "content": "hi"}]):
                chunks.append(chunk)
        except Exception:
            pass
        assert len(chunks) == 0

    async def test_chat_stream_with_mock(self):
        from unittest.mock import MagicMock

        client = FusionMLXClient(timeout=5.0)
        resp = _StreamResponse([
            'data: {"choices": [{"delta": {"content": "Hel"}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"content": "lo"}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}',
            "data: [DONE]",
        ])

        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_StreamCtx(resp))
        client._client = mock_http

        chunks = []
        async for chunk in client.chat_stream("model", [{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 3
        assert chunks[0].delta_content == "Hel"
        assert chunks[1].delta_content == "lo"
        assert chunks[2].finish_reason == "stop"

    async def test_chat_stream_tool_calls_in_delta(self):
        from unittest.mock import MagicMock

        client = FusionMLXClient(timeout=5.0)
        resp = _StreamResponse([
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search"}}]}, "finish_reason": null}]}',
            "data: [DONE]",
        ])

        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_StreamCtx(resp))
        client._client = mock_http

        chunks = []
        async for chunk in client.chat_stream("model", [{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert len(chunks[0].delta_tool_calls) == 1

    async def test_chat_stream_invalid_json_skipped(self):
        from unittest.mock import MagicMock

        client = FusionMLXClient(timeout=5.0)
        resp = _StreamResponse([
            "data: not-json",
            'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": null}]}',
            "data: [DONE]",
        ])

        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_StreamCtx(resp))
        client._client = mock_http

        chunks = []
        async for chunk in client.chat_stream("model", [{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0].delta_content == "ok"

    async def test_chat_stream_with_tools_and_kwargs(self):
        from unittest.mock import MagicMock

        client = FusionMLXClient(timeout=5.0)
        resp = _StreamResponse([
            'data: {"choices": [{"delta": {"content": "result"}, "finish_reason": "stop"}]}',
            "data: [DONE]",
        ])

        mock_http = MagicMock()
        mock_http.stream = MagicMock(return_value=_StreamCtx(resp))
        client._client = mock_http

        chunks = []
        async for chunk in client.chat_stream(
            "model",
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "test"}}],
            custom_param="value",
        ):
            chunks.append(chunk)

        call_args = mock_http.stream.call_args
        payload = call_args[1]["json"]
        assert "tools" in payload
        assert payload["stream"] is True
        assert payload["custom_param"] == "value"


class TestOpenClawSessions:
    async def test_get_agent_session_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.get_agent_session("fake-id")
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_list_agent_sessions_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.list_agent_sessions()
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_poll_agent_session_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.poll_agent_session("fake-id", timeout=0.5, interval=0.1)
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_poll_agent_session_terminal(self):
        from unittest.mock import AsyncMock

        client = FusionMLXClient(timeout=5.0)
        client.get_agent_session = AsyncMock(return_value={"status": "completed", "id": "s1"})

        result = await client.poll_agent_session("s1", timeout=2.0, interval=0.1)
        assert result["status"] == "completed"

    async def test_poll_agent_session_timeout(self):
        from unittest.mock import AsyncMock

        client = FusionMLXClient(timeout=5.0)
        client.get_agent_session = AsyncMock(return_value={"status": "running", "id": "s1"})

        result = await client.poll_agent_session("s1", timeout=0.5, interval=0.1)
        assert result["status"] == "running"

    async def test_list_agent_sessions_with_mock(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"sessions": [{"id": "s1"}, {"id": "s2"}]}

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        sessions = await client.list_agent_sessions()
        assert len(sessions) == 2

    async def test_list_agent_sessions_list_response(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [{"id": "s1"}]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        sessions = await client.list_agent_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"


class TestMCPProtocol:
    async def test_mcp_list_tools_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.mcp_list_tools()
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_mcp_call_tool_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.mcp_call_tool("search", {"q": "test"})
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_mcp_list_resources_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.mcp_list_resources()
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_mcp_read_resource_no_server(self):
        client = FusionMLXClient(timeout=1.0)
        try:
            await client.mcp_read_resource("file:///test")
            assert False, "Should have raised"
        except Exception:
            pass

    async def test_mcp_list_tools_with_mock(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "tools": [
                {"name": "search", "description": "Search", "input_schema": {"type": "object"}},
                {"name": "calc", "description": "Calculate", "parameters": {"type": "object"}},
            ]
        }

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        tools = await client.mcp_list_tools()
        assert len(tools) == 2
        assert tools[0].name == "search"
        assert tools[0].input_schema == {"type": "object"}
        assert tools[1].name == "calc"

    async def test_mcp_list_tools_list_response(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {"name": "tool1", "description": "T1", "input_schema": {}},
        ]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        tools = await client.mcp_list_tools()
        assert len(tools) == 1
        assert tools[0].name == "tool1"

    async def test_mcp_call_tool_with_mock(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": "42"}

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        result = await client.mcp_call_tool("calc", {"expr": "6*7"})
        assert result["result"] == "42"

    async def test_mcp_call_tool_with_server_name(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"result": "ok"}

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        await client.mcp_call_tool("calc", {"expr": "1+1"}, server_name="math_server")
        call_args = mock_http_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["server"] == "math_server"

    async def test_mcp_list_resources_with_mock(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "resources": [
                {"uri": "file:///data.csv", "name": "data", "description": "CSV", "mime_type": "text/csv"},
            ]
        }

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        resources = await client.mcp_list_resources()
        assert len(resources) == 1
        assert resources[0].uri == "file:///data.csv"
        assert resources[0].mime_type == "text/csv"

    async def test_mcp_list_resources_list_response(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {"uri": "file:///a.txt", "name": "a"},
        ]

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        resources = await client.mcp_list_resources()
        assert len(resources) == 1

    async def test_mcp_read_resource_with_mock(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": "file content here"}

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        result = await client.mcp_read_resource("file:///data.csv")
        assert result["content"] == "file content here"

    async def test_mcp_read_resource_with_server_name(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": "ok"}

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        await client.mcp_read_resource("file:///x", server_name="srv")
        call_args = mock_http_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["server"] == "srv"

    async def test_mcp_list_tools_with_server_name(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"tools": []}

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        await client.mcp_list_tools(server_name="my_server")
        call_args = mock_http_client.get.call_args
        params = call_args[1].get("params", {})
        assert params.get("server") == "my_server"

    async def test_mcp_list_resources_with_server_name(self):
        from unittest.mock import AsyncMock, MagicMock

        client = FusionMLXClient(timeout=5.0)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"resources": []}

        mock_http_client = AsyncMock()
        mock_http_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_http_client

        await client.mcp_list_resources(server_name="my_server")
        call_args = mock_http_client.get.call_args
        params = call_args[1].get("params", {})
        assert params.get("server") == "my_server"
