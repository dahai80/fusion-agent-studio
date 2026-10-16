"""Tests for N4 subagent protocol (issue #341).

Covers: spec/report roundtrip, registry isolation + tool allowlist, depth-1
guard, fork mode, token budget, subagent_spawn tool, template registry,
lifecycle events, and SUBAGENT_START hooks.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.subagent import (
    SubAgentRegistry,
    SubAgentReport,
    SubAgentRunner,
    SubAgentSpec,
)
from server.fusion_mlx_client import LLMResponse, StreamChunk
from tools.base import BaseTool
from tools.registry import ToolRegistry


class T1Tool(BaseTool):
    name = "t1"
    description = "Stub tool one"
    parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return f"t1:{tag}"


class T2Tool(BaseTool):
    name = "t2"
    description = "Stub tool two"
    parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return f"t2:{tag}"


def _llm_response(content="", tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
        finish_reason="stop",
    )


class ToolCallClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[list[dict]] = []

    async def chat(self, model, messages, tools=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return _llm_response(content="done")

    async def chat_stream(self, messages, model="", **kwargs):
        content = ""
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            content = resp.content or ""
        yield StreamChunk(delta_content=content, delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        if isinstance(input, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in input]


def _make_parent(responses, tools=None):
    client = ToolCallClient(responses)
    gw = LLMGateway()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    reg = tools if tools is not None else ToolRegistry()
    rt = AgentRuntime(llm_gateway=gw, tool_registry=reg)
    return rt, client


def _tc(name, args):
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class TestSpecReportRoundtrip:
    def test_spec_roundtrip(self):
        spec = SubAgentSpec(
            name="researcher",
            task="summarize",
            model="m1",
            tools=["t1"],
            system_prompt="be terse",
            max_turns=3,
            budget_tokens=100,
            fork=True,
            team="alpha",
        )
        d = spec.to_dict()
        assert d["team"] == "alpha"
        spec2 = SubAgentSpec.from_dict(d)
        assert spec2 == spec

    def test_report_roundtrip(self):
        report = SubAgentReport(
            name="researcher", ok=True, report="done", turns=2,
            tokens_used=42, error="", team="alpha", duration_ms=99,
        )
        d = report.to_dict()
        assert d["ok"] is True
        report2 = SubAgentReport.from_dict(d)
        assert report2 == report

    def test_report_from_dict_defaults(self):
        r = SubAgentReport.from_dict({"name": "x", "ok": "yes"})
        assert r.name == "x"
        assert r.ok is True
        assert r.turns == 0

    def test_spec_defaults(self):
        spec = SubAgentSpec(name="n", task="t")
        assert spec.max_turns == 8
        assert spec.tools == []
        assert spec.budget_tokens == 0
        assert spec.fork is False


class TestSubAgentRegistry:
    def test_register_get_list(self):
        r = SubAgentRegistry()
        spec = SubAgentSpec(name="expert", task="default task")
        r.register(spec)
        assert r.get("expert") is spec
        assert r.list() == ["expert"]

    def test_unknown_get_returns_none(self):
        r = SubAgentRegistry()
        assert r.get("nope") is None
        assert r.list() == []

    def test_register_requires_name(self):
        r = SubAgentRegistry()
        with pytest.raises(ValueError):
            r.register(SubAgentSpec(name="", task="t"))

    def test_overwrite(self):
        r = SubAgentRegistry()
        r.register(SubAgentSpec(name="a", task="1"))
        r.register(SubAgentSpec(name="a", task="2"))
        assert r.get("a").task == "2"
        assert r.list() == ["a"]


class TestIsolation:
    @pytest.mark.asyncio
    async def test_no_tools_report_only(self):
        rt, client = _make_parent([_llm_response(content="final report text")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="solo", task="write report"))
        assert report.ok is True
        assert report.report == "final report text"
        assert report.error == ""
        assert report.name == "solo"
        assert report.duration_ms >= 0
        sub_reg = runner.last_runtime.tools
        assert sub_reg.count == 0
        assert rt.tools.count == 0
        assert runner.last_ctx.session_id == "sub:solo"
        assert runner.last_ctx.metadata.get("subagent") == "solo"

    @pytest.mark.asyncio
    async def test_sub_session_id_uses_parent(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="p1")
        report = await runner.run(SubAgentSpec(name="w", task="t"), parent_ctx=parent_ctx)
        assert report.ok is True
        assert runner.last_ctx.session_id == "p1:sub:w"

    @pytest.mark.asyncio
    async def test_turns_counted(self):
        resp_tool = _llm_response(tool_calls=[_tc("t1", {"tag": "a"})])
        rt, client = _make_parent(
            [resp_tool, _llm_response(content="final")],
            tools=_tool_registry(),
        )
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="c", task="t", tools=["t1"]))
        assert report.ok is True
        assert report.turns == 2
        assert report.report == "final"
        assert report.tokens_used == 20

    @pytest.mark.asyncio
    async def test_tool_output_never_in_report(self):
        resp_tool = _llm_response(tool_calls=[_tc("t1", {"tag": "secret"})])
        rt, _ = _make_parent(
            [resp_tool, _llm_response(content="report only")],
            tools=_tool_registry(),
        )
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="c", task="t", tools=["t1"]))
        assert report.ok is True
        assert report.report == "report only"
        assert "secret" not in report.report
        assert "t1:secret" not in report.report


def _tool_registry():
    reg = ToolRegistry()
    reg.register(T1Tool())
    reg.register(T2Tool())
    return reg


class TestToolAllowlist:
    @pytest.mark.asyncio
    async def test_allowlist_subset(self):
        rt, _ = _make_parent([_llm_response(content="ok")], tools=_tool_registry())
        runner = SubAgentRunner(rt)
        report = await runner.run(
            SubAgentSpec(name="a", task="t", tools=["t1"]),
            parent_ctx=None,
        )
        assert report.ok is True
        sub_reg = runner.last_runtime.tools
        assert sub_reg.count == 1
        assert sub_reg.has("t1")
        assert not sub_reg.has("t2")

    @pytest.mark.asyncio
    async def test_missing_allowlisted_tool_fails(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="a", task="t", tools=["missing_tool"]))
        assert report.ok is False
        assert "missing_tool" in report.error
        assert report.report == ""

    @pytest.mark.asyncio
    async def test_control_flow_tools_filtered(self):
        rt, _ = _make_parent([_llm_response(content="ok")], tools=_tool_registry())
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="a", task="t", tools=["register_tool", "t1"]))
        assert report.ok is True
        sub_reg = runner.last_runtime.tools
        assert sub_reg.has("t1")
        assert not sub_reg.has("register_tool")
        assert sub_reg.count == 1


class TestDepthGuard:
    @pytest.mark.asyncio
    async def test_sub_graph_denied_in_sub_run(self):
        inner = AgentGraph(name="inner")
        inner.add_node("start", NodeConfig(type="start", label="s"))
        inner.add_node("llm1", NodeConfig(type="llm", label="l", model="test-model"))
        inner.add_node("end", NodeConfig(type="end", label="e"))
        inner.add_edge("start", "llm1")
        inner.add_edge("llm1", "end")

        rt, client = _make_parent(
            [_llm_response(tool_calls=[_tc("__sub_graph__", {"graph_json": inner.to_json()})])]
        )
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="d", task="go deeper"))
        assert report.ok is True
        assert client._idx == 1, "no second LLM call after sub-graph denial"
        errors = [e for e in runner.last_events if e.type == AgentEventType.ERROR]
        assert errors, "depth gate must yield an ERROR event"
        assert "depth limit" in errors[0].content
        assert report.report == ""


class TestForkMode:
    @pytest.mark.asyncio
    async def test_fork_seeds_parent_messages(self):
        rt, _ = _make_parent([_llm_response(content="fork report")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="p1")
        parent_ctx.add_message("user", "hello")
        parent_ctx.add_message("assistant", "hi there")
        report = await runner.run(
            SubAgentSpec(name="f", task="continue", fork=True),
            parent_ctx=parent_ctx,
        )
        assert report.ok is True
        assert report.report == "fork report"
        msgs = runner.last_ctx.messages
        assert msgs[0]["content"] == "hello"
        assert msgs[1]["content"] == "hi there"
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert user_msgs[-1]["content"] == "continue"

    @pytest.mark.asyncio
    async def test_fork_window_last_20(self):
        rt, _ = _make_parent([_llm_response(content="r")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="p2")
        for i in range(30):
            parent_ctx.add_message("user", f"u{i}")
            parent_ctx.add_message("assistant", f"a{i}")
        await runner.run(
            SubAgentSpec(name="f", task="t", fork=True),
            parent_ctx=parent_ctx,
        )
        seeded = [m for m in runner.last_ctx.messages if m.get("role") in ("user", "assistant")]
        assert len(seeded) <= 22
        assert seeded[0]["content"] == "u20"

    @pytest.mark.asyncio
    async def test_no_fork_fresh_messages(self):
        rt, _ = _make_parent([_llm_response(content="r")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="p3")
        parent_ctx.add_message("user", "old")
        await runner.run(SubAgentSpec(name="nf", task="t"), parent_ctx=parent_ctx)
        msgs = runner.last_ctx.messages
        assert [m["content"] for m in msgs if m.get("role") == "user"] == ["t"]

    @pytest.mark.asyncio
    async def test_fork_false_ignores_parent_history(self):
        rt, _ = _make_parent([_llm_response(content="r")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="p4")
        parent_ctx.add_message("user", "history that must not leak")
        report = await runner.run(SubAgentSpec(name="nf2", task="t"), parent_ctx=parent_ctx)
        assert report.ok is True
        all_text = json.dumps(runner.last_ctx.messages, ensure_ascii=False)
        assert "history that must not leak" not in all_text


class TestBudget:
    @pytest.mark.asyncio
    async def test_budget_exceeded_stops_gracefully(self):
        # Budget check runs once per dispatch iteration (after the LLM node
        # returns). A single no-tool call with usage 10 vs cap 5 trips it.
        rt, _ = _make_parent([_llm_response(content="final")])
        runner = SubAgentRunner(rt)
        report = await runner.run(
            SubAgentSpec(name="b", task="t", budget_tokens=5)
        )
        assert report.ok is True
        assert report.report == "final"
        assert report.tokens_used == 10
        exceeded = [
            e for e in runner.last_events
            if e.type == AgentEventType.TOKEN_BUDGET_EXCEEDED
        ]
        assert exceeded, "token cap mechanism must fire"
        assert report.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_no_budget_runs_to_completion(self):
        rt, _ = _make_parent([_llm_response(content="done")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="nb", task="t"))
        assert report.ok is True
        exceeded = [
            e for e in runner.last_events
            if e.type == AgentEventType.TOKEN_BUDGET_EXCEEDED
        ]
        assert not exceeded


class TestSubAgentTool:
    @pytest.mark.asyncio
    async def test_execute_returns_report_json(self):
        from tools.subagent_tool import SubAgentTool

        captured = {}

        class FakeRunner:
            async def run(self, spec, parent_ctx=None):
                captured["spec"] = spec
                return SubAgentReport(name=spec.name, ok=True, report="sub did it", turns=1)

        tool = SubAgentTool(runner_provider=lambda: FakeRunner())
        out = await tool.execute(name="x", task="do it")
        data = json.loads(out)
        assert data["ok"] is True
        assert data["report"] == "sub did it"
        assert captured["spec"].name == "x"
        assert captured["spec"].task == "do it"

    @pytest.mark.asyncio
    async def test_missing_runner_returns_error(self):
        from tools.subagent_tool import SubAgentTool

        tool = SubAgentTool(runner_provider=lambda: None)
        out = await tool.execute(name="x", task="t")
        assert out == "Error: subagent runner not wired"

    @pytest.mark.asyncio
    async def test_runner_provider_raises_returns_error(self):
        from tools.subagent_tool import SubAgentTool

        def boom():
            raise RuntimeError("wiring failed")

        tool = SubAgentTool(runner_provider=boom)
        out = await tool.execute(name="x", task="t")
        assert out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_template_merge_via_spec_provider(self):
        from tools.subagent_tool import SubAgentTool

        registry = SubAgentRegistry()
        registry.register(
            SubAgentSpec(name="expert", task="default", system_prompt="be terse", team="research")
        )
        captured = {}

        class FakeRunner:
            async def run(self, spec, parent_ctx=None):
                captured["spec"] = spec
                return SubAgentReport(name=spec.name, ok=True, report="ok")

        tool = SubAgentTool(
            runner_provider=lambda: FakeRunner(),
            spec_provider=registry.get,
        )
        out = await tool.execute(name="expert", task="custom task")
        data = json.loads(out)
        assert data["ok"] is True
        spec = captured["spec"]
        assert spec.name == "expert"
        assert spec.task == "custom task"
        assert spec.system_prompt == "be terse"
        assert spec.team == "research"

    @pytest.mark.asyncio
    async def test_overrides_applied(self):
        from tools.subagent_tool import SubAgentTool

        captured = {}

        class FakeRunner:
            async def run(self, spec, parent_ctx=None):
                captured["spec"] = spec
                return SubAgentReport(name=spec.name, ok=True, report="ok")

        tool = SubAgentTool(runner_provider=lambda: FakeRunner())
        await tool.execute(name="x", task="t", model="m9", tools=["t1"], max_turns=3, fork=True)
        spec = captured["spec"]
        assert spec.model == "m9"
        assert spec.tools == ["t1"]
        assert spec.max_turns == 3
        assert spec.fork is True

    @pytest.mark.asyncio
    async def test_missing_name_or_task(self):
        from tools.subagent_tool import SubAgentTool

        class FakeRunner:
            async def run(self, spec, parent_ctx=None):
                return SubAgentReport(name=spec.name, ok=True, report="ok")

        tool = SubAgentTool(runner_provider=lambda: FakeRunner())
        out = await tool.execute(name="", task="t")
        assert out.startswith("Error:")
        out2 = await tool.execute(name="x", task="")
        assert out2.startswith("Error:")


class TestLifecycleAndHooks:
    @pytest.mark.asyncio
    async def test_lifecycle_events_emitted(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="lc", task="t", team="alpha"))
        assert report.ok is True
        events = rt.lifecycle_bus.replay(domain="agent")
        names = [e.event for e in events]
        assert "subagent_start" in names
        assert "subagent_stop" in names
        start = next(e for e in events if e.event == "subagent_start")
        stop = next(e for e in events if e.event == "subagent_stop")
        assert start.payload["name"] == "lc"
        assert start.payload["model"]
        assert start.payload["fork"] is False
        assert start.payload["team"] == "alpha"
        assert stop.payload["name"] == "lc"
        assert stop.payload["ok"] is True
        assert "duration_ms" in stop.payload

    @pytest.mark.asyncio
    async def test_lifecycle_session_id_scoped(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        parent_ctx = AgentContext(session_id="ps")
        await runner.run(SubAgentSpec(name="lc2", task="t"), parent_ctx=parent_ctx)
        events = rt.lifecycle_bus.replay(session_id="ps:sub:lc2", domain="agent")
        names = [e.event for e in events]
        assert "subagent_start" in names
        assert "subagent_stop" in names

    @pytest.mark.asyncio
    async def test_subagent_start_hook_fired(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        fired = []

        def cb(payload):
            fired.append(payload)

        engine = HookEngine(auto_register_decorators=False)
        engine.register(HookConfig(event="SUBAGENT_START", type="callback", callback=cb))
        rt.hooks = engine
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="hk", task="t"))
        assert report.ok is True
        assert fired, "SUBAGENT_START hook must fire"
        assert fired[0]["name"] == "hk"

    @pytest.mark.asyncio
    async def test_subagent_stop_hook_fired(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        fired = []

        def cb(payload):
            fired.append(payload)

        engine = HookEngine(auto_register_decorators=False)
        engine.register(HookConfig(event="SUBAGENT_STOP", type="callback", callback=cb))
        rt.hooks = engine
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="hk2", task="t"))
        assert report.ok is True
        assert fired and fired[0]["ok"] is True

    @pytest.mark.asyncio
    async def test_error_run_emits_stop_event(self):
        class BoomGatewayClient:
            async def chat(self, model, messages, tools=None, **kwargs):
                raise RuntimeError("llm down")

            async def chat_stream(self, messages, model="", **kwargs):
                raise RuntimeError("llm down")
                yield

        client = BoomGatewayClient()
        gw = LLMGateway()
        gw.set_default_client(client)
        rt = AgentRuntime(llm_gateway=gw, tool_registry=ToolRegistry())
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="boom", task="t"))
        assert report.ok is False
        assert "llm down" in report.error
        events = rt.lifecycle_bus.replay(domain="agent")
        names = [e.event for e in events]
        assert "subagent_start" in names and "subagent_stop" in names
