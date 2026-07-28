"""Built-in tools for agent execution."""

from .base import BaseTool, ToolResult
from .registry import ToolRegistry
from .file_tools import FileReadTool, FileWriteTool, FileListTool
from .terminal_tools import TerminalTool
from .git_tools import GitTool
from .text_tools import TextProcessTool, TextSearchTool
from .http_tools import HttpRequestTool
from .code_tools import CodeExecuteTool
from .data_tools import JsonParseTool, CsvParseTool, Base64Tool
from .utility_tools import DateTimeTool, UuidTool, HashTool, PathOpsTool, ZipTool
from .db_tools import SqliteQueryTool, AnnotationNode
from .computer_use_tools import ScreenCaptureTool, MouseTool, KeyboardTool, ClipboardTool

__all__ = [
    "BaseTool", "ToolResult",
    "ToolRegistry",
    "FileReadTool", "FileWriteTool", "FileListTool",
    "TerminalTool",
    "GitTool",
    "TextProcessTool", "TextSearchTool",
    "HttpRequestTool", "CodeExecuteTool",
    "JsonParseTool", "CsvParseTool", "Base64Tool",
    "DateTimeTool", "UuidTool", "HashTool", "PathOpsTool", "ZipTool",
    "SqliteQueryTool", "AnnotationNode",
    "ScreenCaptureTool", "MouseTool", "KeyboardTool", "ClipboardTool",
]


def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry with all built-in tools registered (19 tools)."""
    registry = ToolRegistry()
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(FileListTool())
    registry.register(TerminalTool())
    registry.register(GitTool())
    registry.register(TextProcessTool())
    registry.register(TextSearchTool())
    registry.register(HttpRequestTool())
    registry.register(CodeExecuteTool())
    registry.register(JsonParseTool())
    registry.register(CsvParseTool())
    registry.register(Base64Tool())
    registry.register(DateTimeTool())
    registry.register(UuidTool())
    registry.register(HashTool())
    registry.register(PathOpsTool())
    registry.register(ZipTool())
    registry.register(SqliteQueryTool())
    registry.register(AnnotationNode())
    registry.register(ScreenCaptureTool())
    registry.register(MouseTool())
    registry.register(KeyboardTool())
    registry.register(ClipboardTool())
    return registry