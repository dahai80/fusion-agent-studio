"""Tests for tools module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry
from tools.file_tools import FileReadTool, FileWriteTool, FileListTool
from tools.terminal_tools import TerminalTool
from tools.git_tools import GitTool
from tools.text_tools import TextProcessTool, TextSearchTool
from tools import create_default_registry


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(success=True, output="done")
        assert str(r) == "done"

    def test_error_result(self):
        r = ToolResult(success=False, error="failed")
        assert "Error" in str(r)

    def test_empty_result(self):
        r = ToolResult(success=True)
        assert str(r) == ""


class TestBaseTool:
    def test_openai_schema(self):
        class TestTool(BaseTool):
            name = "test_tool"
            description = "A test tool"
            parameters = {
                "input": {"type": "string", "description": "Input text"},
            }

            async def execute(self, **kwargs) -> str:
                return "ok"

        tool = TestTool()
        schema = tool.openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "test_tool"
        assert "input" in schema["function"]["parameters"]["properties"]

    def test_default_name_from_class(self):
        class MyCustomTool(BaseTool):
            name = "my_custom_tool"
            description = "Custom tool"

            async def execute(self, **kwargs) -> str:
                return "ok"

        tool = MyCustomTool()
        assert tool.name == "my_custom_tool"


class TestToolRegistry:
    def test_register(self):
        registry = ToolRegistry()
        tool = FileReadTool()
        registry.register(tool)
        assert registry.has("file_read")
        assert registry.get("file_read") is tool

    def test_register_no_name(self):
        registry = ToolRegistry()
        from unittest.mock import MagicMock, AsyncMock

        bad_tool = MagicMock(spec=BaseTool)
        bad_tool.name = ""
        bad_tool.description = ""
        bad_tool.parameters = {}
        bad_tool.execute = AsyncMock(return_value="")
        with pytest.raises(ValueError, match="must have a name"):
            registry.register(bad_tool)

    def test_unregister(self):
        registry = ToolRegistry()
        registry.register(FileReadTool())
        registry.unregister("file_read")
        assert not registry.has("file_read")

    def test_get_nonexistent(self):
        registry = ToolRegistry()
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_list_tools(self):
        registry = ToolRegistry()
        registry.register(FileReadTool())
        registry.register(FileWriteTool())
        tools = registry.list_tools()
        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "file_read" in names
        assert "file_write" in names

    def test_to_openai_schemas(self):
        registry = ToolRegistry()
        registry.register(FileReadTool())
        schemas = registry.to_openai_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"

    def test_count(self):
        registry = ToolRegistry()
        assert registry.count == 0
        registry.register(FileReadTool())
        assert registry.count == 1

    def test_register_from_class(self):
        registry = ToolRegistry()
        tool = registry.register_from_class(FileReadTool)
        assert registry.has("file_read")
        assert isinstance(tool, FileReadTool)

    def test_create_default_registry(self):
        registry = create_default_registry()
        assert registry.count >= 7  # 7 built-in tools
        assert registry.has("file_read")
        assert registry.has("file_write")
        assert registry.has("file_list")
        assert registry.has("terminal")
        assert registry.has("git")
        assert registry.has("text_process")
        assert registry.has("text_search")


class TestFileReadTool:
    async def test_read_existing_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("Hello, World!")
            f.flush()
            path = f.name

        tool = FileReadTool()
        result = await tool.execute(path=path)
        assert result == "Hello, World!"

    async def test_read_nonexistent_file(self):
        tool = FileReadTool()
        result = await tool.execute(path="/nonexistent/path/file.txt")
        assert "Error: File not found" in result

    async def test_read_no_path(self):
        tool = FileReadTool()
        result = await tool.execute()
        assert "Error: path is required" in result

    async def test_read_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileReadTool()
            result = await tool.execute(path=tmpdir)
            assert "Error: Not a file" in result


class TestFileWriteTool:
    async def test_write_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            tool = FileWriteTool()
            result = await tool.execute(path=str(path), content="Hello")
            assert "Written to" in result
            assert path.read_text() == "Hello"

    async def test_append_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            path.write_text("Hello")
            tool = FileWriteTool()
            result = await tool.execute(path=str(path), content=" World", append=True)
            assert "Appended to" in result
            assert path.read_text() == "Hello World"

    async def test_write_no_path(self):
        tool = FileWriteTool()
        result = await tool.execute(content="test")
        assert "Error: path is required" in result

    async def test_write_creates_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "nested" / "test.txt"
            tool = FileWriteTool()
            result = await tool.execute(path=str(path), content="nested")
            assert "Written to" in result
            assert path.exists()


class TestFileListTool:
    async def test_list_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "file1.txt").write_text("a")
            Path(tmpdir, "file2.py").write_text("b")
            Path(tmpdir, "subdir").mkdir()

            tool = FileListTool()
            result = await tool.execute(path=tmpdir)
            assert "file1.txt" in result
            assert "file2.py" in result
            assert "[DIR]" in result or "subdir" in result

    async def test_list_with_pattern(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "data.txt").write_text("a")
            Path(tmpdir, "script.py").write_text("b")
            Path(tmpdir, "notes.txt").write_text("c")

            tool = FileListTool()
            result = await tool.execute(path=tmpdir, pattern="*.txt")
            assert "data.txt" in result
            assert "notes.txt" in result
            assert "script.py" not in result

    async def test_list_nonexistent(self):
        tool = FileListTool()
        result = await tool.execute(path="/nonexistent")
        assert "Error: Path not found" in result

    async def test_list_file_not_dir(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            tool = FileListTool()
            result = await tool.execute(path=f.name)
            assert "Error: Not a directory" in result

    async def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileListTool()
            result = await tool.execute(path=tmpdir)
            assert "Empty directory" in result


class TestTerminalTool:
    async def test_execute_simple_command(self):
        tool = TerminalTool()
        result = await tool.execute(command="echo hello")
        assert "hello" in result

    async def test_execute_with_args(self):
        tool = TerminalTool()
        result = await tool.execute(command="echo hello world", timeout=5)
        assert "hello world" in result

    async def test_no_command(self):
        tool = TerminalTool()
        result = await tool.execute()
        assert "Error: command is required" in result

    async def test_too_long_command(self):
        tool = TerminalTool()
        result = await tool.execute(command="x" * 10001, timeout=5)
        assert "Error: command too long" in result

    async def test_nonexistent_command(self):
        tool = TerminalTool()
        result = await tool.execute(command="nonexistent_cmd_xyz", timeout=5)
        assert "not found" in result or "Error" in result

    async def test_timeout(self):
        tool = TerminalTool()
        result = await tool.execute(command="sleep 10", timeout=1)
        assert "timed out" in result.lower() or "Error" in result


class TestGitTool:
    async def test_git_status_in_non_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = GitTool()
            result = await tool.execute(action="status", repo_path=tmpdir)
            assert "Not a git repository" in result

    async def test_git_invalid_action(self):
        tool = GitTool()
        result = await tool.execute(action="invalid_action")
        assert "Unknown action" in result

    async def test_commit_no_message(self):
        tool = GitTool()
        result = await tool.execute(action="commit", repo_path=".")
        assert "Error: commit message is required" in result


class TestTextProcessTool:
    async def test_uppercase(self):
        tool = TextProcessTool()
        result = await tool.execute(text="hello", operation="uppercase")
        assert result == "HELLO"

    async def test_lowercase(self):
        tool = TextProcessTool()
        result = await tool.execute(text="HELLO", operation="lowercase")
        assert result == "hello"

    async def test_trim(self):
        tool = TextProcessTool()
        result = await tool.execute(text="  hello  ", operation="trim")
        assert result == "hello"

    async def test_count_words(self):
        tool = TextProcessTool()
        result = await tool.execute(text="hello world foo", operation="count_words")
        assert "3" in result

    async def test_count_lines(self):
        tool = TextProcessTool()
        result = await tool.execute(text="a\nb\nc\n", operation="count_lines")
        assert "Total lines: 4" in result  # 4 lines: "a", "b", "c", ""

    async def test_count_chars(self):
        tool = TextProcessTool()
        result = await tool.execute(text="hello", operation="count_chars")
        assert "5" in result

    async def test_split_lines(self):
        tool = TextProcessTool()
        result = await tool.execute(text="a\nb\nc", operation="split_lines")
        assert "1: a" in result
        assert "2: b" in result
        assert "3: c" in result

    async def test_reverse(self):
        tool = TextProcessTool()
        result = await tool.execute(text="hello", operation="reverse")
        assert result == "olleh"

    async def test_sort_lines(self):
        tool = TextProcessTool()
        result = await tool.execute(text="c\na\nb", operation="sort_lines")
        assert result == "a\nb\nc"

    async def test_unique_lines(self):
        tool = TextProcessTool()
        result = await tool.execute(text="a\nb\na\nc\nb", operation="unique_lines")
        assert result == "a\nb\nc"

    async def test_no_text(self):
        tool = TextProcessTool()
        result = await tool.execute(operation="count_words")
        assert "Error: text is required" in result

    async def test_unknown_operation(self):
        tool = TextProcessTool()
        result = await tool.execute(text="test", operation="unknown")
        assert "Unknown operation" in result


class TestTextSearchTool:
    async def test_find_plain_text(self):
        tool = TextSearchTool()
        result = await tool.execute(
            text="hello world hello",
            pattern="hello",
        )
        assert "2 occurrence" in result

    async def test_find_no_match(self):
        tool = TextSearchTool()
        result = await tool.execute(
            text="hello world",
            pattern="xyz",
        )
        assert "No matches" in result

    async def test_find_regex(self):
        tool = TextSearchTool()
        result = await tool.execute(
            text="abc123 def456",
            pattern=r"\d+",
            use_regex=True,
        )
        assert "2 match" in result

    async def test_no_text(self):
        tool = TextSearchTool()
        result = await tool.execute(pattern="test")
        assert "Error: text is required" in result

    async def test_no_pattern(self):
        tool = TextSearchTool()
        result = await tool.execute(text="test")
        assert "Error: pattern is required" in result

    async def test_invalid_regex(self):
        tool = TextSearchTool()
        result = await tool.execute(
            text="test",
            pattern=r"[invalid",
            use_regex=True,
        )
        assert "Error in regex" in result
