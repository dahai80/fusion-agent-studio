"""Git tool — perform common Git operations."""

from __future__ import annotations

from pathlib import Path

from .base import BaseTool


class GitTool(BaseTool):
    """Execute Git commands in a repository."""

    name = "git"
    description = "Execute a Git operation in a repository"
    parameters = {
        "action": {
            "type": "string",
            "description": "Git action to perform: status, log, diff, commit, branch, pull",
            "enum": ["status", "log", "diff", "commit", "branch", "pull"],
        },
        "repo_path": {
            "type": "string",
            "description": "Path to the git repository",
            "default": ".",
        },
        "message": {
            "type": "string",
            "description": "Commit message (required for commit action)",
            "default": "",
        },
        "max_results": {
            "type": "integer",
            "description": "Max results for log/diff (default: 10)",
            "default": 10,
        },
    }

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action", "status")
        repo_path = kwargs.get("repo_path", ".")
        message = kwargs.get("message", "")
        max_results = int(kwargs.get("max_results", 10))

        # Validate action first (before checking repo)
        valid_actions = ["status", "log", "diff", "commit", "branch", "pull"]
        if action not in valid_actions:
            return f"Error: Unknown action: {action}"

        # Validate commit message early
        if action == "commit" and not message:
            return "Error: commit message is required"

        repo = Path(repo_path).expanduser().resolve()

        if not (repo / ".git").exists():
            return f"Error: Not a git repository: {repo}"

        if action == "status":
            return await self._git_cmd(repo, "status", "--short")
        elif action == "log":
            result = await self._git_cmd(repo, "log", f"--max-count={max_results}",
                                         "--oneline", "--decorate")
            if not result:
                return "No commits found"
            return f"Recent commits (last {max_results}):\n{result}"
        elif action == "diff":
            result = await self._git_cmd(repo, "diff", "--stat")
            if not result:
                return "No uncommitted changes"
            return f"Uncommitted changes:\n{result}"
        elif action == "commit":
            if not message:
                return "Error: commit message is required"
            result = await self._git_cmd(repo, "add", "-A")
            result += "\n" + await self._git_cmd(repo, "commit", "-m", message)
            return result
        elif action == "branch":
            result = await self._git_cmd(repo, "branch", "-a")
            return f"Branches:\n{result}"
        elif action == "pull":
            return await self._git_cmd(repo, "pull", "--ff-only")
        else:
            return f"Error: Unknown action: {action}"

    async def _git_cmd(self, repo: Path, *args: str) -> str:
        """Execute a git command and return its output."""
        import asyncio

        cmd = ["git", "-C", str(repo), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip()
            if stderr:
                err = stderr.decode("utf-8", errors="replace").strip()
                if err:
                    output = f"{output}\n[STDERR]\n{err}" if output else err
            return output if output else "(no output)"
        except asyncio.TimeoutError:
            return "Error: Git command timed out"
        except FileNotFoundError:
            return "Error: git not found in PATH"
        except Exception as e:
            return f"Error: {e}"