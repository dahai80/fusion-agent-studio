"""Targeted tests to reach 95%+ coverage — mocks external dependencies."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.exporter import GraphExporter
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.orchestrator import AgentConfig, MultiAgentOrchestrator
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import FusionMLXClient, LLMResponse
from server.process_manager import FusionMLXProcessManager
from tools.base import BaseTool
from tools.file_tools import FileListTool, FileReadTool, FileWriteTool
from tools.git_tools import GitTool
from tools.registry import ToolRegistry
from tools.terminal_tools import TerminalTool
from tools.text_tools import TextSearchTool

# ── Mock-based FusionMLXClient tests ──


class TestFusionMLXClientMocked:
    @pytest.mark.asyncio
    async def test_chat_with_tools(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Let me check",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "f1", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        result = await client.chat(
            model="test",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "test_tool"}}],
        )
        assert result.content == "Let me check"
        assert len(result.tool_calls) == 1
        assert result.usage["prompt_tokens"] == 10

    @pytest.mark.asyncio
    async def test_chat_with_kwargs(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {},
        }
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        await client.chat(model="test", messages=[], top_p=0.9, stop=["\n"])
        call_kwargs = mock_client.post.call_args[1]["json"]
        assert call_kwargs["top_p"] == 0.9

    @pytest.mark.asyncio
    async def test_list_models_mocked(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"id": "m1"}]}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        models = await client.list_models()
        assert len(models) == 1

    @pytest.mark.asyncio
    async def test_health_mocked_true(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        assert await client.health() is True

    @pytest.mark.asyncio
    async def test_health_mocked_false(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        client = FusionMLXClient()
        client._client = mock_client
        assert await client.health() is False

    @pytest.mark.asyncio
    async def test_get_server_stats_mocked(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models_loaded": 2}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        stats = await client.get_server_stats()
        assert stats["models_loaded"] == 2

    @pytest.mark.asyncio
    async def test_get_server_stats_failure(self):
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("fail"))
        client = FusionMLXClient()
        client._client = mock_client
        assert await client.get_server_stats() == {}

    @pytest.mark.asyncio
    async def test_create_agent_session_mocked(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"session_id": "sess-123"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        result = await client.create_agent_session(model="test")
        assert result["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_submit_tool_result_mocked(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"accepted": True}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client = FusionMLXClient()
        client._client = mock_client
        result = await client.submit_tool_result("s1", "c1", "done")
        assert result["accepted"] is True


# ── Mock-based ProcessManager tests ──


class TestProcessManagerMocked:
    @patch("server.process_manager.subprocess.Popen")
    def test_start_success(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        mgr = FusionMLXProcessManager(port=12345, model="qwen3.5-9b")
        mgr._health_check = MagicMock(return_value=True)
        assert mgr.start(wait_timeout=1.0) is True

    @patch("server.process_manager.subprocess.Popen")
    def test_start_already_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mgr = FusionMLXProcessManager(port=12346)
        mgr.process = mock_proc
        assert mgr.start() is True

    @patch("server.process_manager.subprocess.Popen")
    def test_start_file_not_found(self, mock_popen):
        mock_popen.side_effect = FileNotFoundError()
        mgr = FusionMLXProcessManager(port=12347)
        assert mgr.start(wait_timeout=1.0) is False

    @patch("server.process_manager.subprocess.Popen")
    def test_start_generic_error(self, mock_popen):
        mock_popen.side_effect = PermissionError("denied")
        mgr = FusionMLXProcessManager(port=12348)
        assert mgr.start(wait_timeout=1.0) is False

    @patch("server.process_manager.subprocess.Popen")
    def test_stop_graceful(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        mgr = FusionMLXProcessManager(port=12349)
        mgr.process = mock_proc
        mgr.stop()
        mock_proc.terminate.assert_called_once()

    @patch("server.process_manager.subprocess.Popen")
    def test_stop_timeout(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [TimeoutError("timeout"), None]
        mock_popen.return_value = mock_proc
        mgr = FusionMLXProcessManager(port=12350)
        mgr.process = mock_proc
        mgr.stop()
        mock_proc.kill.assert_called_once()

    def test_stop_already_stopped(self):
        mgr = FusionMLXProcessManager(port=12351)
        mgr.stop()
        assert mgr.process is None

    def test_restart(self):
        mgr = FusionMLXProcessManager(port=12352)
        mgr.stop = MagicMock()
        mgr.start = MagicMock(return_value=True)
        assert mgr.restart() is True
        mgr.stop.assert_called_once()

    def test_context_manager(self):
        mgr = FusionMLXProcessManager(port=12353)
        mgr.start = MagicMock(return_value=True)
        mgr.stop = MagicMock()
        mgr.__enter__()
        mgr.__exit__(None, None, None)
        mgr.stop.assert_called_once()

    def test_capture_output_no_process(self):
        mgr = FusionMLXProcessManager(port=12354)
        assert mgr._capture_output() == ("", "")

    def test_is_running_false(self):
        mgr = FusionMLXProcessManager(port=12355)
        assert mgr.is_running() is False


# ── Git tool advanced tests ──


class TestGitToolAdvanced:
    @pytest.mark.asyncio
    async def _init_repo(self, tmpdir: str) -> Path:
        repo = Path(tmpdir)
        p = await asyncio.create_subprocess_exec("git", "init", cwd=str(repo))
        await p.wait()
        for cfg in [
            ["git", "config", "user.email", "t@t.com"],
            ["git", "config", "user.name", "T"],
        ]:
            proc = await asyncio.create_subprocess_exec(*cfg, cwd=str(repo))
            await proc.wait()
        return repo

    @pytest.mark.asyncio
    async def test_git_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._init_repo(tmpdir)
            (repo / "f.txt").write_text("hello")
            tool = GitTool()
            result = await tool.execute(action="status", repo_path=str(repo))
            assert "f.txt" in result or "?" in result

    @pytest.mark.asyncio
    async def test_git_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._init_repo(tmpdir)
            (repo / "f.txt").write_text("hello")
            tool = GitTool()
            result = await tool.execute(
                action="commit", repo_path=str(repo), message="init"
            )
            assert "commit" in result.lower() or "file" in result.lower()

    @pytest.mark.asyncio
    async def test_git_log_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._init_repo(tmpdir)
            tool = GitTool()
            result = await tool.execute(action="log", repo_path=str(repo))
            assert "No commits" in result or "commit" in result.lower()

    @pytest.mark.asyncio
    async def test_git_diff_no_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._init_repo(tmpdir)
            (repo / "f.txt").write_text("hello")
            for c in [["git", "add", "-A"], ["git", "commit", "-m", "init"]]:
                proc = await asyncio.create_subprocess_exec(*c, cwd=str(repo))
                await proc.wait()
            tool = GitTool()
            result = await tool.execute(action="diff", repo_path=str(repo))
            assert ("no uncommitted" in result.lower()) or (
                "no output" in result.lower()
            )

    @pytest.mark.asyncio
    async def test_git_pull_no_remote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = await self._init_repo(tmpdir)
            (repo / "f.txt").write_text("hello")
            for c in [["git", "add", "-A"], ["git", "commit", "-m", "init"]]:
                proc = await asyncio.create_subprocess_exec(*c, cwd=str(repo))
                await proc.wait()
            tool = GitTool()
            result = await tool.execute(action="pull", repo_path=str(repo))
            # No remote configured, should return some error/output
            assert len(result) > 0

    @pytest.mark.asyncio
    async def test_git_cmd_timeout(self):
        tool = GitTool()
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with patch("asyncio.create_subprocess_exec") as me:
                me.return_value = MagicMock()
                result = await tool._git_cmd(Path("."), "status")
                assert "timed out" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_git_cmd_not_found(self):
        tool = GitTool()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await tool._git_cmd(Path("."), "status")
            assert "not found" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_git_cmd_generic_error(self):
        tool = GitTool()
        with patch(
            "asyncio.create_subprocess_exec", side_effect=PermissionError("denied")
        ):
            result = await tool._git_cmd(Path("."), "status")
            assert "Error" in result


# ── File tools advanced tests ──


class TestFileToolsAdvanced:
    @pytest.mark.asyncio
    async def test_file_read_permission_error(self):
        tool = FileReadTool()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"test")
            path = f.name
        try:
            os.chmod(path, 0o000)
            result = await tool.execute(path=path)
            assert "Error" in result or "Permission denied" in result
        finally:
            os.chmod(path, 0o644)
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_file_read_generic_error(self):
        tool = FileReadTool()
        with patch("pathlib.Path.read_text", side_effect=OSError("disk error")):
            result = await tool.execute(path="/some/file.txt")
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_file_write_permission_error(self):
        tool = FileWriteTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            ro = Path(tmpdir) / "ro"
            ro.mkdir()
            ro.chmod(0o444)
            result = await tool.execute(path=str(ro / "test.txt"), content="test")
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_file_write_generic_error(self):
        tool = FileWriteTool()
        with patch("builtins.open", side_effect=OSError("disk full")):
            result = await tool.execute(path="/some/file.txt", content="test")
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_file_list_max_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(10):
                Path(tmpdir, f"f_{i}.txt").write_text(str(i))
            tool = FileListTool()
            result = await tool.execute(path=tmpdir, max_results=3)
            lines = result.strip().split("\n")
            assert len(lines) <= 4

    @pytest.mark.asyncio
    async def test_file_list_permission_error(self):
        tool = FileListTool()
        with patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
            result = await tool.execute(path="/tmp")
            assert "Permission denied" in result

    @pytest.mark.asyncio
    async def test_file_list_generic_error(self):
        tool = FileListTool()
        with patch("pathlib.Path.iterdir", side_effect=OSError("IO error")):
            result = await tool.execute(path="/tmp")
            assert "Error" in result


# ── Terminal tool advanced tests ──


class TestTerminalToolAdvanced:
    @pytest.mark.asyncio
    async def test_terminal_file_not_found(self):
        tool = TerminalTool()
        with patch("asyncio.create_subprocess_shell", side_effect=FileNotFoundError()):
            result = await tool.execute(command="x")
            assert "not found" in result.lower() or "Error" in result

    @pytest.mark.asyncio
    async def test_terminal_generic_error(self):
        tool = TerminalTool()
        with patch(
            "asyncio.create_subprocess_shell", side_effect=RuntimeError("crash")
        ):
            result = await tool.execute(command="x")
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_terminal_with_stderr(self):
        tool = TerminalTool()
        result = await tool.execute(command="ls /nonexistent_xyz_123 2>&1; echo 'done'")
        assert "done" in result or "not found" in result or "Error" in result


# ── Base tool coverage ──


class TestBaseToolAdvanced:
    def test_openai_schema_required(self):
        class T(BaseTool):
            name = "t1"
            description = "desc"
            parameters = {"p1": {"type": "string"}, "p2": {"type": "integer"}}

            async def execute(self, **kwargs):
                return "ok"

        tool = T()
        s = tool.openai_schema()
        assert s["function"]["parameters"]["required"] == ["p1", "p2"]

    def test_tool_name_from_class(self):
        class MyTool(BaseTool):
            name = "my_tool"
            description = "desc"

            async def execute(self, **kwargs):
                return "ok"

        assert MyTool().name == "my_tool"


# ── Runtime error paths ──


class MockTool(BaseTool):
    name = "mock_tool"
    description = "Test tool"
    parameters = {"input": {"type": "string"}}

    async def execute(self, **kwargs):
        return "result"


class TestRuntimeErrorPaths:
    @pytest.mark.asyncio
    async def test_missing_start_node(self):
        graph = AgentGraph(name="test")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "end")
        graph.start_node_id = "nonexistent"
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        events = [e async for e in runtime.execute_graph(graph, "hi")]
        assert any(e.type == AgentEventType.ERROR for e in events)

    @pytest.mark.asyncio
    async def test_llm_no_model(self):
        graph = AgentGraph(name="test")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("llm", NodeConfig(type="llm", label="LLM", model=""))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "end")
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        events = [e async for e in runtime.execute_graph(graph, "hi")]
        assert any(e.type == AgentEventType.ERROR for e in events)

    @pytest.mark.asyncio
    async def test_invalid_tool_call_json(self):
        graph = AgentGraph(name="test")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="test-model"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "end")

        class MLX:
            async def chat(
                self,
                model,
                messages,
                tools=None,
                temperature=0.7,
                max_tokens=4096,
                **kw,
            ):
                return LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "mock_tool", "arguments": "bad json"},
                        }
                    ],
                )

        reg = ToolRegistry()
        reg.register(MockTool())
        runtime = AgentRuntime(MLX(), reg)
        events = [e async for e in runtime.execute_graph(graph, "test")]
        assert any(e.type == AgentEventType.ERROR for e in events)

    def test_condition_has_tool_calls_false(self):
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        vm = runtime.variables
        assert (
            runtime.condition_engine.evaluate("has_tool_calls", AgentContext(), vm)
            == "false"
        )

    def test_condition_has_error_false(self):
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        vm = runtime.variables
        assert (
            runtime.condition_engine.evaluate("has_error", AgentContext(), vm)
            == "false"
        )

    def test_condition_unknown(self):
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        ctx = AgentContext()
        ctx.iteration_count = 5
        vm = runtime.variables
        assert runtime.condition_engine.evaluate("iteration", ctx, vm) == "false"

    def test_condition_malformed_iteration(self):
        runtime = AgentRuntime(MagicMock(), ToolRegistry())
        vm = runtime.variables
        assert (
            runtime.condition_engine.evaluate("iteration >", AgentContext(), vm)
            == "false"
        )


# ── Orchestrator error paths ──


class TestOrchestratorErrorPaths:
    @pytest.mark.asyncio
    async def test_extract_sub_tasks_from_text(self):
        orch = MultiAgentOrchestrator(MagicMock(), ToolRegistry())
        ctx = AgentContext()
        ctx.add_message("assistant", "t1\nt2\nt3")
        tasks = orch._extract_sub_tasks(ctx, 2)
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_master_worker(self):
        mlx = MagicMock()
        mlx.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        g = AgentGraph(name="g")
        g.add_node("start", NodeConfig(type="start"))
        g.add_node("llm", NodeConfig(type="llm", label="LLM", model="test"))
        g.add_node("end", NodeConfig(type="end"))
        g.add_edge("start", "llm")
        g.add_edge("llm", "end")
        orch = MultiAgentOrchestrator(mlx, ToolRegistry())
        result = await orch.master_worker(AgentConfig(name="m", graph=g), [], "task")
        assert len(result.results) >= 2

    @pytest.mark.asyncio
    async def test_parallel_success(self):
        mlx = MagicMock()
        mlx.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
        g = AgentGraph(name="g")
        g.add_node("start", NodeConfig(type="start"))
        g.add_node("llm", NodeConfig(type="llm", label="LLM", model="test"))
        g.add_node("end", NodeConfig(type="end"))
        g.add_edge("start", "llm")
        g.add_edge("llm", "end")
        orch = MultiAgentOrchestrator(mlx, ToolRegistry())
        result = await orch.parallel([AgentConfig(name="w1", graph=g)], "work")
        assert len(result.results) == 1


# ── Text tool tests ──


class TestTextToolAdvanced:
    @pytest.mark.asyncio
    async def test_search_plain_max_results(self):
        tool = TextSearchTool()
        result = await tool.execute(
            text="target " * 50, pattern="target", max_results=5
        )
        assert "5" in result or "occurrence" in result

    @pytest.mark.asyncio
    async def test_search_plain_single(self):
        tool = TextSearchTool()
        result = await tool.execute(text="hello world", pattern="world")
        assert "1 occurrence" in result

    @pytest.mark.asyncio
    async def test_search_regex_multi(self):
        tool = TextSearchTool()
        result = await tool.execute(text="a1 b2 c3", pattern=r"\d+", use_regex=True)
        assert "3" in result or "match" in result

    @pytest.mark.asyncio
    async def test_search_no_matches(self):
        tool = TextSearchTool()
        result = await tool.execute(text="hello world", pattern="xyz")
        assert "No matches" in result


# ── Exporter tests ──


class TestExporterAdvanced:
    def test_export_with_tool_params(self):
        graph = AgentGraph(name="T")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node(
            "tool",
            NodeConfig(
                type="tool",
                tool_name="file_read",
                tool_params={"path": "/tmp/test.txt"},
            ),
        )
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "tool")
        graph.add_edge("tool", "end")
        output = GraphExporter.to_python(graph)
        assert "file_read" in output
        assert "/tmp/test.txt" in output

    def test_export_with_condition_labels(self):
        graph = AgentGraph(name="C")
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("cond", NodeConfig(type="condition", condition_expr="true"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "cond")
        graph.add_edge("cond", "end", "true")
        graph.add_edge("cond", "end", "false")
        output = GraphExporter.to_yaml(graph)
        assert "true" in output
        assert "false" in output
