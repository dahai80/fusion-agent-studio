"""Code sandbox — sandbox-exec isolation, AST analysis, diff preview.

Executes generated code in macOS sandbox-exec isolation, provides
AST-based safety checks before execution, and generates unified diffs
for preview before applying changes.
"""
from __future__ import annotations

import ast
import difflib
import logging
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SANDBOX_PROFILE = """
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "__SANDBOX_DIR__"))
(deny network*)
"""

DANGEROUS_IMPORTS = {
    "os", "subprocess", "shutil", "sys", "socket",
    "http", "urllib", "requests", "pickle", "ctypes",
    "multiprocessing", "threading", "signal",
}

DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__",
    "open", "input", "breakpoint",
}

DANGEROUS_ATTRIBUTES = {
    "__subclasses__", "__bases__", "__mro__", "__globals__",
    "__builtins__", "__code__", "__func__",
}


@dataclass
class ASTAnalysis:
    safe: bool = True
    issues: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    function_calls: list[str] = field(default_factory=list)
    has_network: bool = False
    has_file_write: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "issues": self.issues,
            "imports": self.imports,
            "function_calls": self.function_calls,
            "has_network": self.has_network,
            "has_file_write": self.has_file_write,
        }


@dataclass
class DiffResult:
    file_path: str = ""
    original: str = ""
    modified: str = ""
    diff: str = ""
    additions: int = 0
    deletions: int = 0
    has_changes: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "additions": self.additions,
            "deletions": self.deletions,
            "has_changes": self.has_changes,
            "diff": self.diff,
        }


@dataclass
class SandboxResult:
    success: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    execution_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "execution_id": self.execution_id,
        }


class ASTChecker:
    """Analyze Python code AST for safety violations."""

    def analyze(self, code: str) -> ASTAnalysis:
        result = ASTAnalysis()
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            result.safe = False
            result.issues.append(f"Syntax error: {e}")
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    result.imports.append(alias.name)
                    if mod in DANGEROUS_IMPORTS:
                        result.issues.append(f"Dangerous import: {alias.name}")
                        result.safe = False

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module.split(".")[0]
                    result.imports.append(node.module)
                    if mod in DANGEROUS_IMPORTS:
                        result.issues.append(f"Dangerous import: {node.module}")
                        result.safe = False

            elif isinstance(node, ast.Call):
                name = self._get_call_name(node)
                if name:
                    result.function_calls.append(name)
                    if name in DANGEROUS_CALLS:
                        result.issues.append(f"Dangerous call: {name}")
                        result.safe = False

            if isinstance(node, ast.Attribute):
                attr = node.attr
                if attr in ("write", "writelines"):
                    result.has_file_write = True
                if attr in ("connect", "send", "sendto", "bind"):
                    result.has_network = True
                if attr in DANGEROUS_ATTRIBUTES:
                    result.issues.append(f"Unsafe attribute access: {attr}")
                    result.safe = False

        logger.info("AST analysis: safe=%s issues=%d imports=%d", result.safe, len(result.issues), len(result.imports))
        return result

    def _get_call_name(self, node: ast.Call) -> str:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""


class DiffPreview:
    """Generate unified diffs for file change preview."""

    def diff(self, original: str, modified: str, file_path: str = "file") -> DiffResult:
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            orig_lines, mod_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        ))
        diff_text = "".join(diff_lines)
        additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        has_changes = bool(diff_text)

        logger.debug("Diff: %s +%d/-%d lines", file_path, additions, deletions)
        return DiffResult(
            file_path=file_path,
            original=original,
            modified=modified,
            diff=diff_text,
            additions=additions,
            deletions=deletions,
            has_changes=has_changes,
        )

    def diff_files(self, original_path: str | Path, modified_path: str | Path) -> DiffResult:
        orig = Path(original_path).read_text(encoding="utf-8", errors="replace")
        mod = Path(modified_path).read_text(encoding="utf-8", errors="replace")
        return self.diff(orig, mod, file_path=Path(original_path).name)


class CodeSandbox:
    """Execute code in macOS sandbox-exec isolation."""

    def __init__(self, timeout: int = 30, use_sandbox: bool = True):
        self.timeout = timeout
        self.use_sandbox = use_sandbox
        self._ast_checker = ASTChecker()
        self._diff_preview = DiffPreview()

    @property
    def ast_checker(self) -> ASTChecker:
        return self._ast_checker

    @property
    def diff_preview(self) -> DiffPreview:
        return self._diff_preview

    def check_safety(self, code: str) -> ASTAnalysis:
        return self._ast_checker.analyze(code)

    def execute(self, code: str, language: str = "python") -> SandboxResult:
        exec_id = uuid.uuid4().hex[:8]
        analysis = self._ast_checker.analyze(code)
        if not analysis.safe:
            logger.warning("Code safety check FAILED for exec %s: %s", exec_id, analysis.issues)
            return SandboxResult(
                success=False,
                exit_code=-1,
                stderr=f"Safety check failed: {'; '.join(analysis.issues)}",
                execution_id=exec_id,
            )

        with tempfile.TemporaryDirectory(prefix=f"sandbox_{exec_id}_") as tmpdir:
            if language == "python":
                return self._execute_python(code, tmpdir, exec_id)
            return SandboxResult(
                success=False,
                stderr=f"Unsupported language: {language}",
                execution_id=exec_id,
            )

    def _execute_python(self, code: str, work_dir: str, exec_id: str) -> SandboxResult:
        script_path = Path(work_dir) / "script.py"
        script_path.write_text(code, encoding="utf-8")

        if self.use_sandbox:
            profile = SANDBOX_PROFILE.replace("__SANDBOX_DIR__", work_dir)
            profile_path = Path(work_dir) / "sandbox.sb"
            profile_path.write_text(profile)
            logger.info("Sandbox profile written: %s", profile_path)
            cmd = ["sandbox-exec", "-f", str(profile_path), sys.executable, str(script_path)]
        else:
            cmd = [sys.executable, str(script_path)]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=work_dir,
            )
            success = proc.returncode == 0
            logger.info("Sandbox exec %s: exit=%d success=%s", exec_id, proc.returncode, success)
            return SandboxResult(
                success=success,
                exit_code=proc.returncode,
                stdout=proc.stdout[:10000],
                stderr=proc.stderr[:5000],
                execution_id=exec_id,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Sandbox exec %s: TIMEOUT (%ds)", exec_id, self.timeout)
            return SandboxResult(
                success=False,
                timed_out=True,
                execution_id=exec_id,
            )
        except Exception as e:
            logger.error("Sandbox exec %s: ERROR %s", exec_id, e)
            return SandboxResult(
                success=False,
                stderr=str(e),
                execution_id=exec_id,
            )
