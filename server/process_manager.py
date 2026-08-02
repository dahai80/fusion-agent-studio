"""FusionMLX process manager — manages the fusion-mlx server process lifecycle."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


class FusionMLXProcessManager:
    """Manages the lifecycle of a fusion-mlx serve process.

    Handles starting, stopping, health checking, and restarting
    the fusion-mlx server as a subprocess.
    """

    def __init__(
        self,
        port: int = 11434,
        model: str = "",
        model_dir: str = "",
        host: str = "127.0.0.1",
        log_level: str = "WARNING",
        extra_args: list[str] | None = None,
    ):
        self.port = port
        self.model = model
        self.model_dir = model_dir
        self.host = host
        self.log_level = log_level
        self.extra_args = extra_args or []
        self.process: subprocess.Popen | None = None
        self._health_check_url = f"http://{host}:{port}/v1/models"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self, wait_timeout: float = 30.0) -> bool:
        """Start fusion-mlx as a subprocess.

        Args:
            wait_timeout: Maximum seconds to wait for the server to become healthy.

        Returns:
            True if the server started successfully, False otherwise.
        """
        if self.process and self.process.poll() is None:
            logger.info("fusion-mlx is already running on port %d", self.port)
            return True

        cmd = [sys.executable, "-m", "fusion_mlx", "serve", "--port", str(self.port)]

        if self.model:
            cmd.append(self.model)
        if self.model_dir:
            cmd.extend(["--model-dir", self.model_dir])
        if self.host:
            cmd.extend(["--host", self.host])
        if self.log_level:
            cmd.extend(["--log-level", self.log_level])
        cmd.extend(self.extra_args)

        logger.info("Starting fusion-mlx: %s", " ".join(cmd))

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except FileNotFoundError:
            logger.error("fusion-mlx not found. Is it installed?")
            return False
        except Exception as e:
            logger.error("Failed to start fusion-mlx: %s", e)
            return False

        # Wait for server to become healthy
        start_time = time.time()
        while time.time() - start_time < wait_timeout:
            if self._health_check():
                logger.info(
                    "fusion-mlx started on %s (%.1fs)",
                    self.base_url, time.time() - start_time,
                )
                return True
            time.sleep(0.5)

        # Timeout — capture stdout/stderr for diagnostics
        stdout, stderr = self._capture_output()
        logger.error(
            "fusion-mlx failed to start within %.0fs\nstdout: %s\nstderr: %s",
            wait_timeout, stdout[:1000], stderr[:1000],
        )
        self.stop()
        return False

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the fusion-mlx process gracefully.

        Args:
            timeout: Seconds to wait for graceful shutdown before force kill.
        """
        if self.process is None or self.process.poll() is not None:
            self.process = None
            return

        logger.info("Stopping fusion-mlx (PID %d)...", self.process.pid)

        try:
            self.process.terminate()
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("fusion-mlx did not stop gracefully, sending SIGKILL")
            self.process.kill()
            self.process.wait(timeout=5.0)
        except Exception as e:
            logger.error("Error stopping fusion-mlx: %s", e)
            self.process.kill()
            self.process.wait(timeout=5.0)

        self.process = None

    def restart(self, wait_timeout: float = 30.0) -> bool:
        """Restart the fusion-mlx process."""
        self.stop()
        return self.start(wait_timeout=wait_timeout)

    def is_running(self) -> bool:
        """Check if the fusion-mlx process is running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def _health_check(self) -> bool:
        """Check if the fusion-mlx HTTP server is responding."""
        import httpx
        try:
            resp = httpx.get(self._health_check_url, timeout=1.0)
            return resp.status_code == 200
        except Exception:
            return False

    def _capture_output(self) -> tuple[str, str]:
        """Capture any pending stdout/stderr from the process."""
        stdout = ""
        stderr = ""
        if self.process and self.process.stdout:
            try:
                stdout = self.process.stdout.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass
        if self.process and self.process.stderr:
            try:
                stderr = self.process.stderr.read(4096).decode("utf-8", errors="replace")
            except Exception:
                pass
        return stdout, stderr

    def __enter__(self) -> FusionMLXProcessManager:
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()