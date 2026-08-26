"""File tools — read, write, edit, delete, list, grep, glob on the local filesystem."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from .base import BaseTool

logger = logging.getLogger(__name__)

# 审计 A-3: 写 sink (file_write/edit/delete) 默认挡破坏性系统路径, 避免 LLM
# 经 file 工具覆写/删除 ssh 密钥 / 系统配置 / 凭证目录. 读 sink 不挡 (terminal
# 本就能读, 读非破坏). env FUSION_FILE_ALLOW_SYSTEM=1 显式放开 (受控场景).
# 可选白名单根目录 env FUSION_FILE_ROOTS (冒号分隔), 设了则写仅限这些根内.
_SYSTEM_PATH_PREFIXES = (
    "/etc/", "/System/", "/Library/", "/usr/", "/private/etc/",
    "/bin/", "/sbin/",
    "/opt/", "/root/",
)
# 审计 E-9: 原子串匹配 (`part in sparts`) 假阳性 — `.ssh-backup` 含 `.ssh` 被挡,
# `credentials_backup` 含 `credentials` 被挡. 改路径分量精确匹配: 目录名精确相等
# (.ssh/.aws/...), 文件名精确相等 (id_rsa/credentials/...). 消假阳性, 不漏真敏感.
_CATASTROPHIC_DIR_PARTS = (
    ".ssh", ".aws", ".gnupg", ".config", ".kube",
)
_CATASTROPHIC_FILE_NAMES = (
    "id_rsa", "id_ed25519", "credentials", ".netrc",
    "id_dsa", "id_ecdsa", ".env",
)
# 审计 E-9: ~/Library/LaunchAgents|LaunchDaemons 解析成 /Users/<u>/Library/...,
# startswith("/Library/") 为 False -> 不挡 -> LaunchAgent 持久化可写.
# 用分量名精确匹配 LaunchAgents/LaunchDaemons (不分系统/用户 Library, 均挡写).
_CATASTROPHIC_PERSIST_DIRS = (
    "LaunchAgents", "LaunchDaemons",
)


def _check_file_size(filepath: Path) -> tuple[bool, int, int]:
    # 审计 P1-12/E-19: file_edit/file_grep 复用 file_read 的大小预检.
    # 返回 (ok, size, max_bytes). ok=False 表示超限应拒. 读 stat 不读内容.
    try:
        size = filepath.stat().st_size
    except OSError:
        return True, 0, 0
    max_bytes_env = os.environ.get("FUSION_FILE_MAX_BYTES", "").strip()
    try:
        max_bytes = int(max_bytes_env) if max_bytes_env else 1024 * 1024
    except ValueError:
        max_bytes = 1024 * 1024
    if max_bytes > 0 and size > max_bytes:
        return False, size, max_bytes
    return True, size, max_bytes


def _is_write_blocked(filepath: Path) -> str | None:
    # 返回拦截原因 str, None=放行.
    roots_env = os.environ.get("FUSION_FILE_ROOTS", "").strip()
    if roots_env:
        allowed_roots = [Path(r).expanduser().resolve() for r in roots_env.split(":") if r.strip()]
        if allowed_roots and not any(
            str(filepath) == str(r) or str(filepath).startswith(str(r) + os.sep) for r in allowed_roots
        ):
            return f"path outside FUSION_FILE_ROOTS allowlist: {filepath}"
    allow_system = os.environ.get("FUSION_FILE_ALLOW_SYSTEM", "").strip().lower() in ("1", "true", "yes")
    if allow_system:
        return None
    spath = str(filepath)
    for prefix in _SYSTEM_PATH_PREFIXES:
        if spath.startswith(prefix):
            return f"system path blocked by default ({prefix}...): {filepath}"
    name = filepath.name
    parts = filepath.parts
    # E-9: 分量精确匹配, 消子串假阳性.
    for part in parts:
        if part in _CATASTROPHIC_DIR_PARTS:
            return f"sensitive path blocked by default ({part}): {filepath}"
        if part in _CATASTROPHIC_PERSIST_DIRS:
            return f"persistence path blocked by default ({part}): {filepath}"
    if name in _CATASTROPHIC_FILE_NAMES:
        return f"sensitive path blocked by default ({name}): {filepath}"
    return None


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

        # 审计 E-19/P0-4: file_read 原全量 read_text 进内存, 无上限 -> 读 10GB
        # 日志撑爆 daemon + LLM context + checkpoint 序列化. 先 stat 预检大小,
        # 超 max_bytes (默认 1MB, FUSION_FILE_MAX_BYTES 调) 拒绝, 避免读入.
        # 命中仍部分返回: 仅读前 max_bytes 字节, 标 truncated.
        try:
            size = filepath.stat().st_size
        except OSError as e:
            return f"Error stat file: {e}"
        max_bytes_env = os.environ.get("FUSION_FILE_MAX_BYTES", "").strip()
        try:
            max_bytes = int(max_bytes_env) if max_bytes_env else 1024 * 1024
        except ValueError:
            max_bytes = 1024 * 1024
        if max_bytes > 0 and size > max_bytes:
            logger.warning(
                "file_read blocked large file path=%s size=%d max=%d",
                filepath, size, max_bytes,
            )
            return (
                f"Error: File too large ({size} bytes > max {max_bytes}). "
                f"Set FUSION_FILE_MAX_BYTES higher, or read in chunks via terminal/grep."
            )

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

        blocked = _is_write_blocked(filepath)
        if blocked:
            logger.warning("file_write blocked: %s", blocked)
            return f"Error: {blocked} (set FUSION_FILE_ALLOW_SYSTEM=1 to allow)"

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


class FileEditTool(BaseTool):
    """Edit a file by replacing a unique old_string with new_string (in-place)."""

    name = "file_edit"
    description = (
        "Edit a file by replacing an exact old_string with new_string. "
        "old_string must appear exactly once unless replace_all is set. "
        "Fails if old_string is not found or matches multiple times."
    )
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the file to edit",
        },
        "old_string": {
            "type": "string",
            "description": "Exact text to replace (must match file content exactly, including whitespace)",
        },
        "new_string": {
            "type": "string",
            "description": "Text to insert in place of old_string",
        },
        "replace_all": {
            "type": "boolean",
            "description": "Replace all occurrences instead of requiring a unique match",
            "default": False,
        },
        "encoding": {
            "type": "string",
            "description": "File encoding (default: utf-8)",
            "default": "utf-8",
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        old_string = kwargs.get("old_string", "")
        new_string = kwargs.get("new_string", "")
        replace_all = kwargs.get("replace_all", False)
        encoding = kwargs.get("encoding", "utf-8")

        if not path:
            return "Error: path is required"
        if not old_string:
            return "Error: old_string is required"
        if old_string == new_string:
            return "Error: old_string and new_string are identical — nothing to change"

        filepath = Path(path).expanduser().resolve()

        blocked = _is_write_blocked(filepath)
        if blocked:
            logger.warning("file_edit blocked: %s", blocked)
            return f"Error: {blocked} (set FUSION_FILE_ALLOW_SYSTEM=1 to allow)"

        if not filepath.exists():
            return f"Error: File not found: {filepath}"
        if not filepath.is_file():
            return f"Error: Not a file: {filepath}"

        # 审计 P1-12/E-19: file_edit 原直接 read_text 无大小预检, 读 10GB 文件
        # 撑爆内存. 复用 _check_file_size 预检 (默认 1MB, FUSION_FILE_MAX_BYTES 调).
        ok, size, max_bytes = _check_file_size(filepath)
        if not ok:
            logger.warning("file_edit blocked large file path=%s size=%d max=%d", filepath, size, max_bytes)
            return (
                f"Error: File too large ({size} bytes > max {max_bytes}). "
                f"Set FUSION_FILE_MAX_BYTES higher, or edit via terminal/sed."
            )

        try:
            content = filepath.read_text(encoding=encoding)
            count = content.count(old_string)
            if count == 0:
                return (
                    f"Error: old_string not found in {filepath}. "
                    "Ensure it matches file content exactly (whitespace, indentation)."
                )
            if count > 1 and not replace_all:
                return (
                    f"Error: old_string matches {count} times in {filepath}. "
                    "Provide a longer unique old_string, or set replace_all=true."
                )

            if replace_all:
                new_content = content.replace(old_string, new_string)
                replaced = count
            else:
                new_content = content.replace(old_string, new_string, 1)
                replaced = 1

            filepath.write_text(new_content, encoding=encoding)
            logger.info(
                "file_edit: %s replaced %d occurrence(s) (%d->%d bytes)",
                filepath,
                replaced,
                len(content.encode(encoding)),
                len(new_content.encode(encoding)),
            )
            return f"Edited {filepath}: replaced {replaced} occurrence(s)"
        except PermissionError:
            return f"Error: Permission denied: {filepath}"
        except UnicodeDecodeError:
            return f"Error: Cannot decode file with encoding {encoding}"
        except Exception as e:
            return f"Error editing file: {e}"


class FileDeleteTool(BaseTool):
    """Delete a single file from the filesystem (refuses directories)."""

    name = "file_delete"
    description = (
        "Delete a single file at the given path. Refuses to delete directories."
    )
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the file to delete",
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        if not path:
            return "Error: path is required"

        filepath = Path(path).expanduser().resolve()

        blocked = _is_write_blocked(filepath)
        if blocked:
            logger.warning("file_delete blocked: %s", blocked)
            return f"Error: {blocked} (set FUSION_FILE_ALLOW_SYSTEM=1 to allow)"

        if not filepath.exists():
            return f"Error: File not found: {filepath}"
        if filepath.is_dir():
            return (
                f"Error: {filepath} is a directory. file_delete refuses directories "
                "to avoid recursive deletion. Remove files individually."
            )

        try:
            filepath.unlink()
            logger.info("file_delete: removed %s", filepath)
            return f"Deleted {filepath}"
        except PermissionError:
            return f"Error: Permission denied: {filepath}"
        except Exception as e:
            return f"Error deleting file: {e}"


class FileGrepTool(BaseTool):
    """Recursively search file contents under a directory for a pattern."""

    name = "file_grep"
    description = (
        "Recursively search file contents under a directory for a regex or plain "
        "pattern. Returns file:line:match entries with optional context lines."
    )
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the directory to search under",
        },
        "pattern": {
            "type": "string",
            "description": "Search pattern (regex unless use_regex is false)",
        },
        "use_regex": {
            "type": "boolean",
            "description": "Treat pattern as a regex (default: true)",
            "default": True,
        },
        "case_insensitive": {
            "type": "boolean",
            "description": "Case-insensitive matching (default: false)",
            "default": False,
        },
        "context": {
            "type": "integer",
            "description": "Number of context lines to show before and after each match (default: 0)",
            "default": 0,
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return (default: 50)",
            "default": 50,
        },
        "include": {
            "type": "string",
            "description": "Optional glob to filter files by name, e.g. '*.py' (default: all files)",
            "default": "",
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        pattern = kwargs.get("pattern", "")
        use_regex = kwargs.get("use_regex", True)
        case_insensitive = kwargs.get("case_insensitive", False)
        context = int(kwargs.get("context", 0))
        max_results = int(kwargs.get("max_results", 50))
        include = kwargs.get("include", "")

        if not path:
            return "Error: path is required"
        if not pattern:
            return "Error: pattern is required"

        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"Error: Path not found: {root}"
        if not root.is_dir():
            return f"Error: Not a directory: {root}"

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            if use_regex:
                regex = re.compile(pattern, flags)
            else:
                regex = re.compile(re.escape(pattern), flags)
        except re.error as e:
            return f"Error in regex pattern: {e}"

        matches = []
        try:
            for file_path in sorted(root.rglob(include or "*")):
                if not file_path.is_file():
                    continue
                if any(part.startswith(".") and part not in (".",) for part in file_path.relative_to(root).parts[:-1]):
                    continue
                # 审计 P1-12/E-19: rglob 逐文件 read_text 无大小预检, 遍历含大文件
                # 目录撑爆内存. 超限跳过该文件 (grep 已对权限错误静默跳过, 一致).
                g_ok, g_size, g_max = _check_file_size(file_path)
                if not g_ok:
                    logger.debug("file_grep skipped large file path=%s size=%d max=%d", file_path, g_size, g_max)
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except (PermissionError, OSError):
                    continue
                lines = text.split("\n")
                for idx, line in enumerate(lines):
                    if regex.search(line):
                        rel = file_path.relative_to(root)
                        entry = [f"{rel}:{idx + 1}: {line.rstrip()}"]
                        if context > 0:
                            ctx_start = max(0, idx - context)
                            ctx_end = min(len(lines), idx + context + 1)
                            for j in range(ctx_start, ctx_end):
                                if j == idx:
                                    continue
                                marker = "-" if j < idx else "+"
                                entry.append(f"  {marker}{j + 1}: {lines[j].rstrip()}")
                        matches.append("\n".join(entry))
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
        except PermissionError:
            return f"Error: Permission denied: {root}"
        except Exception as e:
            return f"Error searching: {e}"

        if not matches:
            return f"No matches found for pattern: {pattern}"

        summary = f"Found {len(matches)} match(es) for '{pattern}' under {root}"
        if len(matches) >= max_results:
            summary += f" (showing first {max_results}, more may exist)"
        logger.info("file_grep: %s -> %d match(es)", pattern, len(matches))
        return summary + "\n" + "\n--\n".join(matches)


class FileGlobTool(BaseTool):
    """Recursively find files matching a glob pattern."""

    name = "file_glob"
    description = (
        "Recursively find files matching a glob pattern under a directory. "
        "Returns a sorted list of matching file paths (directories excluded)."
    )
    parameters = {
        "path": {
            "type": "string",
            "description": "Absolute path to the directory to search under",
        },
        "pattern": {
            "type": "string",
            "description": "Glob pattern, e.g. '**/*.py' or '*.md' (default: '**/*')",
            "default": "**/*",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of paths to return (default: 100)",
            "default": 100,
        },
    }

    async def execute(self, **kwargs) -> str:
        path = kwargs.get("path", "")
        pattern = kwargs.get("pattern", "**/*")
        max_results = int(kwargs.get("max_results", 100))

        if not path:
            return "Error: path is required"

        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"Error: Path not found: {root}"
        if not root.is_dir():
            return f"Error: Not a directory: {root}"

        try:
            results = []
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    results.append(str(p.relative_to(root)))
                    if len(results) >= max_results:
                        break
        except Exception as e:
            return f"Error globbing: {e}"

        if not results:
            return f"No files matching '{pattern}' under {root}"

        summary = f"Found {len(results)} file(s) matching '{pattern}' under {root}"
        if len(results) >= max_results:
            summary += f" (showing first {max_results}, more may exist)"
        logger.info("file_glob: %s under %s -> %d file(s)", pattern, root, len(results))
        return summary + "\n" + "\n".join(results)