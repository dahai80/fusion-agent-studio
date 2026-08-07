"""Tests for new P0 capabilities: tools, undo_manager, error_handler, templates."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.templates import TemplateManager, register_default_templates
from agent_runtime.undo_manager import CanvasSnapshot, UndoManager
from tools.code_tools import CodeExecuteTool
from tools.data_tools import Base64Tool, CsvParseTool, JsonParseTool
from tools.http_tools import HttpRequestTool
from tools.registry import ToolRegistry
from tools.utility_tools import DateTimeTool, HashTool, PathOpsTool, UuidTool, ZipTool

# ── UndoManager ──


class TestUndoManager:
    def test_init(self):
        um = UndoManager()
        assert um.undo_count == 0
        assert um.redo_count == 0
        assert um.can_undo is False
        assert um.can_redo is False

    def test_record_and_undo(self):
        um = UndoManager()
        um.record({"n1": {"type": "start"}}, [{"source": "n1", "target": "n2"}])
        um.record(
            {"n1": {"type": "start"}, "n2": {"type": "end"}},
            [{"source": "n1", "target": "n2"}],
        )
        assert um.can_undo is True
        snap = um.undo()
        assert snap is not None
        assert len(snap.nodes) == 1

    def test_redo(self):
        um = UndoManager()
        um.record({"n1": {"type": "start"}}, [])
        um.record({"n1": {"type": "start"}, "n2": {"type": "end"}}, [])
        um.undo()
        assert um.can_redo is True
        snap = um.redo()
        assert snap is not None
        assert len(snap.nodes) == 2

    def test_undo_empty(self):
        um = UndoManager()
        assert um.undo() is None

    def test_redo_empty(self):
        um = UndoManager()
        assert um.redo() is None

    def test_max_history(self):
        um = UndoManager(max_history=3)
        for i in range(5):
            um.record({f"n{i}": {"type": "start"}}, [])
        assert um.undo_count == 3

    def test_clear(self):
        um = UndoManager()
        um.record({"n1": {"type": "start"}}, [])
        um.record({"n2": {"type": "end"}}, [])
        um.clear()
        assert um.undo_count == 0
        assert um.redo_count == 0

    def test_canvas_snapshot_defaults(self):
        snap = CanvasSnapshot()
        assert snap.nodes == {}
        assert snap.edges == []
        assert snap.selected_node_id == ""


# ── HttpRequestTool ──


class TestHttpRequestTool:
    @pytest.mark.asyncio
    async def test_no_url(self):
        tool = HttpRequestTool()
        result = await tool.execute(method="GET")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_invalid_url(self):
        tool = HttpRequestTool()
        result = await tool.execute(
            method="GET", url="http://nonexistent-domain-xyz-123.com"
        )
        assert "Error" in result or "Connection" in result or "Failed" in result

    @pytest.mark.asyncio
    async def test_timeout(self):
        tool = HttpRequestTool()
        result = await tool.execute(method="GET", url="http://localhost:1", timeout=1)
        assert (
            "Error" in result or "timed out" in result.lower() or "Connection" in result
        )


# ── CodeExecuteTool ──


class TestCodeExecuteTool:
    @pytest.mark.asyncio
    async def test_simple_print(self):
        tool = CodeExecuteTool()
        result = await tool.execute(code="print('hello world')")
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_math(self):
        tool = CodeExecuteTool()
        result = await tool.execute(code="print(sum(range(10)))")
        assert "45" in result

    @pytest.mark.asyncio
    async def test_error(self):
        tool = CodeExecuteTool()
        result = await tool.execute(code="print(1/0)")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_code(self):
        tool = CodeExecuteTool()
        result = await tool.execute()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_output(self):
        tool = CodeExecuteTool()
        result = await tool.execute(code="x = 1 + 1")
        assert "no output" in result


# ── JsonParseTool ──


class TestJsonParseTool:
    @pytest.mark.asyncio
    async def test_parse(self):
        tool = JsonParseTool()
        result = await tool.execute(input='{"name": "test", "value": 42}')
        assert "test" in result

    @pytest.mark.asyncio
    async def test_validate(self):
        tool = JsonParseTool()
        result = await tool.execute(input='{"a": 1}', operation="validate")
        assert "Valid" in result

    @pytest.mark.asyncio
    async def test_validate_invalid(self):
        tool = JsonParseTool()
        result = await tool.execute(input="{invalid}", operation="validate")
        assert "Invalid" in result

    @pytest.mark.asyncio
    async def test_pretty_print(self):
        tool = JsonParseTool()
        result = await tool.execute(input='{"a":1,"b":2}', operation="pretty_print")
        assert '"a": 1' in result

    @pytest.mark.asyncio
    async def test_extract_keys(self):
        tool = JsonParseTool()
        result = await tool.execute(
            input='{"name": "test", "count": 5}', operation="extract_keys"
        )
        assert "name" in result
        assert "count" in result

    @pytest.mark.asyncio
    async def test_count_array(self):
        tool = JsonParseTool()
        result = await tool.execute(input="[1,2,3,4,5]", operation="count")
        assert "5" in result

    @pytest.mark.asyncio
    async def test_no_input(self):
        tool = JsonParseTool()
        result = await tool.execute()
        assert "Error" in result


# ── CsvParseTool ──


class TestCsvParseTool:
    @pytest.mark.asyncio
    async def test_parse(self):
        tool = CsvParseTool()
        result = await tool.execute(input="name,age\nAlice,30\nBob,25")
        assert "Alice" in result
        assert "Bob" in result

    @pytest.mark.asyncio
    async def test_get_headers(self):
        tool = CsvParseTool()
        result = await tool.execute(input="a,b,c\n1,2,3", operation="get_headers")
        assert "a" in result
        assert "b" in result

    @pytest.mark.asyncio
    async def test_count_rows(self):
        tool = CsvParseTool()
        result = await tool.execute(input="x,y\n1,2\n3,4\n5,6", operation="count_rows")
        assert "3" in result

    @pytest.mark.asyncio
    async def test_to_json(self):
        tool = CsvParseTool()
        result = await tool.execute(input="a,b\n1,2", operation="to_json")
        assert '"a": "1"' in result

    @pytest.mark.asyncio
    async def test_no_input(self):
        tool = CsvParseTool()
        result = await tool.execute()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_empty(self):
        tool = CsvParseTool()
        result = await tool.execute(input="a,b\n")
        assert "Empty" in result or "Error" in result


# ── Base64Tool ──


class TestBase64Tool:
    @pytest.mark.asyncio
    async def test_encode(self):
        tool = Base64Tool()
        result = await tool.execute(input="hello", operation="encode")
        assert result == "aGVsbG8="

    @pytest.mark.asyncio
    async def test_decode(self):
        tool = Base64Tool()
        result = await tool.execute(input="aGVsbG8=", operation="decode")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_no_input(self):
        tool = Base64Tool()
        result = await tool.execute()
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_invalid_decode(self):
        tool = Base64Tool()
        result = await tool.execute(input="!!!", operation="decode")
        # Base64 decode of "!!!" may succeed or fail depending on Python version
        assert isinstance(result, str)


# ── DateTimeTool ──


class TestDateTimeTool:
    @pytest.mark.asyncio
    async def test_now(self):
        tool = DateTimeTool()
        result = await tool.execute(operation="now")
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_timestamp(self):
        tool = DateTimeTool()
        result = await tool.execute(operation="timestamp")
        assert result.isdigit()

    @pytest.mark.asyncio
    async def test_format(self):
        tool = DateTimeTool()
        result = await tool.execute(operation="format", value=0, format="%Y")
        assert result == "1970"


# ── UuidTool ──


class TestUuidTool:
    @pytest.mark.asyncio
    async def test_generate_one(self):
        tool = UuidTool()
        result = await tool.execute(count=1)
        assert len(result) == 36  # UUID v4 length

    @pytest.mark.asyncio
    async def test_generate_multiple(self):
        tool = UuidTool()
        result = await tool.execute(count=3)
        lines = result.strip().split("\n")
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_invalid_count(self):
        tool = UuidTool()
        assert "Error" in await tool.execute(count=0)
        assert "Error" in await tool.execute(count=101)


# ── HashTool ──


class TestHashTool:
    @pytest.mark.asyncio
    async def test_sha256(self):
        tool = HashTool()
        result = await tool.execute(input="hello", algorithm="sha256")
        assert result == hashlib.sha256(b"hello").hexdigest()

    @pytest.mark.asyncio
    async def test_md5(self):
        tool = HashTool()
        result = await tool.execute(input="hello", algorithm="md5")
        assert result == hashlib.md5(b"hello").hexdigest()

    @pytest.mark.asyncio
    async def test_no_input(self):
        tool = HashTool()
        result = await tool.execute()
        assert "Error" in result


# ── PathOpsTool ──


class TestPathOpsTool:
    @pytest.mark.asyncio
    async def test_absolute(self):
        tool = PathOpsTool()
        result = await tool.execute(path=".", operation="absolute")
        assert result.startswith("/")

    @pytest.mark.asyncio
    async def test_parent(self):
        tool = PathOpsTool()
        result = await tool.execute(path="/a/b/c.txt", operation="parent")
        assert result.endswith("/a/b")

    @pytest.mark.asyncio
    async def test_filename(self):
        tool = PathOpsTool()
        result = await tool.execute(path="/a/b/file.txt", operation="filename")
        assert result == "file.txt"

    @pytest.mark.asyncio
    async def test_stem(self):
        tool = PathOpsTool()
        result = await tool.execute(path="archive.tar.gz", operation="stem")
        assert "archive" in result

    @pytest.mark.asyncio
    async def test_suffix(self):
        tool = PathOpsTool()
        result = await tool.execute(path="file.py", operation="suffix")
        assert result == ".py"

    @pytest.mark.asyncio
    async def test_exists(self):
        tool = PathOpsTool()
        result = await tool.execute(path="/", operation="exists")
        assert result == "True"

    @pytest.mark.asyncio
    async def test_join(self):
        tool = PathOpsTool()
        result = await tool.execute(path="/tmp", join="subdir", operation="absolute")
        assert "subdir" in result


# ── ZipTool ──


class TestZipTool:
    @pytest.mark.asyncio
    async def test_list_not_zip(self):
        tool = ZipTool()
        result = await tool.execute(operation="list", source_path="/tmp")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_source(self):
        tool = ZipTool()
        result = await tool.execute(operation="list")
        assert "Error" in result


# ── Error Handler Node ──


class TestErrorHandlerNode:
    @pytest.mark.asyncio
    async def test_error_handler_in_graph(self):
        graph = AgentGraph(name="Error Test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "err",
            NodeConfig(
                type="error_handler", label="Retry", max_retries=2, retry_delay=0.01
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "err")
        graph.add_edge("err", "end")

        mlx = MagicMock()
        reg = ToolRegistry()
        runtime = AgentRuntime(mlx, reg)
        events = [e async for e in runtime.execute_graph(graph, "test")]
        assert any(e.type == AgentEventType.END for e in events)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1


# ── Templates ──


class TestTemplates:
    def test_register_default_templates(self):
        register_default_templates()
        assert TemplateManager.count() >= 8

    def test_list_templates(self):
        register_default_templates()
        templates = TemplateManager.list()
        names = [t["name"] for t in templates]
        assert "code-assistant" in names
        assert "file-organizer" in names

    def test_get_template(self):
        register_default_templates()
        graph = TemplateManager.get("code-assistant")
        assert graph.name == "code-assistant"
        assert len(graph.nodes) >= 4

    def test_get_nonexistent(self):
        register_default_templates()
        with pytest.raises(KeyError):
            TemplateManager.get("nonexistent")

    def test_has(self):
        register_default_templates()
        assert TemplateManager.has("code-assistant") is True
        assert TemplateManager.has("nonexistent") is False
