"""Code execution tool — run Python code in a subprocess for sandbox safety."""
from __future__ import annotations

import asyncio
import sys

from .base import BaseTool


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
        # Run in a subprocess for sandbox isolation
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c",
                f"import sys; sys.stdout.write(''); exec({code!r})",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace").strip()
            if stderr:
                err = stderr.decode("utf-8", errors="replace").strip()
                if err:
                    output = f"{output}\n[STDERR]\n{err}" if output else err
            return output if output else "(no output)"
        except asyncio.TimeoutError:
            proc.kill() if proc.returncode is None else None
            return f"Error: Code execution timed out after {timeout}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"