"""Fusion-code bridge — subprocess client for fusion-code agent.

Launches fusion-code as a subprocess, sends tasks, parses results.
Never imports fusion-code internals — only communicates via CLI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

FUSION_CODE_BIN = os.path.expanduser("~/fusion/fusion-code/fusion-code")


@dataclass
class CodeTask:
    """A task to send to fusion-code."""

    prompt: str
    working_dir: str = ""
    timeout: float = 300.0
    model: str = ""
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CodeResult:
    """Result from a fusion-code execution."""

    output: str
    exit_code: int
    duration: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class FusionCodeBridge:
    """Bridge to fusion-code CLI agent.

    Communicates via subprocess only — no direct imports.
    """

    def __init__(
        self,
        binary_path: str = FUSION_CODE_BIN,
        default_working_dir: str = "",
        default_timeout: float = 300.0,
    ):
        self.binary_path = binary_path
        self.default_working_dir = default_working_dir or os.getcwd()
        self.default_timeout = default_timeout
        self._process: asyncio.subprocess.Process | None = None

    async def execute(self, task: CodeTask) -> CodeResult:
        """Execute a task via fusion-code subprocess."""
        working_dir = task.working_dir or self.default_working_dir
        timeout = task.timeout or self.default_timeout

        cmd = self._build_command(task)
        logger.info("Executing fusion-code: %s (cwd=%s, timeout=%.0fs)", " ".join(cmd[:5]), working_dir, timeout)

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            self._process = proc

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            duration = time.time() - start

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            error_output = stderr.decode("utf-8", errors="replace") if stderr else ""

            logger.info("fusion-code exited %d in %.1fs (stdout=%d bytes)", proc.returncode, duration, len(output))

            return CodeResult(
                output=output,
                exit_code=proc.returncode or 0,
                duration=duration,
                error=error_output,
            )

        except asyncio.TimeoutError:
            logger.warning("fusion-code timed out after %.0fs", timeout)
            if self._process and self._process.returncode is None:
                self._process.kill()
            return CodeResult(
                output="",
                exit_code=-1,
                duration=time.time() - start,
                error=f"Timeout after {timeout}s",
            )
        except Exception as e:
            logger.exception("fusion-code execution failed")
            return CodeResult(
                output="",
                exit_code=-1,
                duration=time.time() - start,
                error=str(e),
            )
        finally:
            self._process = None

    async def execute_stream(self, task: CodeTask) -> AsyncIterator[str]:
        """Execute a task and stream output lines as they arrive."""
        working_dir = task.working_dir or self.default_working_dir
        _timeout = task.timeout or self.default_timeout

        cmd = self._build_command(task)
        logger.info("Streaming fusion-code: %s", " ".join(cmd[:5]))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            self._process = proc

            async def _read_lines():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    yield line.decode("utf-8", errors="replace").rstrip()

            async for line in _read_lines():
                yield line

            await asyncio.wait_for(proc.wait(), timeout=1.0)

        except asyncio.TimeoutError:
            if self._process and self._process.returncode is None:
                self._process.kill()
        except Exception:
            logger.exception("fusion-code stream failed")
            if self._process and self._process.returncode is None:
                self._process.kill()
        finally:
            self._process = None

    async def cancel(self) -> None:
        """Cancel the running fusion-code process."""
        if self._process and self._process.returncode is None:
            logger.info("Cancelling fusion-code process")
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()

    def is_available(self) -> bool:
        """Check if fusion-code binary exists and is executable."""
        return os.path.isfile(self.binary_path) and os.access(self.binary_path, os.X_OK)

    def _build_command(self, task: CodeTask) -> list[str]:
        """Build the command line for fusion-code."""
        cmd = [self.binary_path, "--print", task.prompt]
        if task.model:
            cmd.extend(["--model", task.model])
        cmd.extend(task.extra_args)
        return cmd

