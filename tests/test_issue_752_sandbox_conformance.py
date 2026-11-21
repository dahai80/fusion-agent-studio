import pytest
from fusion_harness.code_mode import CodeModeTurn
from fusion_harness.funnels import ToolFunnel
from fusion_harness.permissions import PermissionEngine
from fusion_harness.sandbox import ExecutionTier, SandboxPolicy, SandboxRunner

from agent_runtime.code_sandbox import CodeSandbox, CodeSandboxExecutor

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def _make_sandbox() -> CodeSandbox:
    return CodeSandbox(use_sandbox=False, timeout=15)


class TestSandboxRunnerConformance:
    def test_runtime_checkable_protocol(self):
        assert isinstance(_make_sandbox(), SandboxRunner)

    @pytest.mark.asyncio
    async def test_run_sandboxed_code_payload(self):
        sandbox = _make_sandbox()
        result = await sandbox.run_sandboxed(
            "code_execute", {"code": "print('conform_ok')", "language": "python"}
        )
        assert result["success"] is True
        assert "conform_ok" in result["stdout"]

    @pytest.mark.asyncio
    async def test_run_sandboxed_shell_payload(self):
        sandbox = _make_sandbox()
        result = await sandbox.run_sandboxed("bash", {"cmd": "echo shell_ok"})
        assert result["success"] is True
        assert "shell_ok" in result["stdout"]

    @pytest.mark.asyncio
    async def test_run_sandboxed_fail_closed_unknown_payload(self):
        sandbox = _make_sandbox()
        result = await sandbox.run_sandboxed("mystery_tool", {"path": "/x"})
        assert result["success"] is False
        assert result.get("blocked") is True
        assert "not sandboxable" in result["stderr"]

    def test_policy_routes_via_runner_not_blocked(self):
        decision = SandboxPolicy(runner=_make_sandbox()).decide(
            "code_execute", {}, (0.95, "destructive")
        )
        assert decision.tier is ExecutionTier.SANDBOX

    @pytest.mark.asyncio
    async def test_funnel_routes_destructive_via_studio_runner(self):
        runner = _make_sandbox()
        funnel = ToolFunnel(
            permissions=PermissionEngine(mode="bypass"),
            sandbox_policy=SandboxPolicy(runner=runner),
        )
        result, is_error = await funnel.run_tool(
            None, "bash", {"cmd": "echo 'DROP TABLE routed_ok'"}, lambda a: "should-not-run"
        )
        assert is_error is False
        assert "routed_ok" in result
        assert "should-not-run" not in result


class TestCodeExecutorConformance:
    def test_happy_path_returns_value(self):
        executor = CodeSandboxExecutor(sandbox=_make_sandbox())
        value = executor.execute("print(21 * 2)", {"seed": 7})
        assert value == 42

    def test_namespace_injected_as_globals(self):
        executor = CodeSandboxExecutor(sandbox=_make_sandbox())
        script = "import json\nprint(json.dumps({'doubled': n * 2}))"
        value = executor.execute(script, {"n": 4})
        assert value["doubled"] == 8

    def test_error_raises_for_self_repair_loop(self):
        executor = CodeSandboxExecutor(sandbox=_make_sandbox())
        with pytest.raises(RuntimeError, match="boom"):
            executor.execute("raise ValueError('boom')", {})

    def test_safety_check_failure_raises(self):
        executor = CodeSandboxExecutor(sandbox=_make_sandbox())
        with pytest.raises(RuntimeError, match="Safety check failed"):
            executor.execute("import subprocess\nprint(1)", {})

    @pytest.mark.asyncio
    async def test_code_mode_turn_end_to_end(self):
        executor = CodeSandboxExecutor(sandbox=_make_sandbox())
        turn = CodeModeTurn(TOOLS, executor)
        text = "```python\nprint(total * 2)\n```"
        result = turn.run(text, {"total": 100})
        assert result.ok is True
        assert result.value == 200
