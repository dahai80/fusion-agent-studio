"""#495: container workspace backend for shell/code tools.

AC: shell/code tool calls optionally execute inside a container; fallback
path verified when Docker absent; tests cover both backends.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from agent_runtime.code_sandbox import CodeSandbox
from agent_runtime.workspace import (
    container_inner_argv,
    docker_available,
    normalize_workspace_mode,
    resolve_workspace_backend,
    run_in_container,
    workspace_image,
)
from tools.code_tools import CodeExecuteTool, CodeSandboxTool
from tools.terminal_tools import TerminalTool


@pytest.fixture(autouse=True)
def _no_docker_env(monkeypatch):
    monkeypatch.delenv("FUSION_WORKSPACE_BACKEND", raising=False)
    monkeypatch.delenv("FUSION_WORKSPACE_IMAGE", raising=False)


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr("agent_runtime.workspace._DOCKER_CACHE", (0.0, False))
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "docker" else shutil.which(name)
    )


@pytest.fixture
def fake_docker(monkeypatch, tmp_path):
    # Fake `docker` shim on PATH: `docker info` exits 0 (daemon probe);
    # run invocations extract the command after "bash -lc"/"-c" and run it
    # via the real bash — proves container wiring without a Docker daemon.
    import time as _time

    monkeypatch.setattr("agent_runtime.workspace._DOCKER_CACHE", (_time.time(), True))
    shim = tmp_path / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        "[ \"$1\" = \"info\" ] && exit 0\n"
        "args=(\"$@\")\n"
        "host=\"\"\n"
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        "  if [ \"${args[$i]}\" = \"-v\" ]; then\n"
        "    host=\"${args[$((i+1))]}\"\n"
        "    host=\"${host%%:/workspace}\"\n"
        "  fi\n"
        "done\n"
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        "  if [ \"${args[$i]}\" = \"-lc\" ] || [ \"${args[$i]}\" = \"-c\" ]; then\n"
        "    cmd=\"${args[$((i+1))]}\"\n"
        "    [ -n \"$host\" ] && cmd=\"${cmd//\\/workspace\\//$host/}\"\n"
        "    exec bash -lc \"$cmd\"\n"
        "  fi\n"
        "done\n"
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        "  if [ \"${args[$i]}\" = \"python3\" ]; then\n"
        "    script=\"${args[$((i+1))]}\"\n"
        "    [ -n \"$host\" ] && script=\"${script/\\/workspace/$host}\"\n"
        "    exec python3 \"$script\"\n"
        "  fi\n"
        "done\n"
        "exec bash \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    # subprocess.run resolves "docker" via PATH — prepend shim dir. Also
    # pin shutil.which so docker_available() sees it.
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    real_which = shutil.which

    def which(name, *a, **kw):
        if name == "docker":
            return str(shim)
        return real_which(name, *a, **kw)

    monkeypatch.setattr("shutil.which", which)
    return shim


class TestResolveBackend:
    def test_no_docker_auto_falls_back_local(self, no_docker):
        assert resolve_workspace_backend("auto") == "local"

    def test_no_docker_docker_requested_falls_back_local(self, no_docker):
        assert resolve_workspace_backend("docker") == "local"

    def test_local_forced(self, fake_docker):
        assert resolve_workspace_backend("local") == "local"

    def test_docker_available_selected(self, fake_docker):
        assert resolve_workspace_backend("auto") == "docker"

    def test_env_backend_preferred_when_no_arg(self, fake_docker, monkeypatch):
        monkeypatch.setenv("FUSION_WORKSPACE_BACKEND", "local")
        assert resolve_workspace_backend("") == "local"

    def test_invalid_mode_ignored(self, no_docker, monkeypatch):
        assert normalize_workspace_mode("vmware") == ""
        monkeypatch.setenv("FUSION_WORKSPACE_BACKEND", "bogus")
        assert resolve_workspace_backend("") == "local"

    def test_docker_available_false_without_binary(self, no_docker):
        assert docker_available(force=True) is False


class TestRunInContainer:
    async def test_container_command_construction(self, fake_docker, tmp_path, monkeypatch):
        calls = []
        real_run = subprocess.run

        def spy_run(cmd, *a, **kw):
            calls.append(cmd)
            kw.setdefault("capture_output", True)
            kw.setdefault("text", True)
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr("agent_runtime.workspace.subprocess.run", spy_run)
        workdir = tmp_path / "ws"
        workdir.mkdir()
        result = run_in_container(["bash", "-lc", "echo hi"], str(workdir), 30, exec_id="t1")
        assert result.success is True
        assert "hi" in result.stdout
        cmd = calls[0]
        assert cmd[:7] == ["docker", "run", "--rm", "--network", "none", "--memory", "512m"]
        assert "--pids-limit" in cmd and "256" in cmd
        mount_idx = cmd.index("-v")
        assert cmd[mount_idx + 1].endswith(":/workspace")
        assert cmd[cmd.index("-w") + 1] == "/workspace"
        assert workspace_image() in cmd

    async def test_container_timeout(self, fake_docker, tmp_path):
        result = run_in_container(["bash", "-lc", "sleep 5"], "", timeout=1, exec_id="t2")
        assert result.timed_out is True
        assert result.success is False

    def test_container_inner_argv(self):
        assert container_inner_argv("python", "/workspace/script.py") == ["python3", "/workspace/script.py"]
        assert container_inner_argv("shell", "/workspace/script.sh") == ["bash", "/workspace/script.sh"]


class TestTerminalToolBackends:
    async def test_terminal_docker_backend(self, fake_docker):
        tool = TerminalTool()
        out = await tool.execute(command="echo container-ok", workspace="docker")
        assert "container-ok" in out
        assert not out.startswith("Error")

    async def test_terminal_docker_unavailable_falls_back_local(self, no_docker):
        tool = TerminalTool()
        out = await tool.execute(command="echo local-ok", workspace="docker")
        assert "local-ok" in out
        assert not out.startswith("Error")

    async def test_terminal_default_local(self, fake_docker):
        # workspace unset -> auto. With docker present auto picks container,
        # but catastrophic-pattern blacklist must still apply on both paths.
        tool = TerminalTool()
        out = await tool.execute(command="rm -rf /", workspace="docker")
        assert out.startswith("Error: command matched a catastrophic pattern")

    async def test_terminal_invalid_workspace_ignored(self, fake_docker):
        tool = TerminalTool()
        out = await tool.execute(command="echo ok", workspace="bogus")
        assert "ok" in out


class TestCodeSandboxBackends:
    async def test_code_sandbox_container_python(self, fake_docker):
        sb = CodeSandbox(timeout=30, use_sandbox=True, workspace="docker")
        assert sb._backend == "docker"
        result = sb.execute("print('in-container')", "python")
        assert result.success is True, result.stderr
        assert "in-container" in result.stdout

    async def test_code_sandbox_docker_unavailable_falls_back_sandboxexec(self, no_docker):
        sb = CodeSandbox(timeout=30, use_sandbox=True, workspace="docker")
        assert sb._backend == "local"
        result = sb.execute("print('fallback-ok')", "python")
        assert result.success is True, result.stderr
        assert "fallback-ok" in result.stdout

    async def test_code_sandbox_non_container_language_stays_local(self, fake_docker):
        sb = CodeSandbox(timeout=30, use_sandbox=True, workspace="docker")
        # javascript not in default image -> local path even with docker
        result = sb.execute("console.log('js-ok')", "javascript")
        assert result.success is True, result.stderr
        assert "js-ok" in result.stdout

    async def test_code_sandbox_ast_check_still_runs_in_docker_mode(self, fake_docker):
        sb = CodeSandbox(timeout=30, use_sandbox=True, workspace="docker")
        result = sb.execute("import os\nprint(os.getcwd())", "python")
        assert result.success is False
        assert "Safety check failed" in result.stderr

    async def test_code_execute_tool_workspace_param(self, fake_docker):
        tool = CodeExecuteTool()
        out = await tool.execute(code="print('ce-ok')", workspace="docker")
        assert "ce-ok" in out
        assert not out.startswith("Error")

    async def test_code_execute_tool_fallback(self, no_docker):
        tool = CodeExecuteTool()
        out = await tool.execute(code="print('ce-fb')", workspace="docker")
        assert "ce-fb" in out
        assert not out.startswith("Error")

    async def test_code_sandbox_tool_workspace_param(self, fake_docker):
        tool = CodeSandboxTool()
        out = await tool.execute(code="print('cs-ok')", language="python", workspace="docker")
        assert "cs-ok" in out
        assert not out.startswith("Error")


class TestToolConfigSelection:
    def test_manifest_config_reaches_tool_kwargs(self):
        # runtime._merge_tool_config_defaults merges tool config as kwargs
        # defaults; terminal honors workspace from config identically to
        # an explicit call arg.
        from agent_runtime.runtime import AgentRuntime

        class _TC:
            name = "terminal"
            config = {"workspace": "docker"}

        class _Def:
            tools = [_TC()]

        configs = AgentRuntime.build_tool_configs(_Def())
        assert configs["terminal"]["workspace"] == "docker"
        rt = AgentRuntime()
        rt.set_tool_configs(_Def())
        merged = rt._merge_tool_config_defaults("terminal", {"command": "echo hi"})
        assert merged["workspace"] == "docker"
        assert merged["command"] == "echo hi"
