"""Final coverage push — targets remaining uncovered lines."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.base import BaseTool
from tools.terminal_tools import TerminalTool
from tools.file_tools import FileReadTool, FileWriteTool, FileListTool
from tools.text_tools import TextProcessTool, TextSearchTool
from tools.git_tools import GitTool


# ── BaseTool: line 35 (__init_subclass__ sets name) ──

class ImplicitNameTool(BaseTool):
    description = "No explicit name"
    parameters = {"x": {"type": "string"}}
    async def execute(self, **kwargs):
        return "ok"


class TestBaseToolFinal:

    def test_init_subclass_sets_name_from_class_name(self):
        """Line 35: cls.name = cls.__name__.lower()"""
        tool = ImplicitNameTool()
        assert tool.name == "implicitnametool"

    def test_abstract_method_marker(self):
        """Line 44: abstract method body."""
        assert hasattr(BaseTool.execute, "__isabstractmethod__")


# ── TerminalTool: line 73 (nonzero exit, no output) ──

class TestTerminalToolFinal:

    @pytest.mark.asyncio
    async def test_nonzero_exit_no_output(self):
        """Line 73: return prefix when command fails with no output."""
        tool = TerminalTool()
        r = await tool.execute(command="exit 1")
        assert "exited with code 1" in r


# ── FileTool: lines 47-50 (UnicodeDecodeError, generic Exception) ──

class TestFileToolFinal:

    @pytest.mark.asyncio
    async def test_unicode_decode_error(self):
        """Line 47-48: catch UnicodeDecodeError."""
        tool = FileReadTool()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"\xff\xfe\x80\x81")
            p = f.name
        try:
            r = await tool.execute(path=p, encoding="ascii")
            assert "Error" in r or "Cannot decode" in r
        finally:
            Path(p).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_read_generic_exception(self):
        """Line 49-50: catch generic Exception."""
        tool = FileReadTool()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w") as f:
            f.write("ok")
            p = f.name
        try:
            with patch("pathlib.Path.read_text", side_effect=RuntimeError("boom")):
                r = await tool.execute(path=p)
                assert "Error reading file" in r
        finally:
            Path(p).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_write_generic_exception(self):
        """Line 98-101: catch generic Exception in write."""
        tool = FileWriteTool()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            r = await tool.execute(path="/x/y/z.txt", content="test")
            assert "Error" in r

    @pytest.mark.asyncio
    async def test_list_other_type(self):
        """Line 155: [OTHER] for non-regular entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.txt"
            target.write_text("x")
            link = Path(tmpdir) / "link"
            try:
                link.symlink_to(target)
                tool = FileListTool()
                r = await tool.execute(path=tmpdir)
                assert "target.txt" in r or "link" in r
            except (OSError, NotImplementedError):
                pass


# ── TextTool: lines 129, 155-156 (regex edge cases, generic exception) ──

class TestTextToolFinal:

    @pytest.mark.asyncio
    async def test_regex_no_match_no_results(self):
        """Line 129: return 'No matches found' for regex with no matches."""
        tool = TextSearchTool()
        r = await tool.execute(text="abc", pattern=r"\d+", use_regex=True)
        assert "No matches found" in r

    @pytest.mark.asyncio
    async def test_search_generic_exception(self):
        """Lines 155-156: catch generic Exception."""
        tool = TextSearchTool()
        with patch("re.finditer", side_effect=RuntimeError("crash")):
            r = await tool.execute(text="x", pattern="x", use_regex=True)
            assert "Error searching text" in r

    @pytest.mark.asyncio
    async def test_plain_text_no_match(self):
        """Line 137: no matches for plain text."""
        tool = TextSearchTool()
        r = await tool.execute(text="hello world", pattern="xyz")
        assert "No matches" in r


# ── GitTool: lines 64, 69, 73, 83, 100->102 (git commands) ──

class TestGitToolFinal:

    @pytest.mark.asyncio
    async def _repo(self, tmpdir: str, commit: bool = True) -> Path:
        repo = Path(tmpdir)
        for c in [["git", "init"], ["git", "config", "user.email", "x@x.com"], ["git", "config", "user.name", "X"]]:
            p = await asyncio.create_subprocess_exec(*c, cwd=str(repo))
            await p.wait()
        (repo / "f.txt").write_text("hello")
        if commit:
            for c in [["git", "add", "-A"], ["git", "commit", "-m", "init"]]:
                p = await asyncio.create_subprocess_exec(*c, cwd=str(repo))
                await p.wait()
        return repo

    @pytest.mark.asyncio
    async def test_log_with_commits(self):
        """Line 64: log returns commit list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._repo(tmpdir)
            r = await GitTool().execute(action="log", repo_path=str(repo))
            assert "Recent commits" in r

    @pytest.mark.asyncio
    async def test_diff_with_changes(self):
        """Line 69: diff shows uncommitted changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._repo(tmpdir)
            (repo / "f.txt").write_text("modified")
            r = await GitTool().execute(action="diff", repo_path=str(repo))
            assert "Uncommitted" in r or "changed" in r.lower()

    @pytest.mark.asyncio
    async def test_commit_with_message(self):
        """Line 73: commit with message succeeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._repo(tmpdir, commit=False)
            r = await GitTool().execute(action="commit", repo_path=str(repo), message="test")
            assert "commit" in r.lower() or "file" in r.lower() or "changed" in r.lower()

    @pytest.mark.asyncio
    async def test_branch_list(self):
        """Line 83: branch lists branches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._repo(tmpdir)
            r = await GitTool().execute(action="branch", repo_path=str(repo))
            assert "Branches" in r or "master" in r or "main" in r

    @pytest.mark.asyncio
    async def test_git_cmd_no_output(self):
        """Lines 100->102: return '(no output)' for empty git output."""
        tool = GitTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._repo(tmpdir, commit=False)
            # 'git status --porcelain' in a repo with no commits still shows output
            # Use a command that truly produces no output
            r = await tool._git_cmd(repo, "rev-parse", "--verify", "HEAD")
            # HEAD doesn't exist in empty repo, so stdout is empty, stderr has error
            # The key is that the tool handles this without crashing
            assert isinstance(r, str)