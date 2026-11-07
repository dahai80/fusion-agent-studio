import json

import pytest

from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from tools.base import BaseTool
from tools.registry import ToolRegistry
from tools.terminal_tools import TerminalTool, _find_host_path


def test_find_host_path_matches_host_roots():
    # #640 defect 1: host paths are unreachable in the workspace container.
    assert _find_host_path("cat /Users/dahai/x.txt") == "/Users/dahai/x.txt"
    assert _find_host_path("ls ~/.fusion-agent-studio") == "~/.fusion-agent-studio"
    assert _find_host_path("rm -rf /app/cache") == "/app/cache"
    assert _find_host_path("cat /workspace/a.md") == ""
    assert _find_host_path("echo $HOME") == ""


@pytest.mark.asyncio
async def test_container_blocks_host_path_with_scope_message(monkeypatch):
    # fail-fast must return the actionable boundary message, not run.
    import agent_runtime.workspace as ws

    monkeypatch.setattr(ws, "resolve_workspace_backend", lambda req="": "docker")
    tool = TerminalTool()
    out = await tool.execute(command="cat /Users/dahai/secret.txt", workspace="docker")
    assert out.startswith("Error: sandboxed: workspace-scoped access only")
    assert "/Users/dahai/secret.txt" in out
    assert "/workspace" in out


@pytest.mark.asyncio
async def test_container_error_carries_scope_hint(monkeypatch):
    import agent_runtime.workspace as ws
    from agent_runtime.code_sandbox import SandboxResult

    monkeypatch.setattr(ws, "resolve_workspace_backend", lambda req="": "docker")

    def _fake_run(argv, workdir="", timeout=30, exec_id=""):
        return SandboxResult(success=False, exit_code=1, stderr="cat: no.txt: No such file")

    monkeypatch.setattr(ws, "run_in_container", _fake_run)
    tool = TerminalTool()
    out = await tool.execute(command="cat no.txt", workspace="docker")
    assert "Error: Command exited with code 1" in out
    assert "workspace-scoped access only" in out


class _StallTool(BaseTool):
    name = "stall_tool"
    description = "Stub tool the fake LLM calls until the budget dies"
    parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return "stalled"


class _ExhaustThenReportClient:
    def __init__(self):
        self.report_asked = False

    async def chat(self, model, messages, tools=None, **kwargs):
        last = messages[-1]
        if isinstance(last, dict) and "turn budget is exhausted" in str(last.get("content", "")):
            self.report_asked = True
            assert tools is None
            from server.fusion_mlx_client import LLMResponse

            return LLMResponse(
                content="Delivered: workspaces/e2eprobe-mock/ (compose overlay, seed).",
                tool_calls=[],
                usage={"prompt_tokens": 5, "completion_tokens": 5},
                finish_reason="stop",
            )
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "stall_tool", "arguments": json.dumps({"tag": "x"})},
                }
            ],
            usage={"prompt_tokens": 5, "completion_tokens": 5},
            finish_reason="tool_calls",
        )

    async def chat_stream(self, messages, model="", **kwargs):
        from server.fusion_mlx_client import StreamChunk

        yield StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        return []


@pytest.mark.asyncio
async def test_budget_exhaustion_reserves_report_turn():
    # #640 defect 2: max_iterations + report_len=0 after disk writes must
    # still produce a report via one tool-free reserve turn.
    gw = LLMGateway()
    client = _ExhaustThenReportClient()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    reg = ToolRegistry()
    reg.register(_StallTool())
    rt = AgentRuntime(llm_gateway=gw, tool_registry=reg)
    runner = SubAgentRunner(rt)
    report = await runner.run(
        SubAgentSpec(name="envmock", task="make mock pack", max_turns=2, tools=["stall_tool"])
    )
    assert report.partial is True
    assert client.report_asked is True
    assert "Delivered" in report.report
    assert report.error != ""
