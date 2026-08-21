"""Built-in tools for agent execution."""

import logging

logger = logging.getLogger(__name__)

from .artifact_fc_tools import (
    ArtifactContextBudgetTool,
    ArtifactCreateSnapshotTool,
    ArtifactCreateTool,
    ArtifactGetSourceTool,
    ArtifactListAllTool,
    ArtifactLoadTool,
    ArtifactPatchTool,
    ArtifactUpdateTool,
)
from .base import BaseTool, ToolResult
from .code_tools import CodeExecuteTool, CodeSandboxTool
from .computer_use_tools import (
    ClipboardTool,
    KeyboardTool,
    MouseTool,
    ScreenCaptureTool,
)
from .data_tools import Base64Tool, CsvParseTool, JsonParseTool
from .db_tools import AnnotationNode, SqliteQueryTool
from .file_tools import (
    FileDeleteTool,
    FileEditTool,
    FileGlobTool,
    FileGrepTool,
    FileListTool,
    FileReadTool,
    FileWriteTool,
)
from .git_tools import GitTool
from .http_tools import HttpRequestTool
from .mcp_tool import MCPRegistry, MCPTool
from .plan_tools import EXIT_PLAN_MODE_SENTINEL, ExitPlanModeTool
from .registry import ToolRegistry
from .terminal_tools import TerminalTool
from .text_tools import TextProcessTool, TextSearchTool
from .utility_tools import DateTimeTool, HashTool, PathOpsTool, UuidTool, ZipTool

__all__ = [
    "AnnotationNode",
    "ArtifactContextBudgetTool",
    "ArtifactCreateSnapshotTool",
    "ArtifactCreateTool",
    "ArtifactGetSourceTool",
    "ArtifactListAllTool",
    "ArtifactLoadTool",
    "ArtifactPatchTool",
    "ArtifactUpdateTool",
    "Base64Tool",
    "BaseTool",
    "ClipboardTool",
    "CodeExecuteTool",
    "CodeSandboxTool",
    "CsvParseTool",
    "DateTimeTool",
    "EXIT_PLAN_MODE_SENTINEL",
    "ExitPlanModeTool",
    "FileDeleteTool",
    "FileEditTool",
    "FileGlobTool",
    "FileGrepTool",
    "FileListTool",
    "FileReadTool",
    "FileWriteTool",
    "GitTool",
    "HashTool",
    "HttpRequestTool",
    "JsonParseTool",
    "KeyboardTool",
    "MCPRegistry",
    "MCPTool",
    "MouseTool",
    "PathOpsTool",
    "ScreenCaptureTool",
    "SqliteQueryTool",
    "TerminalTool",
    "TextProcessTool",
    "TextSearchTool",
    "ToolRegistry",
    "ToolResult",
    "UuidTool",
    "ZipTool",
]


def create_default_registry() -> ToolRegistry:
    """Create a ToolRegistry with all built-in tools registered (37 tools)."""
    registry = ToolRegistry()
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(FileEditTool())
    registry.register(FileDeleteTool())
    registry.register(FileListTool())
    registry.register(FileGrepTool())
    registry.register(FileGlobTool())
    registry.register(TerminalTool())
    registry.register(GitTool())
    registry.register(TextProcessTool())
    registry.register(TextSearchTool())
    registry.register(HttpRequestTool())
    registry.register(CodeExecuteTool())
    registry.register(CodeSandboxTool())
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
    registry.register(ArtifactGetSourceTool())
    registry.register(ArtifactCreateTool())
    registry.register(ArtifactUpdateTool())
    registry.register(ArtifactCreateSnapshotTool())
    registry.register(ArtifactListAllTool())
    registry.register(ArtifactPatchTool())
    registry.register(ArtifactLoadTool())
    registry.register(ArtifactContextBudgetTool())
    registry.register(ExitPlanModeTool())
    from .plugin_manager import PluginManager
    _pm = PluginManager(registry)
    _pm.load_all()
    if _pm.loaded_count:
        logger.info("create_default_registry loaded %d plugin tools", _pm.loaded_count)
    if _pm.failed_plugins:
        registry.failed_plugins = _pm.failed_plugins
    return registry