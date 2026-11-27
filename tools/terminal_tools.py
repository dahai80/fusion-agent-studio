"""Terminal tool — execute shell commands on the local system."""

from __future__ import annotations

import asyncio
import logging
import os
import re

from .base import BaseTool

logger = logging.getLogger(__name__)

# Audit A-3/E-1: catastrophic command blocklist for the terminal sink. The
# terminal tool is arbitrary shell execution by design (that is its purpose),
# so no allowlist (it would break backward compat + use cases), but block the
# irreversible destructive patterns: rm -rf (root/home/env-expanded targets),
# mkfs (formatting), dd to block devices (incl. raw disk rdisk),
# shutdown/reboot/halt/poweroff/init 0/kill init, raw disk writes (> >> tee).
# env FUSION_TERMINAL_UNRESTRICTED=1 can fully opt out (e.g. controlled CI).
# This is the catastrophe defense line by default; every command is logged.
# E-1: the original substring-regex blocklist missed `rm -rf ~`/
# `rm -rf $HOME`/`rm -rf /Users/*`/`dd of=/dev/rdisk0`/`init 0`/
# `kill -9 1`/`osascript shut down`/`tee /dev/sda`. Added these known bypasses.
_CATASTROPHIC_PATTERNS = [
    # rm with -r (recursive) flag + target being a root tree (starts with /:
    # / /usr /opt /var ...) or a home-dir expansion (~ / $HOME / $USERPROFILE)
    # / /Users/*. These recursively delete system trees or user homes,
    # irreversible. Note: only recursive rm is blocked; single-file rm is not
    # (rm file.txt works normally).
    re.compile(
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)+(/[^\s]*|~[^\s]*|\\?\$HOME|\\?\$USERPROFILE)",
        re.IGNORECASE,
    ),
    re.compile(r"\bmkfs\b"),
    # dd writing to block devices: includes raw disk rdisk + relative traversal.
    re.compile(r"\bdd\b.*\bof=/dev/(r?)(sd|nvme|disk|hd)"),
    # Raw disk writes: > >> tee.
    re.compile(r"(>>?)\s*/dev/(r?)(sd|nvme|disk|hd)"),
    re.compile(r"\btee\s+/dev/(r?)(sd|nvme|disk|hd)"),
    # Shutdown: shutdown/reboot/halt/poweroff/init 0/kill init/osascript shut down.
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\binit\s+0\b"),
    re.compile(r"\bkill\s+(-9\s+)?1\b"),
    re.compile(r"osascript.*shut\s*down"),
    # Audit P1-23: recursive chmod/chown on system trees (irreversible
    # permission/ownership rewrite, same class as rm -r on a root tree).
    re.compile(r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)+(/[^\s]*|~[^\s]*|\\?\$HOME)", re.IGNORECASE),
    re.compile(r"\bchown\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)+(/[^\s]*|~[^\s]*|\\?\$HOME)", re.IGNORECASE),
    # Fork bomb: canonical :(){:|:&};: form.
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
]

# #640: host-side paths that can never resolve inside the workspace-scoped
# container (only the session workdir is mounted at /workspace).
_HOST_PATH_PATTERN = re.compile(r"(?:/Users/\S+|/home/\S+|~/\S+|~|/app/\S+)", re.IGNORECASE)


def _find_host_path(command: str) -> str:
    # 'Shell expansions like $HOME are left alone — they may legitimately
    # resolve to container-local paths (/root). Literal host roots
    # (/Users, /home, /app, ~) are always unreachable in the container.
    m = _HOST_PATH_PATTERN.search(command)
    return m.group(0) if m else ""


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
        "workspace": {
            "type": "string",
            "description": "Execution backend: docker (isolated container) or local (default: auto)",
            "default": "auto",
            "enum": ["auto", "docker", "local"],
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

        logger.info("terminal exec workdir=%s cmd=%s", workdir or "(cwd)", command[:300])

        # Audit A-3: catastrophic command blocklist (secure-by-default).
        # Opting out requires an explicit env flag.
        unrestricted = os.environ.get("FUSION_TERMINAL_UNRESTRICTED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if not unrestricted:
            for pat in _CATASTROPHIC_PATTERNS:
                if pat.search(command):
                    logger.warning("terminal blocked catastrophic command: %s", command[:200])
                    return (
                        "Error: command matched a catastrophic pattern (rm -rf /, mkfs, "
                        "dd to disk, shutdown, raw disk write) and is blocked by default. "
                        "Set FUSION_TERMINAL_UNRESTRICTED=1 to allow."
                    )

        # #495: optional container isolation. tool config `workspace: docker`
        # (manifest) or LLM arg routes exec through a throwaway container
        # (--network none, 512m, --rm); Docker absent -> local fallback.
        try:
            from agent_runtime.workspace import resolve_workspace_backend, run_in_container

            backend = resolve_workspace_backend(str(kwargs.get("workspace", "")))
        except Exception as e:
            logger.warning("workspace backend resolve failed: %s", e)
            backend = "local"
        if backend == "docker":
            # #640: the container only mounts the session workdir at
            # /workspace — host paths are unreachable. Fail fast with an
            # actionable scope message instead of letting the command die
            # inside the container with no boundary context.
            host_path = _find_host_path(command)
            if host_path:
                logger.warning("terminal container blocked host path: %s", host_path)
                return (
                    "Error: sandboxed: workspace-scoped access only — path "
                    f"'{host_path}' is outside the session workspace. Only the "
                    "session workdir is mounted, at /workspace inside the "
                    "sandbox; re-anchor your paths there."
                )
            result = await asyncio.to_thread(
                run_in_container,
                ["bash", "-lc", command],
                workdir,
                timeout,
            )
            if result.timed_out:
                return f"Error: Command timed out after {timeout}s"
            if result.success:
                out = result.stdout.strip()
                if result.stderr.strip():
                    out = (
                        f"{out}\n[STDERR]\n{result.stderr.strip()}"
                        if out
                        else result.stderr.strip()
                    )
                return out if out else "Command completed (exit code 0, no output)"
            prefix = f"Error: Command exited with code {result.exit_code}"
            detail = result.stderr.strip() or result.stdout.strip()
            body = f"{prefix}:\n{detail}" if detail else prefix
            # #640: container failures are often host-path misses; always
            # carry the scope so the agent can re-anchor without burning
            # turns guessing the boundary.
            return f"{body}\n[hint] sandboxed: workspace-scoped access only — the session workdir is mounted at /workspace; host paths are not reachable."

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir if workdir else None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
                # Audit P1-5/NEW-1: prefix non-zero exits with "Error: " to
                # align with runtime tool error detection
                # (result.startswith("Error:") -> is_tool_error). The previous
                # "Command exited with code N" did not match -> stop_on_tool_error
                # never fired for failing shells, so errors cascaded as normal
                # results.
                prefix = f"Error: Command exited with code {proc.returncode}"
                if result:
                    return f"{prefix}:\n{result}"
                return prefix

            return result if result else "Command completed (exit code 0, no output)"

        except FileNotFoundError:
            return f"Error: Command not found: {command.split()[0]}"
        except Exception as e:
            return f"Error executing command: {e}"
