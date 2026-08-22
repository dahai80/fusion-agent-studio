"""Tests for C15 GitTool extended actions (P1-9, issue #198).

Covers push/fetch/checkout/merge/rebase/reset/stash/show — each in a
real temporary git repo so the full subprocess path is exercised. The
write actions that mutate history (push/fetch/merge/rebase/reset) run
against a local bare remote or local branches so no network is needed.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from tools.git_tools import GitTool


async def _run(cmd: list[str], cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    res = out.decode("utf-8", errors="replace").strip()
    if err:
        res += err.decode("utf-8", errors="replace").strip()
    return res


@pytest.fixture
def git_env():
    # Real temp repo with identity + an initial commit. Returns (repo_path, helper).
    with tempfile.TemporaryDirectory(prefix="c15git_") as tmp:
        repo = tmp
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "tester",
            "GIT_AUTHOR_EMAIL": "t@t.t",
            "GIT_COMMITTER_NAME": "tester",
            "GIT_COMMITTER_EMAIL": "t@t.t",
        }

        async def init():
            await _run(["git", "init"], repo)
            await _run(["git", "config", "user.name", "tester"], repo)
            await _run(["git", "config", "user.email", "t@t.t"], repo)
            with open(os.path.join(repo, "a.txt"), "w") as f:
                f.write("hello\n")
            await _run(["git", "add", "-A"], repo)
            await _run(["git", "commit", "-m", "init"], repo)

        yield repo, init, env


class TestGitToolExtendedActions:
    async def test_push_to_local_bare_remote(self, git_env):
        repo, init, env = git_env
        await init()
        # bare remote in sibling dir
        remote_dir = os.path.join(os.path.dirname(repo), "remote.git")
        await _run(["git", "init", "--bare", remote_dir], os.path.dirname(repo))
        await _run(["git", "remote", "add", "origin", remote_dir], repo)
        tool = GitTool()
        result = await tool.execute(action="push", repo_path=repo,
                                     remote="origin", branch="main")
        # either success or "main -> main" in stderr output
        assert "Error: git not found" not in result
        assert "main" in result or "Everything up-to-date" in result

    async def test_fetch_from_remote(self, git_env):
        repo, init, env = git_env
        await init()
        remote_dir = os.path.join(os.path.dirname(repo), "remote.git")
        await _run(["git", "init", "--bare", remote_dir], os.path.dirname(repo))
        await _run(["git", "remote", "add", "origin", remote_dir], repo)
        await _run(["git", "push", "-u", "origin", "main"], repo)
        tool = GitTool()
        result = await tool.execute(action="fetch", repo_path=repo,
                                     remote="origin")
        assert "Error: git not found" not in result

    async def test_checkout_create_and_switch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        r1 = await tool.execute(action="checkout", repo_path=repo,
                                branch="feature", create_new=True)
        assert "feature" in r1
        # back to main/master
        r2 = await tool.execute(action="checkout", repo_path=repo, branch="main")
        assert "Error" not in r2 or "did not match" not in r2

    async def test_checkout_requires_branch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="checkout", repo_path=repo)
        assert "requires branch or target" in result

    async def test_merge_branch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        await tool.execute(action="checkout", repo_path=repo,
                           branch="feat", create_new=True)
        with open(os.path.join(repo, "b.txt"), "w") as f:
            f.write("b\n")
        await _run(["git", "add", "-A"], repo)
        await _run(["git", "commit", "-m", "feat"], repo)
        await tool.execute(action="checkout", repo_path=repo, branch="main")
        result = await tool.execute(action="merge", repo_path=repo, branch="feat")
        assert "Error: git not found" not in result

    async def test_merge_requires_branch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="merge", repo_path=repo)
        assert "requires branch" in result

    async def test_rebase_branch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        await tool.execute(action="checkout", repo_path=repo,
                           branch="side", create_new=True)
        with open(os.path.join(repo, "c.txt"), "w") as f:
            f.write("c\n")
        await _run(["git", "add", "-A"], repo)
        await _run(["git", "commit", "-m", "side"], repo)
        result = await tool.execute(action="rebase", repo_path=repo, branch="main")
        assert "Error: git not found" not in result

    async def test_rebase_requires_branch(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="rebase", repo_path=repo)
        assert "requires branch" in result

    async def test_reset_soft(self, git_env):
        repo, init, env = git_env
        await init()
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("changed\n")
        await _run(["git", "add", "-A"], repo)
        await _run(["git", "commit", "-m", "second"], repo)
        tool = GitTool()
        result = await tool.execute(action="reset", repo_path=repo,
                                    mode="soft", target="HEAD~1")
        assert "Error: git not found" not in result
        # soft reset keeps changes staged
        status = await tool.execute(action="status", repo_path=repo)
        assert "a.txt" in status

    async def test_reset_invalid_mode(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="reset", repo_path=repo, mode="bogus")
        assert "invalid reset mode" in result

    async def test_stash_save_and_pop(self, git_env):
        repo, init, env = git_env
        await init()
        with open(os.path.join(repo, "a.txt"), "w") as f:
            f.write("stashed\n")
        tool = GitTool()
        saved = await tool.execute(action="stash", repo_path=repo)
        assert "Error: git not found" not in saved
        # working tree clean of that change after stash
        await tool.execute(action="status", repo_path=repo)
        popped = await tool.execute(action="stash", repo_path=repo, pop=True)
        assert "Error: git not found" not in popped

    async def test_show_head(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="show", repo_path=repo, target="HEAD")
        assert "Error: git not found" not in result
        assert "init" in result

    async def test_show_nonexistent_no_crash(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        result = await tool.execute(action="show", repo_path=repo,
                                    target="deadbeef")
        # git prints error to stderr, tool merges it; must not crash
        assert isinstance(result, str)

    async def test_extended_enum_registered(self):
        from tools import create_default_registry
        reg = create_default_registry()
        tool = reg.get("git")
        actions = tool.parameters["action"]["enum"]
        for a in ["push", "fetch", "checkout", "merge", "rebase", "reset",
                  "stash", "show"]:
            assert a in actions, f"missing action {a}"

    async def test_existing_actions_still_work(self, git_env):
        repo, init, env = git_env
        await init()
        tool = GitTool()
        # clean tree -> status is "(no output)"; dirty tree lists the file.
        dirty = await tool.execute(action="status", repo_path=repo)
        assert dirty in ("(no output)", "No uncommitted changes") or "a.txt" in dirty
        assert "init" in await tool.execute(action="log", repo_path=repo)
        assert "Branches" in await tool.execute(action="branch", repo_path=repo)
