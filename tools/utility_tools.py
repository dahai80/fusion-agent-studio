"""Utility tools — date/time, UUID, hash, path operations."""
from __future__ import annotations
import uuid
import hashlib
import time
from datetime import datetime
from pathlib import Path
from .base import BaseTool


class DateTimeTool(BaseTool):
    name = "date_time"
    description = "Get current date/time, format timestamps, or calculate time differences"
    parameters = {
        "operation": {
            "type": "string", "enum": ["now", "format", "timestamp"],
            "description": "Operation: now=current datetime, format=format a timestamp, timestamp=unix timestamp",
        },
        "format": {"type": "string", "description": "Output format (strftime)", "default": "%Y-%m-%d %H:%M:%S"},
        "value": {"type": "number", "description": "Unix timestamp to format", "default": 0},
    }

    async def execute(self, **kwargs) -> str:
        op = kwargs.get("operation", "now")
        fmt = kwargs.get("format", "%Y-%m-%d %H:%M:%S")
        if op == "now":
            return datetime.now().strftime(fmt)
        if op == "timestamp":
            return str(int(time.time()))
        if op == "format":
            ts = float(kwargs.get("value", time.time()))
            return datetime.fromtimestamp(ts).strftime(fmt)
        return datetime.now().strftime(fmt)


class UuidTool(BaseTool):
    name = "uuid"
    description = "Generate UUIDs (universally unique identifiers)"
    parameters = {
        "count": {"type": "integer", "description": "Number of UUIDs to generate", "default": 1},
        "version": {"type": "integer", "enum": [4], "description": "UUID version (only v4 supported)", "default": 4},
    }

    async def execute(self, **kwargs) -> str:
        count = int(kwargs.get("count", 1))
        if count < 1:
            return "Error: count must be >= 1"
        if count > 100:
            return "Error: count must be <= 100"
        result = [str(uuid.uuid4()) for _ in range(count)]
        return "\n".join(result)


class HashTool(BaseTool):
    name = "hash"
    description = "Compute hash/digest of text using various algorithms"
    parameters = {
        "input": {"type": "string", "description": "Text to hash"},
        "algorithm": {
            "type": "string", "enum": ["md5", "sha1", "sha256", "sha512"],
            "description": "Hash algorithm", "default": "sha256",
        },
    }

    async def execute(self, **kwargs) -> str:
        input_str = kwargs.get("input", "")
        algo = kwargs.get("algorithm", "sha256")
        if not input_str:
            return "Error: input is required"
        data = input_str.encode()
        if algo == "md5":
            return hashlib.md5(data).hexdigest()
        elif algo == "sha1":
            return hashlib.sha1(data).hexdigest()
        elif algo == "sha256":
            return hashlib.sha256(data).hexdigest()
        elif algo == "sha512":
            return hashlib.sha512(data).hexdigest()
        return f"Error: Unknown algorithm: {algo}"


class PathOpsTool(BaseTool):
    name = "path_ops"
    description = "Perform filesystem path operations (join, resolve, absolute, parent, filename)"
    parameters = {
        "path": {"type": "string", "description": "Input path"},
        "operation": {
            "type": "string",
            "enum": ["absolute", "parent", "filename", "stem", "suffix", "exists", "is_file", "is_dir"],
            "description": "Path operation to perform",
        },
        "join": {"type": "string", "description": "Path segment to join (optional)", "default": ""},
    }

    async def execute(self, **kwargs) -> str:
        path_str = kwargs.get("path", ".")
        op = kwargs.get("operation", "absolute")
        join_str = kwargs.get("join", "")
        p = Path(path_str).expanduser()
        if join_str:
            p = p / join_str
        p = p.resolve()
        if op == "absolute":
            return str(p)
        if op == "parent":
            return str(p.parent)
        if op == "filename":
            return p.name
        if op == "stem":
            return p.stem
        if op == "suffix":
            return p.suffix
        if op == "exists":
            return str(p.exists())
        if op == "is_file":
            return str(p.is_file())
        if op == "is_dir":
            return str(p.is_dir())
        return str(p)


class ZipTool(BaseTool):
    name = "zip"
    description = "Compress or decompress files (ZIP format)"
    parameters = {
        "operation": {"type": "string", "enum": ["compress", "list"], "description": "Operation"},
        "source_path": {"type": "string", "description": "Path to file or directory to compress"},
        "output_path": {"type": "string", "description": "Output ZIP file path", "default": ""},
    }

    async def execute(self, **kwargs) -> str:
        op = kwargs.get("operation", "list")
        source = kwargs.get("source_path", "")
        if not source:
            return "Error: source_path is required"
        p = Path(source).expanduser().resolve()
        if not p.exists():
            return f"Error: Path not found: {p}"
        if op == "list":
            import zipfile
            if not p.is_file() or not p.suffix.lower() == ".zip":
                return f"Error: Not a ZIP file: {p}"
            try:
                with zipfile.ZipFile(p, "r") as zf:
                    names = zf.namelist()
                    total = len(names)
                    info = []
                    for name in names[:50]:
                        zi = zf.getinfo(name)
                        info.append(f"  {name} ({zi.file_size} bytes)")
                    if total > 50:
                        info.append(f"  ... ({total - 50} more files)")
                    return f"ZIP contents ({total} files):\n" + "\n".join(info)
            except Exception as e:
                return f"Error reading ZIP: {e}"
        return f"Error: Unknown operation: {op}"