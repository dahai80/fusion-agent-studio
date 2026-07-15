"""File tools — read, write, and list files on the local filesystem."""

from __future__ import annotations

import os
from pathlib import Path

from .base import BaseTool


class FileReadTool(BaseTool):
    """Read the contents of a file."""

    name = "file_read"
    description = "Read the contents of a file at the given path"
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the file to read",
        },
        "encoding": {
            "type": "string",
            "description": "File encoding (default: utf-8)",
            "default": "utf-8",
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        encoding = kwargs.get("encoding", "utf-8")

        if not path:
            return "Error: path is required"

        filepath = Path(path).expanduser().resolve()

        if not filepath.exists():
            return f"Error: File not found: {filepath}"
        if not filepath.is_file():
            return f"Error: Not a file: {filepath}"

        try:
            content = filepath.read_text(encoding=encoding)
            return content
        except PermissionError:
            return f"Error: Permission denied: {filepath}"
        except UnicodeDecodeError:
            return f"Error: Cannot decode file with encoding {encoding}"
        except Exception as e:
            return f"Error reading file: {e}"


class FileWriteTool(BaseTool):
    """Write content to a file."""

    name = "file_write"
    description = "Write content to a file at the given path"
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the file to write",
        },
        "content": {
            "type": "string",
            "description": "Content to write to the file",
        },
        "encoding": {
            "type": "string",
            "description": "File encoding (default: utf-8)",
            "default": "utf-8",
        },
        "append": {
            "type": "boolean",
            "description": "Append to file instead of overwriting",
            "default": False,
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        content = kwargs.get("content", "")
        encoding = kwargs.get("encoding", "utf-8")
        append = kwargs.get("append", False)

        if not path:
            return "Error: path is required"

        filepath = Path(path).expanduser().resolve()

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(filepath, mode, encoding=encoding) as f:
                f.write(content)
            size = len(content.encode(encoding))
            action = "Appended to" if append else "Written to"
            return f"{action} {filepath} ({size} bytes)"
        except PermissionError:
            return f"Error: Permission denied: {filepath}"
        except Exception as e:
            return f"Error writing file: {e}"


class FileListTool(BaseTool):
    """List files and directories at a given path."""

    name = "file_list"
    description = "List files and directories at the given path"
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the directory to list",
        },
        "pattern": {
            "type": "string",
            "description": "Optional glob pattern to filter (e.g., '*.py')",
            "default": "",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return",
            "default": 50,
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", ".")
        pattern = kwargs.get("pattern", "")
        max_results = int(kwargs.get("max_results", 50))

        dirpath = Path(path).expanduser().resolve()

        if not dirpath.exists():
            return f"Error: Path not found: {dirpath}"
        if not dirpath.is_dir():
            return f"Error: Not a directory: {dirpath}"

        try:
            if pattern:
                items = list(dirpath.glob(pattern))
            else:
                items = sorted(dirpath.iterdir())

            # Limit results
            items = items[:max_results]

            result_lines = []
            for item in items:
                if item.is_dir():
                    result_lines.append(f"[DIR]  {item.name}")
                elif item.is_file():
                    size = item.stat().st_size
                    result_lines.append(f"[FILE] {item.name} ({size} bytes)")
                else:
                    result_lines.append(f"[OTHER] {item.name}")

            if not result_lines:
                return f"Empty directory: {dirpath}"

            total = len(items)
            prefix = f"Contents of {dirpath} ({total} items):\n"
            return prefix + "\n".join(result_lines)

        except PermissionError:
            return f"Error: Permission denied: {dirpath}"
        except Exception as e:
            return f"Error listing directory: {e}"