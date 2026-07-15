"""Terminal tool — execute shell commands on the local system."""

from __future__ import annotations

import asyncio
import shlex

from .base import BaseTool


class TerminalTool(BaseTool):
    """Execute shell commands in a subprocess."""

    name = "terminal"
    description = "Execute a shell command and return its output"
    parameters = {
        "command": {
            "type": "string",
            "description": "Shell command to execute",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 30)",
            "default": 30,
        },
        "workdir": {
            "type": "string",
            "description": "Working directory (default: current directory)",
            "default": "",
        },
    }

    async def execute(self, **kwargs) -> str:
        command = kwargs.get("command", "")
        timeout = int(kwargs.get("timeout", 30))
        workdir = kwargs.get("workdir", "")

        if not command:
            return "Error: command is required"

        if len(command) > 10000:
            return "Error: command too long (max 10000 characters)"

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir if workdir else None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Command timed out after {timeout}s"

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(f"[STDERR]\n{stderr.decode('utf-8', errors='replace')}")

            result = "".join(output_parts).strip()

            if proc.returncode != 0:
                prefix = f"Command exited with code {proc.returncode}"
                if result:
                    return f"{prefix}:\n{result}"
                return prefix

            return result if result else f"Command completed (exit code 0, no output)"

        except FileNotFoundError:
            return f"Error: Command not found: {command.split()[0]}"
        except Exception as e:
            return f"Error executing command: {e}"