import json

import pytest

from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.subagent import SubAgentReport, SubAgentRunner, SubAgentSpec
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry

_LONG_REPORT = "# Delivery Report\n\n" + ("All deliverables written. " * 40)


def _tc(name, args):
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _WorkTool(BaseTool):
    name = "work_tool"
    description = "Stub tool the fake LLM calls until budget death"
    parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return "worked"


class _ExhaustThenReportClient:
    def __init__(self, report_text):
        self.report_text = report_text

    async def chat(self, model, messages, tools=None, **kwargs):
        return LLMResponse(
            content=self.report_text,
            tool_calls=[_tc("work_tool", {"tag": "x"})],
            usage={"prompt_tokens": 5, "completion_tokens": 5},
            finish_reason="tool_calls",
        )

    async def chat_stream(self, messages, model="", **kwargs):
        from server.fusion_mlx_client import StreamChunk

        yield StreamChunk(delta_content="", delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        return []


def _runner(client):
    gw = LLMGateway()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    reg = ToolRegistry()
    reg.register(_WorkTool())
    rt = AgentRuntime(llm_gateway=gw, tool_registry=reg)
    return SubAgentRunner(rt)


@pytest.mark.asyncio
async def test_substantive_report_on_exhaustion_completes_with_warning():
    # #643: 24/24 turns with a 7970-char report must not be judged failed —
    # the deliverable is done and described; verdict = completed + warning.
    runner = _runner(_ExhaustThenReportClient(_LONG_REPORT))
    report = await runner.run(
        SubAgentSpec(name="envmock", task="make mock pack", max_turns=2, tools=["work_tool"])
    )
    assert report.ok is True
    assert report.partial is True
    assert report.error == ""
    assert report.warning == "max turns reached, report-only completion"
    assert _LONG_REPORT[:50] in report.report


@pytest.mark.asyncio
async def test_empty_report_on_exhaustion_still_fails():
    # #636 behavior preserved: exhaustion without a substantive report fails.
    runner = _runner(_ExhaustThenReportClient(""))
    report = await runner.run(
        SubAgentSpec(name="envmock", task="make mock pack", max_turns=2, tools=["work_tool"])
    )
    assert report.ok is False
    assert report.partial is True
    assert "Max iterations exceeded" in report.error
    assert report.warning == ""


@pytest.mark.asyncio
async def test_trivial_report_on_exhaustion_still_fails():
    runner = _runner(_ExhaustThenReportClient("ok"))
    report = await runner.run(
        SubAgentSpec(name="envmock", task="t", max_turns=2, tools=["work_tool"])
    )
    assert report.ok is False
    assert report.warning == ""


def test_report_warning_roundtrip():
    r = SubAgentReport(name="x", ok=True, report="r", warning="max turns reached, report-only completion")
    d = r.to_dict()
    assert d["warning"] == "max turns reached, report-only completion"
    r2 = SubAgentReport.from_dict(d)
    assert r2.warning == r.warning


def test_report_from_dict_defaults_warning():
    r = SubAgentReport.from_dict({"name": "x", "ok": True})
    assert r.warning == ""
