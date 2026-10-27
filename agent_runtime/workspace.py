"""Workspace execution backends (#495): optional container isolation for
shell/code tools with graceful fallback to the local path when Docker absent.

Selection order: tool manifest config `workspace: docker|local|auto` (merged
as kwargs default by runtime._merge_tool_config_defaults) > env
FUSION_WORKSPACE_BACKEND > "auto" (docker if available, else local).
Only python/shell/bash containerize — other language toolchains are not in the
default image and stay on the local sandbox-exec path.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .code_sandbox import SandboxResult

logger = logging.getLogger(__name__)

_DOCKER_CACHE: tuple[float, bool] = (0.0, False)
_DOCKER_CACHE_TTL = 60.0

VALID_WORKSPACE_MODES = {"", "auto", "docker", "local"}

# Languages runnable inside the default container image (bash + python3).
CONTAINER_LANGUAGES = {"python", "shell", "bash"}


def workspace_image() -> str:
    return os.environ.get("FUSION_WORKSPACE_IMAGE", "python:3.12-slim").strip() or "python:3.12-slim"


def docker_available(force: bool = False) -> bool:
    # #495: probe cached 60s — `docker info` is too slow to run per tool call.
    global _DOCKER_CACHE
    now = time.time()
    if not force and now - _DOCKER_CACHE[0] < _DOCKER_CACHE_TTL:
        return _DOCKER_CACHE[1]
    ok = False
    if shutil.which("docker"):
        try:
            proc = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=10
            )
            ok = proc.returncode == 0
        except Exception as e:
            logger.warning("docker info probe failed: %s", e)
    _DOCKER_CACHE = (now, ok)
    logger.info("docker availability probe: available=%s", ok)
    return ok


def normalize_workspace_mode(value) -> str:
    mode = str(value or "").strip().lower()
    if mode not in VALID_WORKSPACE_MODES:
        logger.warning("unknown workspace mode %r ignored", value)
        return ""
    return mode


def resolve_workspace_backend(requested: str = "") -> str:
    # Returns "docker" | "local". docker requested/unavailable -> local
    # fallback with warning (AC: fallback verified when Docker absent).
    mode = normalize_workspace_mode(requested)
    if not mode:
        mode = normalize_workspace_mode(os.environ.get("FUSION_WORKSPACE_BACKEND", "auto")) or "auto"
    if mode == "local":
        return "local"
    if mode == "docker":
        if docker_available():
            return "docker"
        logger.warning("workspace=docker requested but docker unavailable, falling back to local")
        return "local"
    if docker_available():
        return "docker"
    return "local"


def run_in_container(
    inner_argv: list[str],
    workdir: str = "",
    timeout: int = 30,
    exec_id: str = "",
) -> SandboxResult:
    # #495: run a command inside a throwaway container. Hard limits:
    # --rm (ephemeral), --network none (no net), 512m memory, 256 pids.
    # workdir is bind-mounted read-write at /workspace; caller's inner_argv
    # must reference /workspace-relative paths.
    exec_id = exec_id or uuid.uuid4().hex[:8]
    cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", "512m",
        "--pids-limit", "256",
    ]
    if workdir:
        resolved = str(Path(workdir).resolve())
        cmd += ["-v", f"{resolved}:/workspace", "-w", "/workspace"]
    cmd += [workspace_image()] + inner_argv
    logger.info("container exec %s: %s", exec_id, " ".join(cmd[:10]))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        success = proc.returncode == 0
        logger.info(
            "container exec %s: exit=%d success=%s", exec_id, proc.returncode, success
        )
        return SandboxResult(
            success=success,
            exit_code=proc.returncode,
            stdout=proc.stdout[:10000],
            stderr=proc.stderr[:5000],
            execution_id=exec_id,
        )
    except subprocess.TimeoutExpired:
        logger.warning("container exec %s: TIMEOUT (%ds)", exec_id, timeout)
        return SandboxResult(success=False, timed_out=True, execution_id=exec_id)
    except Exception as e:
        logger.error("container exec %s: ERROR %s", exec_id, e)
        return SandboxResult(success=False, stderr=str(e), execution_id=exec_id)


def container_inner_argv(language: str, container_script: str) -> list[str]:
    # Container-side interpreter argv for a mounted script path.
    if language == "python":
        return ["python3", container_script]
    return ["bash", container_script]
