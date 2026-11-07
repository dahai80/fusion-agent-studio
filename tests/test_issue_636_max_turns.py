import json

import pytest

from agent_runtime.builtin_agents import _create_builtin_agent
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.seat_pack import load_seat_specs
from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from agent_runtime.task_executor import TaskExecutor
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry


def test_env_mock_seat_has_iteration_budget():
    # #636: env-mock task shape (stubs + fixtures + compose + seed, each
    # versioned) needs more than the 8-turn subagent default.
    spec = load_seat_specs()["delivery-env-mock"]
    assert int(spec.get("max_iterations", 0)) >= 16


def test_builtin_agent_carries_max_iterations(tmp_path):
    from agent_runtime.agent_package import AgentPackage

    seat = dict(load_seat_specs()["delivery-env-mock"])
    pkg = AgentPackage(tmp_path / "agent")
    _create_builtin_agent(seat, pkg)
    manifest = pkg.load_manifest()
    assert manifest.max_iterations == 24


class _ExecDaemon:
    _agents_index_loaded = True
    _agents = {
        "delivery-env-mock": {
            "model": "m",
            "tools": ["file_write"],
            "max_iterations": 24,
        },
        "plain-agent": {"model": "", "tools": []},
    }

    def _load_agents_index(self):
        pass


def _tc(name, args):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_executor_spec_carries_manifest_budget():
    # #636: graph path honors manifest.max_iterations; the subagent path
    # (task executor -> subagent.run_task) must too.
    ex = TaskExecutor.__new__(TaskExecutor)
    ex._daemon = _ExecDaemon()
    spec = ex._spec_for_agent("delivery-env-mock")
    assert spec["max_turns"] == 24


def test_executor_spec_no_budget_key_when_default():
    ex = TaskExecutor.__new__(TaskExecutor)
    ex._daemon = _ExecDaemon()
    spec = ex._spec_for_agent("plain-agent")
    assert "max_turns" not in spec


class _LoopTool(BaseTool):
    name = "loop_tool"
    description = "Stub tool that the fake LLM calls forever"
    parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return f"loop:{tag}"


class _EndlessToolCallClient:
    async def chat(self, model, messages, tools=None, **kwargs):
        return LLMResponse(
            content="",
            tool_calls=[_tc("loop_tool", {"tag": "x"})],
            usage={"prompt_tokens": 5, "completion_tokens": 5},
            finish_reason="tool_calls",
        )

    async def chat_stream(self, messages, model="", **kwargs):
        from server.fusion_mlx_client import StreamChunk

        yield StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        return []


@pytest.mark.asyncio
async def test_max_turns_exhaustion_error_is_enriched(tmp_path):
    # #636: bare "Max iterations exceeded" hid turns/tokens/report state
    # from consumers; the enriched error must say budget-exhausted-mid-work.
    gw = LLMGateway()
    gw.set_default_client(_EndlessToolCallClient())
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    reg = ToolRegistry()
    reg.register(_LoopTool())
    rt = AgentRuntime(llm_gateway=gw, tool_registry=reg)
    runner = SubAgentRunner(rt)
    report = await runner.run(
        SubAgentSpec(name="envmock", task="make mock pack", max_turns=2, tools=["loop_tool"])
    )
    assert report.partial is True
    assert report.ok is False
    assert "Max iterations exceeded" in report.error
    assert "budget exhausted mid-work" in report.error
    assert "max_iterations" in report.error
