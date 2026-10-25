import time

import pytest

from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.permissions import PermissionEngine, PermissionRule
from agent_runtime.context import AgentContext
from agent_runtime.funnels import FunnelOutcome, LLMFunnel, ToolFunnel
from agent_runtime.graph import NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


def _runtime() -> AgentRuntime:
    return AgentRuntime(llm_gateway=None, tool_registry=ToolRegistry())


class TestParallelAdmissionCtx:
    @pytest.mark.asyncio
    async def test_parallel_admit_receives_node_ctx(self):
        rt = _runtime()
        seen = {}

        class Recorder:
            async def admit(self, ctx=None, **kwargs):
                seen["ctx"] = ctx
                seen["tool"] = kwargs.get("tool_name")
                return FunnelOutcome(allowed=True, args=kwargs.get("args") or {})

        rt.tool_funnel = Recorder()
        ctx = AgentContext()
        node = NodeConfig(type="llm", label="n1")
        result, is_error = await rt._exec_parallel_tool(ctx, node, "missing_tool", {"a": 1})
        assert seen["ctx"] is ctx
        assert seen["tool"] == "missing_tool"
        assert is_error is True

    @pytest.mark.asyncio
    async def test_parallel_plan_gate_denies_write_tool(self):
        rt = _runtime()
        rt.tool_funnel = ToolFunnel(permissions=PermissionEngine())
        ctx = AgentContext()
        ctx.plan_mode = True
        node = NodeConfig(type="llm", label="n1")
        result, is_error = await rt._exec_parallel_tool(ctx, node, "write_file", {"path": "/tmp/x"})
        assert is_error is True
        assert "permission" in result.lower() or "blocked" in result.lower()

    @pytest.mark.asyncio
    async def test_parallel_role_rule_applies(self):
        engine = PermissionEngine(
            rules=[PermissionRule(action="deny", tool="shell_exec", roles=["reviewer"])]
        )
        rt = _runtime()
        rt.tool_funnel = ToolFunnel(permissions=engine)
        node = NodeConfig(type="llm", label="n1")

        worker = AgentContext()
        worker.role = "worker"
        _, worker_err = await rt._exec_parallel_tool(worker, node, "shell_exec", {"cmd": "ls"})
        assert worker_err is True

        reviewer = AgentContext()
        reviewer.role = "reviewer"
        result, err = await rt._exec_parallel_tool(reviewer, node, "shell_exec", {"cmd": "ls"})
        assert err is True
        assert "permission" in result.lower()


class _FakeResponse:
    content = "hello"
    tool_calls = []
    usage = {"prompt_tokens": 3, "completion_tokens": 2}
    finish_reason = "stop"
    model = "fake"


class _FakeGateway:
    async def chat(self, **kwargs):
        return _FakeResponse()

    def chat_stream(self, **kwargs):
        raise AssertionError("non-stream path expected")


class TestLlmFunnelSingleEntry:
    @pytest.mark.asyncio
    async def test_chat_engine_emits_llm_events(self):
        from agent_runtime.chat_engine import ChatEngine

        rt = _runtime()
        rt.llm_gateway = _FakeGateway()
        engine = ChatEngine(runtime=rt)
        session = engine.create_session()
        events = [e async for e in engine.send(session.id, "hi")]
        assert any(e.type.value == "token" for e in events)

        names = [e.event for e in rt.lifecycle_bus.replay(domain="agent")]
        assert "llm_before" in names
        assert "llm_after" in names

    @pytest.mark.asyncio
    async def test_workflow_engine_emits_llm_events(self):
        from agent_runtime.workflow_engine import WorkflowEngine

        engine = WorkflowEngine(llm_gateway=_FakeGateway())
        out = await engine._run_agent({"name": "w1", "system_prompt": "sys"}, "hi")
        assert out == "hello"
        names = [e.event for e in engine.llm_funnel.lifecycle.replay(domain="agent")]
        assert "llm_before" in names
        assert "llm_after" in names


class TestHookBudgetFailClosed:
    @pytest.mark.asyncio
    async def test_security_event_blocks_when_budget_exhausted(self):
        eng = HookEngine(auto_register_decorators=False, max_fire_ms=20)

        def slow(payload):
            time.sleep(0.1)
            return {"decision": "approve"}

        eng.register(HookConfig(event="PRE_TOOL_USE", callback=slow))
        eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "approve"}))
        res = await eng.fire("PRE_TOOL_USE", {}, tool_name="shell_exec")
        assert res.decision == "block"
        assert res.continue_loop is False
        assert "budget" in res.reason

    @pytest.mark.asyncio
    async def test_non_security_event_still_skips(self):
        eng = HookEngine(auto_register_decorators=False, max_fire_ms=20)

        def slow(payload):
            time.sleep(0.1)
            return {"decision": "approve"}

        eng.register(HookConfig(event="SESSION_START", callback=slow))
        eng.register(HookConfig(event="SESSION_START", callback=lambda p: {"decision": "approve"}))
        res = await eng.fire("SESSION_START", {})
        assert res.decision == "approve"
        assert res.continue_loop is True

    @pytest.mark.asyncio
    async def test_tool_funnel_blocks_on_exhausted_security_hooks(self):
        eng = HookEngine(auto_register_decorators=False, max_fire_ms=20)

        def slow(payload):
            time.sleep(0.1)
            return {"decision": "approve"}

        eng.register(HookConfig(event="PRE_TOOL_USE", callback=slow))
        eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "approve"}))
        funnel = ToolFunnel(hooks=eng, permissions=PermissionEngine())
        outcome = await funnel.admit(ctx=AgentContext(), tool_name="shell_exec", args={})
        assert outcome.allowed is False
        assert outcome.blocked_by == "hook"


def test_llm_funnel_importable():
    assert LLMFunnel is not None
