"""Git tool — perform common Git operations."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BaseTool

logger = logging.getLogger(__name__)


class GitTool(BaseTool):
    """Execute Git commands in a repository."""

    name = "git"
    description = "Execute a Git operation in a repository"
    parameters = {
        "action": {
            "type": "string",
            "description": (
                "Git action to perform: status, log, diff, commit, branch, "
                "pull, push, fetch, checkout, merge, rebase, reset, stash, show"
            ),
            "enum": [
                "status", "log", "diff", "commit", "branch", "pull",
                "push", "fetch", "checkout", "merge", "rebase", "reset",
                "stash", "show",
            ],
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
        "branch": {
            "type": "string",
            "description": (
                "Branch name (checkout/merge/rebase/push/fetch target branch)"
            ),
            "default": "",
        },
        "remote": {
            "type": "string",
            "description": "Remote name for push/fetch (default: origin)",
            "default": "origin",
        },
        "target": {
            "type": "string",
            "description": (
                "Commit hash / ref for show/reset (e.g. HEAD, HEAD~1, a1b2c3d)"
            ),
            "default": "",
        },
        "mode": {
            "type": "string",
            "description": "reset mode: soft, mixed, hard (default: mixed)",
            "enum": ["soft", "mixed", "hard"],
            "default": "mixed",
        },
        "create_new": {
            "type": "boolean",
            "description": "checkout: create a new branch (-b) if true",
            "default": False,
        },
        "pop": {
            "type": "boolean",
            "description": "stash: pop the latest stash instead of saving",
            "default": False,
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
        branch = kwargs.get("branch", "")
        remote = kwargs.get("remote", "origin") or "origin"
        target = kwargs.get("target", "")
        mode = kwargs.get("mode", "mixed") or "mixed"
        create_new = bool(kwargs.get("create_new", False))
        pop = bool(kwargs.get("pop", False))

        valid_actions = [
            "status", "log", "diff", "commit", "branch", "pull",
            "push", "fetch", "checkout", "merge", "rebase", "reset",
            "stash", "show",
        ]
        if action not in valid_actions:
            return f"Error: Unknown action: {action}"

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
            if not result or result == "(no output)":
                return "No commits found"
            return f"Recent commits (last {max_results}):\n{result}"
        elif action == "diff":
            result = await self._git_cmd(repo, "diff", "--stat")
            if not result or result == "(no output)":
                return "No uncommitted changes"
            return f"Uncommitted changes:\n{result}"
        elif action == "commit":
            result = await self._git_cmd(repo, "add", "-A")
            result += "\n" + await self._git_cmd(repo, "commit", "-m", message)
            return result
        elif action == "branch":
            result = await self._git_cmd(repo, "branch", "-a")
            return f"Branches:\n{result}"
        elif action == "pull":
            return await self._git_cmd(repo, "pull", "--ff-only")
        elif action == "push":
            args = ["push", remote]
            if branch:
                args.append(branch)
            logger.info("git push %s %s", remote, branch or "(current)")
            return await self._git_cmd(repo, *args)
        elif action == "fetch":
            args = ["fetch", remote]
            if branch:
                args.append(branch)
            logger.info("git fetch %s %s", remote, branch or "(all)")
            return await self._git_cmd(repo, *args)
        elif action == "checkout":
            if not branch and not target:
                return "Error: checkout requires branch or target"
            ref = branch or target
            args = ["checkout"]
            if create_new and branch:
                args.append("-b")
            args.append(ref)
            logger.info("git checkout %s%s", "-b " if create_new and branch else "", ref)
            return await self._git_cmd(repo, *args)
        elif action == "merge":
            if not branch:
                return "Error: merge requires branch"
            logger.info("git merge %s", branch)
            return await self._git_cmd(repo, "merge", branch)
        elif action == "rebase":
            if not branch:
                return "Error: rebase requires branch (upstream)"
            logger.info("git rebase %s", branch)
            return await self._git_cmd(repo, "rebase", branch)
        elif action == "reset":
            if mode not in ("soft", "mixed", "hard"):
                return f"Error: invalid reset mode: {mode}"
            args = ["reset", f"--{mode}"]
            ref = target or "HEAD"
            args.append(ref)
            logger.info("git reset --%s %s", mode, ref)
            return await self._git_cmd(repo, *args)
        elif action == "stash":
            if pop:
                logger.info("git stash pop")
                return await self._git_cmd(repo, "stash", "pop")
            logger.info("git stash save")
            return await self._git_cmd(repo, "stash")
        elif action == "show":
            ref = target or "HEAD"
            result = await self._git_cmd(
                repo, "show", "--stat", "--patch", f"--max-count={max_results}", ref
            )
            if not result or result == "(no output)":
                return f"No content for: {ref}"
            return result
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
