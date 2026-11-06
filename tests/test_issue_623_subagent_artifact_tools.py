import pytest

from agent_runtime._runtime_nodes import _NodeExecutorsMixin
from agent_runtime.context import AgentContext
from agent_runtime.daemon_server import DaemonServer
from tools.artifact_fc_tools import ArtifactCreateTool


def test_artifact_tools_wired_in_daemon_runtime(tmp_path, monkeypatch):
    # #623: registry instances shipped _manager=None — every artifact_create
    # errored "ArtifactManager not available" and seat agents thrashed.
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path))
    monkeypatch.setenv("FUSION_DAEMON_SOCK", str(tmp_path / "d.sock"))
    daemon = DaemonServer(socket_path=str(tmp_path / "d.sock"))
    runtime = daemon._get_runtime()
    tool = runtime.tools.get("artifact_create")
    assert tool is not None
    assert tool._manager is not None


@pytest.mark.asyncio
async def test_artifact_create_works_after_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path))
    monkeypatch.setenv("FUSION_DAEMON_SOCK", str(tmp_path / "d.sock"))
    daemon = DaemonServer(socket_path=str(tmp_path / "d.sock"))
    runtime = daemon._get_runtime()
    tool = runtime.tools.get("artifact_create")
    out = await tool.execute(
        name="t", artifact_type="document", content="# hello", agent_id="a1"
    )
    assert "Error" not in out


def test_registry_instances_start_unwired():
    # regression anchor: factory tools must keep _manager=None so wiring
    # stays explicit (and the daemon wiring test above stays meaningful).
    tool = ArtifactCreateTool()
    assert tool._manager is None


def _ctx():
    return AgentContext(session_id="s623")


def test_first_identical_error_unchanged():
    ctx = _ctx()
    out = _NodeExecutorsMixin._escalate_repeat_tool_error(
        ctx, "artifact_create", "Error: ArtifactManager not available"
    )
    assert out == "Error: ArtifactManager not available"


def test_repeated_error_gets_fallback_hint():
    ctx = _ctx()
    err = "Error: ArtifactManager not available"
    _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "artifact_create", err)
    out = _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "artifact_create", err)
    assert "failed identically 2 times" in out
    assert "file_write" in out


def test_different_errors_counted_separately():
    ctx = _ctx()
    a = _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "t1", "Error: A")
    b = _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "t1", "Error: B")
    assert a == "Error: A" and b == "Error: B"


def test_second_error_of_other_tool_hinted():
    ctx = _ctx()
    _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "t2", "Error: X")
    out = _NodeExecutorsMixin._escalate_repeat_tool_error(ctx, "t2", "Error: X")
    assert "hint:" in out
