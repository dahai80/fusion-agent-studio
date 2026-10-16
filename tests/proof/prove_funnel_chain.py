from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.funnels import LifecycleBus, LLMFunnel, ToolFunnel
from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.permissions import PermissionEngine, PermissionRule
from agent_runtime.safety import SafetyAction

logger = logging.getLogger("prove_funnel_chain")


@dataclass
class Ctx:
    session_id: str = "proof-session"
    plan_mode: bool = False


class Verdict:
    def __init__(self, action, approval=False, reason="proof"):
        self.action = action
        self.approval_flag = approval
        self.requires_approval = approval
        self.reason = reason


class SafetyStub:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def evaluate_action(self, **kwargs):
        self.calls += 1
        return self.verdict


class Budget:
    def __init__(self, exceeded):
        self.exceeded = exceeded

    def is_exceeded(self):
        return self.exceeded


class Audit:
    def __init__(self):
        self.entries = []

    def log(self, **kwargs):
        self.entries.append(kwargs)


def _block_hooks(reason):
    eng = HookEngine(auto_register_decorators=False)
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "block", "reason": reason}))
    return eng


def _rewrite_hooks(updated):
    eng = HookEngine(auto_register_decorators=False)
    eng.register(
        HookConfig(
            event="PRE_TOOL_USE",
            callback=lambda p: {"decision": "approve", "updated_input": updated},
        )
    )
    return eng


async def prove_hook_block():
    bus = LifecycleBus()
    funnel = ToolFunnel(hooks=_block_hooks("proof-deny"), permissions=PermissionEngine(), lifecycle_bus=bus)
    executed = []

    async def executor(args):
        executed.append(args)
        return "ok"

    result, is_error = await funnel.run_tool(Ctx(), "any_tool", {"a": 1}, executor)
    assert is_error and result.startswith("Blocked by hook"), result
    assert executed == []
    deny_events = [e for e in bus.replay(session_id="proof-session") if e.event == "tool_deny"]
    assert len(deny_events) == 1 and deny_events[0].payload["gate"] == "hook"
    logger.info("PASS hook block: executor skipped, tool_deny emitted")


async def prove_hook_rewrite():
    funnel = ToolFunnel(hooks=_rewrite_hooks('{"x": 1}'), permissions=PermissionEngine())
    out = await funnel.admit(ctx=Ctx(), tool_name="t1", args={"orig": True})
    assert out.allowed and out.args == {"x": 1}
    assert any(s.get("verdict") == "rewrite" for s in out.decision_log)
    logger.info("PASS hook rewrite: args=%s", out.args)


async def prove_safety_block_and_approval():
    block = SafetyStub(Verdict(SafetyAction.BLOCK, approval=False, reason="bad"))
    f1 = ToolFunnel(permissions=PermissionEngine(), safety_gateway=block)
    out1 = await f1.admit(ctx=Ctx(), tool_name="t1", args={})
    assert not out1.allowed and out1.blocked_by == "safety"

    ask = SafetyStub(Verdict(SafetyAction.BLOCK, approval=True, reason="human"))
    f2 = ToolFunnel(permissions=PermissionEngine(), safety_gateway=ask)
    out2 = await f2.admit(ctx=Ctx(), tool_name="t1", args={})
    assert out2.needs_approval and out2.safety_result is ask.verdict
    logger.info("PASS safety block + approval passthrough")


async def prove_permission_deny_first():
    engine = PermissionEngine(rules=[PermissionRule(action="deny", tool="dangerous_tool", layer="global")], mode="approved")
    safety = SafetyStub(Verdict(SafetyAction.BLOCK, approval=False))
    funnel = ToolFunnel(permissions=engine, safety_gateway=safety)
    out = await funnel.admit(ctx=Ctx(), tool_name="dangerous_tool", args={})
    assert not out.allowed and out.blocked_by == "permission"
    assert safety.calls == 0
    logger.info("PASS permission deny-first: safety never called")


async def prove_budget_and_allow():
    f1 = ToolFunnel(permissions=PermissionEngine(), budget_ref=Budget(True))
    out1 = await f1.admit(ctx=Ctx(), tool_name="t1", args={})
    assert not out1.allowed and out1.blocked_by == "budget"

    bus = LifecycleBus()
    audit = Audit()
    f2 = ToolFunnel(permissions=PermissionEngine(), lifecycle_bus=bus, budget_ref=Budget(False), audit_logger=audit)
    executed = []

    async def executor(args):
        executed.append(dict(args))
        return "all good"

    result, is_error = await f2.run_tool(Ctx(), "t1", {"a": 1}, executor)
    assert (result, is_error) == ("all good", False)
    assert executed == [{"a": 1}]
    assert audit.entries == []
    assert [e.event for e in bus.replay(session_id="proof-session")] == ["tool_before", "tool_after"]
    logger.info("PASS budget deny + clean allow path (tool_before/tool_after)")


async def prove_llm_funnel():
    bus = LifecycleBus()
    from agent_runtime.compactor import CompactionConfig, Compactor

    compactor = Compactor(
        CompactionConfig(context_window=200, warning_buffer=100, error_buffer=40, manual_buffer=150, keep_recent_rounds=1)
    )
    funnel = LLMFunnel(compactor=compactor, lifecycle_bus=bus)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "U" * 400},
        {"role": "assistant", "content": "A" * 400},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "final"},
    ]
    proceed, reason, out = await funnel.pre_llm(Ctx(), messages=msgs)
    assert proceed and not reason
    assert out is not msgs
    funnel.post_llm(Ctx(), node_label="n", model="m")
    assert funnel.calls == 1
    events = [e.event for e in bus.replay(session_id="proof-session", domain="agent")]
    for expected in ("llm_before", "compact_before", "compact_after", "llm_after"):
        assert expected in events, expected
    logger.info("PASS llm funnel: compaction events + metering")


async def main():
    await prove_hook_block()
    await prove_hook_rewrite()
    await prove_safety_block_and_approval()
    await prove_permission_deny_first()
    await prove_budget_and_allow()
    await prove_llm_funnel()
    logger.info("ALL FUNNEL CHAIN PROOFS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
