"""Tests for C15 file tools: FileEditTool, FileDeleteTool, FileGrepTool, FileGlobTool.

Issue #178: Claude标志性编辑原语 (原地Edit, 递归Grep/Glob, 文件删除) + CodeSandbox agent tool.
All tests use tempfile + cleanup; no process data left behind.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tools.code_tools import CodeSandboxTool
from tools.file_tools import (
    FileDeleteTool,
    FileEditTool,
    FileGlobTool,
    FileGrepTool,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestFileEditTool:
    async def test_edit_unique_match(self, tmp_dir):
        path = Path(tmp_dir) / "f.txt"
        path.write_text("line one\nline two\nline three\n")
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path),
            old_string="line two",
            new_string="line TWO",
        )
        assert "replaced 1 occurrence" in result
        assert path.read_text() == "line one\nline TWO\nline three\n"

    async def test_edit_multiline_old_string(self, tmp_dir):
        path = Path(tmp_dir) / "f.py"
        original = "def foo():\n    return 1\n"
        path.write_text(original)
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path),
            old_string="def foo():\n    return 1",
            new_string="def foo():\n    return 2",
        )
        assert "replaced 1 occurrence" in result
        assert path.read_text() == "def foo():\n    return 2\n"

    async def test_edit_not_found(self, tmp_dir):
        path = Path(tmp_dir) / "f.txt"
        path.write_text("hello world")
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path),
            old_string="does not exist",
            new_string="x",
        )
        assert "not found" in result
        assert path.read_text() == "hello world"

    async def test_edit_multiple_matches_rejected(self, tmp_dir):
        path = Path(tmp_dir) / "f.txt"
        path.write_text("dup\ndup\ndup\n")
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path),
            old_string="dup",
            new_string="x",
        )
        assert "matches 3 times" in result
        assert path.read_text() == "dup\ndup\ndup\n"

    async def test_edit_replace_all(self, tmp_dir):
        path = Path(tmp_dir) / "f.txt"
        path.write_text("dup\ndup\ndup\n")
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path),
            old_string="dup",
            new_string="x",
            replace_all=True,
        )
        assert "replaced 3 occurrence" in result
        assert path.read_text() == "x\nx\nx\n"

    async def test_edit_identical_strings_rejected(self, tmp_dir):
        path = Path(tmp_dir) / "f.txt"
        path.write_text("same")
        tool = FileEditTool()
        result = await tool.execute(
            path=str(path), old_string="same", new_string="same"
        )
        assert "identical" in result

    async def test_edit_nonexistent_file(self):
        tool = FileEditTool()
        result = await tool.execute(
            path="/nonexistent/xyz.txt",
            old_string="a",
            new_string="b",
        )
        assert "File not found" in result


class TestFileDeleteTool:
    async def test_delete_file(self, tmp_dir):
        path = Path(tmp_dir) / "gone.txt"
        path.write_text("bye")
        tool = FileDeleteTool()
        result = await tool.execute(path=str(path))
        assert "Deleted" in result
        assert not path.exists()

    async def test_delete_refuses_directory(self, tmp_dir):
        tool = FileDeleteTool()
        result = await tool.execute(path=tmp_dir)
        assert "directory" in result
        assert Path(tmp_dir).exists()

    async def test_delete_nonexistent(self):
        tool = FileDeleteTool()
        result = await tool.execute(path="/nonexistent/delete_me.txt")
        assert "File not found" in result


class TestFileGrepTool:
    async def test_grep_finds_pattern(self, tmp_dir):
        (Path(tmp_dir) / "a.py").write_text("def hello():\n    print('hi')\n")
        (Path(tmp_dir) / "b.py").write_text("x = 1\n")
        tool = FileGrepTool()
        result = await tool.execute(path=tmp_dir, pattern="hello")
        assert "Found 1 match" in result
        assert "a.py:1: def hello():" in result

    async def test_grep_recursive(self, tmp_dir):
        sub = Path(tmp_dir) / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("target_line\n")
        (Path(tmp_dir) / "top.py").write_text("target_line\n")
        tool = FileGrepTool()
        result = await tool.execute(path=tmp_dir, pattern="target_line")
        assert "Found 2 match" in result

    async def test_grep_no_matches(self, tmp_dir):
        (Path(tmp_dir) / "a.txt").write_text("nothing here")
        tool = FileGrepTool()
        result = await tool.execute(path=tmp_dir, pattern="zzzznotfound")
        assert "No matches" in result

    async def test_grep_with_context(self, tmp_dir):
        (Path(tmp_dir) / "a.py").write_text("line1\nline2\nMATCH\nline4\nline5\n")
        tool = FileGrepTool()
        result = await tool.execute(path=tmp_dir, pattern="MATCH", context=1)
        assert "MATCH" in result
        assert "line2" in result
        assert "line4" in result

    async def test_grep_plain_text_not_regex(self, tmp_dir):
        (Path(tmp_dir) / "a.txt").write_text("price: 10$ each\n")
        tool = FileGrepTool()
        result = await tool.execute(
            path=tmp_dir, pattern="10$", use_regex=False
        )
        assert "Found 1 match" in result

    async def test_grep_include_filter(self, tmp_dir):
        (Path(tmp_dir) / "a.py").write_text("common_word\n")
        (Path(tmp_dir) / "b.md").write_text("common_word\n")
        tool = FileGrepTool()
        result = await tool.execute(
            path=tmp_dir, pattern="common_word", include="*.py"
        )
        assert "a.py" in result
        assert "b.md" not in result

    async def test_grep_max_results(self, tmp_dir):
        for i in range(10):
            (Path(tmp_dir) / f"f{i}.py").write_text("needle\n")
        tool = FileGrepTool()
        result = await tool.execute(path=tmp_dir, pattern="needle", max_results=3)
        assert "showing first 3" in result


class TestFileGlobTool:
    async def test_glob_python_files(self, tmp_dir):
        (Path(tmp_dir) / "a.py").write_text("x")
        (Path(tmp_dir) / "b.py").write_text("y")
        (Path(tmp_dir) / "c.md").write_text("z")
        tool = FileGlobTool()
        result = await tool.execute(path=tmp_dir, pattern="*.py")
        assert "a.py" in result
        assert "b.py" in result
        assert "c.md" not in result

    async def test_glob_recursive(self, tmp_dir):
        sub = Path(tmp_dir) / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("x")
        tool = FileGlobTool()
        result = await tool.execute(path=tmp_dir, pattern="**/*.py")
        assert "pkg/mod.py" in result or "mod.py" in result

    async def test_glob_no_matches(self, tmp_dir):
        (Path(tmp_dir) / "a.txt").write_text("x")
        tool = FileGlobTool()
        result = await tool.execute(path=tmp_dir, pattern="**/*.rs")
        assert "No files matching" in result

    async def test_glob_max_results(self, tmp_dir):
        for i in range(10):
            (Path(tmp_dir) / f"f{i}.py").write_text("x")
        tool = FileGlobTool()
        result = await tool.execute(
            path=tmp_dir, pattern="*.py", max_results=3
        )
        assert "showing first 3" in result


class TestCodeSandboxTool:
    async def test_sandbox_python_runs(self):
        tool = CodeSandboxTool()
        result = await tool.execute(
            code="print('sandbox ok')", language="python"
        )
        assert "sandbox ok" in result

    async def test_sandbox_python_ast_blocks_dangerous(self):
        tool = CodeSandboxTool()
        result = await tool.execute(
            code="import os\nos.system('echo pwned')",
            language="python",
        )
        assert "Error" in result
        assert "Safety check failed" in result

    async def test_sandbox_unsupported_language(self):
        tool = CodeSandboxTool()
        result = await tool.execute(
            code="whatever", language="cobol"
        )
        assert "Error" in result
        assert "Unsupported language" in result

    async def test_sandbox_no_code(self):
        tool = CodeSandboxTool()
        result = await tool.execute(code="", language="python")
        assert "code is required" in result


class TestRegistryRegistration:
    def test_new_tools_in_default_registry(self):
        from tools import create_default_registry

        registry = create_default_registry()
        for expected in [
            "file_edit",
            "file_delete",
            "file_grep",
            "file_glob",
            "code_sandbox",
        ]:
            assert registry.has(expected), f"tool {expected!r} not registered"
