"""Code execution tool — run Python code in a subprocess for sandbox safety."""

from __future__ import annotations

import asyncio
import logging
import os

from .base import BaseTool

logger = logging.getLogger(__name__)


class CodeExecuteTool(BaseTool):
    name = "code_execute"
    description = (
        "Execute Python code in a subprocess and return the output. Use print() to produce output."
    )
    parameters = {
        "code": {"type": "string", "description": "Python code to execute"},
        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 10},
        "workspace": {
            "type": "string",
            "description": "Execution backend: docker (isolated container) or local sandbox-exec (default: auto)",
            "default": "auto",
            "enum": ["auto", "docker", "local"],
        },
    }

    async def execute(self, **kwargs) -> str:
        code = kwargs.get("code", "")
        timeout = int(kwargs.get("timeout", 10))
        if not code:
            return "Error: code is required"
        # Audit A-3: the original implementation spawned a bare exec()
        # subprocess with full FS/net/proc access = no sandbox. Unify on
        # CodeSandbox (macOS sandbox-exec profile + Python AST safety checks),
        # the same isolation layer as CodeSandboxTool, removing the "dangerous
        # path used by default" gap. use_sandbox defaults to True; non-macOS
        # degrades automatically but still passes AST checks.
        # #495: workspace parameter selects an optional container backend
        # (falls back to sandbox-exec when Docker is absent).
        try:
            from agent_runtime.code_sandbox import CodeSandbox

            sandbox = CodeSandbox(
                timeout=timeout,
                use_sandbox=True,
                workspace=str(kwargs.get("workspace", "auto")),
            )
            logger.info("code_execute: rerouted to CodeSandbox timeout=%s", timeout)
            result = await asyncio.to_thread(sandbox.execute, code, "python")
        except asyncio.TimeoutError:
            return f"Error: Code execution timed out after {timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        if result.timed_out:
            return (
                f"Error: Code execution timed out after {timeout}s (exec_id={result.execution_id})"
            )
        if not result.success:
            err = result.stderr.strip() if result.stderr else f"exit code {result.exit_code}"
            return f"Error: {err} (exec_id={result.execution_id})"
        output = result.stdout.strip()
        if result.stderr.strip():
            output = (
                f"{output}\n[STDERR]\n{result.stderr.strip()}" if output else result.stderr.strip()
            )
        logger.info(
            "code_execute: exit=%d exec_id=%s stdout=%d bytes",
            result.exit_code,
            result.execution_id,
            len(result.stdout),
        )
        return output if output else f"(no output, exec_id={result.execution_id})"


class CodeSandboxTool(BaseTool):
    """Execute code in macOS sandbox-exec isolation with AST safety checks.

    Wraps agent_runtime.code_sandbox.CodeSandbox (8 languages, sandbox-exec
    profile, Python AST analysis). Stronger isolation than CodeExecuteTool.
    """

    name = "code_sandbox"
    description = (
        "Execute code in macOS sandbox-exec isolation with AST safety checks. "
        "Supports python/shell/bash/javascript/swift/go/cpp/c. "
        "Python code is AST-checked for dangerous imports/calls before running."
    )
    parameters = {
        "code": {
            "type": "string",
            "description": "Code to execute",
        },
        "language": {
            "type": "string",
            "description": "Language: python/shell/bash/javascript/swift/go/cpp/c (default: python)",
            "default": "python",
            "enum": [
                "python",
                "shell",
                "bash",
                "javascript",
                "swift",
                "go",
                "cpp",
                "c",
            ],
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 30)",
            "default": 30,
        },
        "use_sandbox": {
            "type": "boolean",
            "description": "Use sandbox-exec isolation (default: true). Set false to bypass on non-macOS.",
            "default": True,
        },
        "workspace": {
            "type": "string",
            "description": "Execution backend: docker (isolated container) or local sandbox-exec (default: auto)",
            "default": "auto",
            "enum": ["auto", "docker", "local"],
        },
    }

    async def execute(self, **kwargs) -> str:
        code = kwargs.get("code", "")
        language = kwargs.get("language", "python")
        timeout = int(kwargs.get("timeout", 30))
        # Audit E-10/P0-6: `use_sandbox` was passed through via kwargs.get ->
        # the LLM could send use_sandbox=False to bypass sandbox-exec and fall
        # back to bare bash/python with full FS+network access and no AST
        # checks (non-Python has no AST anyway). Server forces True and ignores
        # the LLM parameter.
        # FUSION_CODE_NOSANDBOX=1 is the opt-out for controlled environments
        # only (same pattern as the A-3 catastrophic blocklist).
        requested = kwargs.get("use_sandbox", True)
        if requested is False:
            logger.warning(
                "code_sandbox: LLM requested use_sandbox=False — ignored (E-10 server-enforce)"
            )
        allow_nosandbox = os.environ.get("FUSION_CODE_NOSANDBOX", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        use_sandbox = False if allow_nosandbox else True

        if not code:
            return "Error: code is required"

        try:
            from agent_runtime.code_sandbox import CodeSandbox

            sandbox = CodeSandbox(
                timeout=timeout,
                use_sandbox=use_sandbox,
                workspace=str(kwargs.get("workspace", "auto")),
            )
            logger.info(
                "code_sandbox: lang=%s timeout=%s sandbox=%s workspace=%s",
                language,
                timeout,
                use_sandbox,
                kwargs.get("workspace", "auto"),
            )
            result = await asyncio.to_thread(sandbox.execute, code, language)
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

        if result.timed_out:
            return (
                f"Error: Code execution timed out after {timeout}s (exec_id={result.execution_id})"
            )
        if not result.success:
            err = result.stderr.strip() if result.stderr else f"exit code {result.exit_code}"
            return f"Error: {err} (exec_id={result.execution_id})"

        output = result.stdout.strip()
        if result.stderr.strip():
            output = (
                f"{output}\n[STDERR]\n{result.stderr.strip()}" if output else result.stderr.strip()
            )
        logger.info(
            "code_sandbox: exit=%d exec_id=%s stdout=%d bytes",
            result.exit_code,
            result.execution_id,
            len(result.stdout),
        )
        return output if output else f"(no output, exec_id={result.execution_id})"
