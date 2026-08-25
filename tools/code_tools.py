"""Code execution tool — run Python code in a subprocess for sandbox safety."""
from __future__ import annotations

import asyncio
import logging
import os

from .base import BaseTool

logger = logging.getLogger(__name__)


class CodeExecuteTool(BaseTool):
    name = "code_execute"
    description = "Execute Python code in a subprocess and return the output. Use print() to produce output."
    parameters = {
        "code": {"type": "string", "description": "Python code to execute"},
        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 10},
    }

    async def execute(self, **kwargs) -> str:
        code = kwargs.get("code", "")
        timeout = int(kwargs.get("timeout", 10))
        if not code:
            return "Error: code is required"
        # 审计 A-3: 原实现裸 exec() 子进程有完整 FS/net/proc 访问 = 未沙箱.
        # 统一走 CodeSandbox (macOS sandbox-exec profile + Python AST 安全
        # 检查), 与 CodeSandboxTool 同一隔离层, 消除"危险的那套被默认用".
        # use_sandbox 默认 True; 非 macOS 自动降级但仍过 AST 检查.
        try:
            from agent_runtime.code_sandbox import CodeSandbox
            sandbox = CodeSandbox(timeout=timeout, use_sandbox=True)
            logger.info("code_execute: rerouted to CodeSandbox timeout=%s", timeout)
            result = await asyncio.to_thread(sandbox.execute, code, "python")
        except asyncio.TimeoutError:
            return f"Error: Code execution timed out after {timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        if result.timed_out:
            return f"Error: Code execution timed out after {timeout}s (exec_id={result.execution_id})"
        if not result.success:
            err = result.stderr.strip() if result.stderr else f"exit code {result.exit_code}"
            return f"Error: {err} (exec_id={result.execution_id})"
        output = result.stdout.strip()
        if result.stderr.strip():
            output = f"{output}\n[STDERR]\n{result.stderr.strip()}" if output else result.stderr.strip()
        logger.info(
            "code_execute: exit=%d exec_id=%s stdout=%d bytes",
            result.exit_code, result.execution_id, len(result.stdout),
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
                "python", "shell", "bash", "javascript",
                "swift", "go", "cpp", "c",
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
    }

    async def execute(self, **kwargs) -> str:
        code = kwargs.get("code", "")
        language = kwargs.get("language", "python")
        timeout = int(kwargs.get("timeout", 30))
        # 审计 E-10/P0-6: `use_sandbox` 原 kwargs.get 透传 -> LLM 可传
        # use_sandbox=False 绕 sandbox-exec, 退回裸 bash/python 全 FS+网络访问
        # 无 AST 检查 (非 Python 本就无 AST). 服务端强制 True, 忽略 LLM 参数.
        # FUSION_CODE_NOSANDBOX=1 仅受控环境 opt-out (与 A-3 灾难黑名单同模式).
        requested = kwargs.get("use_sandbox", True)
        if requested is False:
            logger.warning(
                "code_sandbox: LLM requested use_sandbox=False — ignored (E-10 server-enforce)"
            )
        allow_nosandbox = os.environ.get(
            "FUSION_CODE_NOSANDBOX", ""
        ).strip().lower() in ("1", "true", "yes")
        use_sandbox = False if allow_nosandbox else True

        if not code:
            return "Error: code is required"

        try:
            from agent_runtime.code_sandbox import CodeSandbox

            sandbox = CodeSandbox(timeout=timeout, use_sandbox=use_sandbox)
            logger.info(
                "code_sandbox: lang=%s timeout=%s sandbox=%s",
                language, timeout, use_sandbox,
            )
            result = await asyncio.to_thread(
                sandbox.execute, code, language
            )
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

        if result.timed_out:
            return f"Error: Code execution timed out after {timeout}s (exec_id={result.execution_id})"
        if not result.success:
            err = result.stderr.strip() if result.stderr else f"exit code {result.exit_code}"
            return f"Error: {err} (exec_id={result.execution_id})"

        output = result.stdout.strip()
        if result.stderr.strip():
            output = f"{output}\n[STDERR]\n{result.stderr.strip()}" if output else result.stderr.strip()
        logger.info(
            "code_sandbox: exit=%d exec_id=%s stdout=%d bytes",
            result.exit_code, result.execution_id, len(result.stdout),
        )
        return output if output else f"(no output, exec_id={result.execution_id})"