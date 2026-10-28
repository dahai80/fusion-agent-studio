from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_runtime.funnels import (
    EventDomain,
    FunnelOutcome,
    LifecycleBus,
    LifecycleEvent,
    LLMFunnel,
    ToolFunnel,
)
from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.permissions import PermissionEngine, PermissionMode, PermissionRule

logger = logging.getLogger(__name__)


@dataclass
class _Ctx:
    session_id: str = "s1"
    plan_mode: bool = False


class _Verdict:
    def __init__(self, action, requires_approval=False, reason="stub-reason"):
        self.action = action
        self.requires_approval = requires_approval
        self.reason = reason


class _SafetyStub:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def evaluate_action(self, **kwargs):
        self.calls += 1
        return self.verdict


class _Budget:
    def __init__(self, exceeded):
        self.exceeded = exceeded

    def is_exceeded(self):
        return self.exceeded


class _Audit:
    def __init__(self):
        self.entries = []

    def log(self, **kwargs):
        self.entries.append(kwargs)


def _block_hook(reason="no"):
    eng = HookEngine(auto_register_decorators=False)
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "block", "reason": reason}))
    return eng


def _rewrite_hook(updated):
    eng = HookEngine(auto_register_decorators=False)
    eng.register(
        HookConfig(
            event="PRE_TOOL_USE",
            callback=lambda p: {"decision": "approve", "updated_input": updated},
        )
    )
    return eng


def test_lifecycle_seq_monotonic_across_sessions():
    bus = LifecycleBus()
    e1 = bus.emit(EventDomain.SESSION, "a", session_id="s1")
    e2 = bus.emit(EventDomain.AGENT, "b", session_id="s2")
    e3 = bus.emit("capability", "c", session_id="s1")
    assert e1.seq == 1 and e2.seq == 2 and e3.seq == 3
    assert e1.domain == "session" and e2.domain == "agent" and e3.domain == "capability"


def test_lifecycle_per_session_deques_and_session_count():
    bus = LifecycleBus()
    bus.emit("agent", "a1", session_id="s1")
    bus.emit("agent", "a2", session_id="s1")
    bus.emit("agent", "b1", session_id="s2")
    assert bus.session_count("s1") == 2
    assert bus.session_count("s2") == 1
    assert bus.session_count("nope") == 0
    s1_events = bus.replay(session_id="s1")
    assert [e.event for e in s1_events] == ["a1", "a2"]


def test_lifecycle_replay_filters():
    bus = LifecycleBus()
    bus.emit("agent", "a1", session_id="s1")
    bus.emit("capability", "c1", session_id="s1")
    bus.emit("agent", "a2", session_id="s2")
    assert [e.event for e in bus.replay(domain="agent")] == ["a1", "a2"]
    assert [e.event for e in bus.replay(session_id="s1", domain="capability")] == ["c1"]
    all_seq = [e.seq for e in bus.replay()]
    assert all_seq == sorted(all_seq)
    limited = bus.replay(limit=2)
    assert len(limited) == 2
    assert limited[-1].seq == max(all_seq)


def test_lifecycle_jsonl_append(tmp_path: Path):
    bus = LifecycleBus(jsonl_dir=str(tmp_path))
    bus.emit("agent", "evt_a", {"k": 1}, session_id="sess-j")
    bus.emit("agent", "evt_b", {}, session_id="")
    f1 = tmp_path / "sess-j.jsonl"
    f2 = tmp_path / "global.jsonl"
    assert f1.exists() and f2.exists()
    lines = f1.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "evt_a"
    assert rec["payload"] == {"k": 1}
    assert rec["session_id"] == "sess-j"
    assert rec["domain"] == "agent"


def test_lifecycle_subscriber_error_fail_open():
    bus = LifecycleBus()
    seen = []

    def bad(ev):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda ev: seen.append(ev.event))
    ev = bus.emit("agent", "evt_ok", session_id="s1")
    assert ev.seq == 1
    assert seen == ["evt_ok"]
    bus.unsubscribe(bad)
    bus.emit("agent", "evt_ok2")
    assert seen == ["evt_ok", "evt_ok2"]


def test_lifecycle_event_to_dict():
    ev = LifecycleEvent(seq=7, ts=123.0, domain=EventDomain.AGENT, event="x", payload={"p": 1}, session_id="s")
    d = ev.to_dict()
    assert d["domain"] == "agent"
    assert d["seq"] == 7 and d["event"] == "x" and d["payload"] == {"p": 1}


def test_funnel_outcome_to_dict():
    out = FunnelOutcome(
        allowed=False,
        blocked_by="hook",
        reason="r",
        needs_approval=True,
        decision_log=[{"stage": "hook", "verdict": "deny"}],
        args={"x": 1},
    )
    d = out.to_dict()
    assert d == {
        "allowed": False,
        "blocked_by": "hook",
        "reason": "r",
        "needs_approval": True,
        "decision_log": [{"stage": "hook", "verdict": "deny"}],
        # #499 confirmation-policy landing added the confirmation slot;
        # default None round-trips through to_dict like the other fields
        "confirmation": None,
    }


async def test_funnel_lifecycle_tool_before_and_deny_events():
    bus = LifecycleBus()
    funnel = ToolFunnel(hooks=_block_hook("denied"), permissions=PermissionEngine(), lifecycle_bus=bus)
    out = await funnel.admit(ctx=_Ctx(), tool_name="any_tool", args={"a": 1})
    assert out.allowed is False
    assert out.blocked_by == "hook"
    events = [(e.event, e.payload) for e in bus.replay(session_id="s1", domain="capability")]
    names = [n for n, _ in events]
    assert names[0] == "tool_before"
    assert "tool_deny" in names
    deny = dict(events)["tool_deny"]
    assert deny["gate"] == "hook" and deny["tool"] == "any_tool"


async def test_funnel_hook_block_via_run_tool_skips_executor():
    bus = LifecycleBus()
    funnel = ToolFunnel(hooks=_block_hook("denied"), lifecycle_bus=bus)
    executed = []

    async def executor(args):
        executed.append(args)
        return "ok"

    result, is_error = await funnel.run_tool(_Ctx(), "any_tool", {"a": 1}, executor)
    assert is_error is True
    assert result.startswith("Blocked by hook")
    assert executed == []
    assert funnel._executions == 0


async def test_funnel_hook_rewrite_updated_input():
    eng = _rewrite_hook('{"x": 1}')
    funnel = ToolFunnel(hooks=eng, permissions=PermissionEngine())
    out = await funnel.admit(ctx=_Ctx(), tool_name="any_tool", args={"orig": True})
    assert out.allowed is True
    assert out.args == {"x": 1}
    rewrite = [s for s in out.decision_log if s.get("stage") == "hook"]
    assert rewrite and rewrite[0]["verdict"] == "rewrite"


async def test_funnel_safety_block_and_approval_passthrough():
    from agent_runtime.safety import SafetyAction

    blocking = _SafetyStub(_Verdict(SafetyAction.BLOCK, requires_approval=False, reason="bad content"))
    funnel = ToolFunnel(permissions=PermissionEngine(), safety_gateway=blocking)
    out = await funnel.admit(ctx=_Ctx(), tool_name="t1", args={})
    assert out.allowed is False and out.blocked_by == "safety"
    assert blocking.calls == 1

    approving = _SafetyStub(_Verdict(SafetyAction.BLOCK, requires_approval=True, reason="need human"))
    funnel2 = ToolFunnel(permissions=PermissionEngine(), safety_gateway=approving)
    out2 = await funnel2.admit(ctx=_Ctx(), tool_name="t1", args={})
    assert out2.allowed is False
    assert out2.needs_approval is True and out2.blocked_by == "safety"
    assert out2.safety_result is approving.verdict


async def test_funnel_permission_deny_first_before_safety():
    from agent_runtime.safety import SafetyAction

    engine = PermissionEngine(
        rules=[PermissionRule(action="deny", tool="dangerous_tool", layer="global", description="no")]
    )
    safety = _SafetyStub(_Verdict(SafetyAction.BLOCK))
    bus = LifecycleBus()
    funnel = ToolFunnel(permissions=engine, safety_gateway=safety, lifecycle_bus=bus)
    out = await funnel.admit(ctx=_Ctx(), tool_name="dangerous_tool", args={})
    assert out.allowed is False and out.blocked_by == "permission"
    assert safety.calls == 0
    assert out.decision_log[-1]["stage"] == "permission"
    assert out.decision_log[-1]["verdict"] == "deny"


async def test_funnel_allow_path_chain_order():
    bus = LifecycleBus()
    audit = _Audit()
    funnel = ToolFunnel(
        permissions=PermissionEngine(),
        lifecycle_bus=bus,
        budget_ref=_Budget(False),
        audit_logger=audit,
    )
    out = await funnel.admit(ctx=_Ctx(), tool_name="t1", args={"a": 1})
    assert out.allowed is True and out.blocked_by == ""
    stages = [s["stage"] for s in out.decision_log]
    assert stages == ["permission", "admit", "permission"]
    assert out.decision_log[-1]["rule_source"] == "legacy_passthrough"
    assert audit.entries == []
    assert [e.event for e in bus.replay(session_id="s1")] == ["tool_before"]


async def test_funnel_budget_exceeded_denies():
    funnel = ToolFunnel(permissions=PermissionEngine(), budget_ref=_Budget(True))
    out = await funnel.admit(ctx=_Ctx(), tool_name="t1", args={})
    assert out.allowed is False and out.blocked_by == "budget"
    assert out.reason == "token budget exceeded"


async def test_funnel_run_tool_allowed_and_error_paths():
    funnel = ToolFunnel(permissions=PermissionEngine())
    seen = []

    async def executor(args):
        seen.append(dict(args))
        return "all good"

    result, is_error = await funnel.run_tool(_Ctx(), "t1", {"a": 1}, executor)
    assert (result, is_error) == ("all good", False)
    assert seen == [{"a": 1}]
    assert funnel._executions == 1

    async def bad_executor(args):
        raise RuntimeError("kaput")

    result2, is_error2 = await funnel.run_tool(_Ctx(), "t1", {}, bad_executor)
    assert is_error2 is True
    assert result2.startswith("Error:")
    assert funnel._executions == 2


async def test_funnel_run_tool_needs_approval_no_handler():
    engine = PermissionEngine(rules=[PermissionRule(action="ask", tool="guarded_tool", layer="global")])
    funnel = ToolFunnel(permissions=engine)
    executed = []

    async def executor(args):
        executed.append(args)
        return "ok"

    result, is_error = await funnel.run_tool(_Ctx(), "guarded_tool", {}, executor)
    assert is_error is True
    assert "requires approval" in result
    assert executed == []


async def test_funnel_readonly_provider_heuristic_allow():
    engine = PermissionEngine(mode=PermissionMode.APPROVED)
    funnel = ToolFunnel(permissions=engine, readonly_tools=lambda: {"ro_tool"})
    out = await funnel.admit(ctx=_Ctx(), tool_name="ro_tool", args={})
    assert out.allowed is True
    assert out.decision_log[-1]["rule_source"] == "heuristic_readonly"

    out2 = await funnel.admit(ctx=_Ctx(), tool_name="write_tool", args={})
    assert out2.allowed is True
    assert out2.decision_log[-1]["rule_source"] == "mode:approved"


async def test_funnel_armed_default_mode_asks():
    engine = PermissionEngine(
        rules=[PermissionRule(action="allow", tool="other_tool", layer="global")]
    )
    assert engine.is_armed() is True
    funnel = ToolFunnel(permissions=engine)
    out = await funnel.admit(ctx=_Ctx(), tool_name="unlisted_tool", args={})
    assert out.allowed is False
    assert out.needs_approval is True and out.blocked_by == "permission"
    assert out.decision_log[-1]["verdict"] == "ask"


def test_permission_is_armed_env(monkeypatch):
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    assert PermissionEngine().is_armed() is False
    monkeypatch.setenv("FUSION_PERMISSION_MODE", "default")
    assert PermissionEngine().is_armed() is True


def test_permission_engine_unique_names_unarmed_passthrough(monkeypatch):
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    engine = PermissionEngine()
    d = engine.evaluate("any_tool", {})
    assert d.decision == "allow" and d.rule_source == "legacy_passthrough"


async def test_llm_funnel_hook_block():
    eng = HookEngine(auto_register_decorators=False)
    eng.register(HookConfig(event="LLM_BEFORE", callback=lambda p: {"decision": "block", "reason": "no-llm"}))
    funnel = LLMFunnel(hooks=eng)
    msgs = [{"role": "user", "content": "hi"}]
    proceed, reason, out_msgs = await funnel.pre_llm(_Ctx(), messages=msgs)
    assert proceed is False
    assert "blocked by hook" in reason and "no-llm" in reason
    assert out_msgs == msgs


def _small_compactor():
    from agent_runtime.compactor import CompactionConfig, Compactor

    return Compactor(
        CompactionConfig(
            context_window=200,
            warning_buffer=100,
            error_buffer=50,
            manual_buffer=10,
            keep_recent_rounds=1,
        )
    )


def _big_messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "U" * 400},
        {"role": "assistant", "content": "A" * 400},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "final"},
    ]


async def test_llm_funnel_compaction_trigger_and_events():
    bus = LifecycleBus()
    funnel = LLMFunnel(compactor=_small_compactor(), lifecycle_bus=bus)
    msgs = _big_messages()
    proceed, reason, out = await funnel.pre_llm(_Ctx(), messages=msgs)
    assert proceed is True and reason == ""
    assert out is not msgs
    assert any(
        m.get("role") == "system" and "[Compacted prior context]" in m.get("content", "") for m in out
    )
    assert out[-1]["content"] == "final"
    agent_events = [e.event for e in bus.replay(session_id="s1", domain="agent")]
    assert "compact_before" in agent_events
    assert "compact_after" in agent_events
    assert "llm_before" in agent_events


async def test_llm_funnel_no_compaction_under_threshold():
    bus = LifecycleBus()
    funnel = LLMFunnel(compactor=_small_compactor(), lifecycle_bus=bus)
    msgs = [{"role": "user", "content": "tiny"}]
    proceed, reason, out = await funnel.pre_llm(_Ctx(), messages=msgs)
    assert (proceed, reason) == (True, "")
    assert out == msgs
    events = [e.event for e in bus.replay(session_id="s1", domain="agent")]
    assert "compact_before" not in events
    assert "llm_before" in events


async def test_llm_funnel_passthrough_no_hooks_no_compactor():
    funnel = LLMFunnel()
    msgs = [{"role": "user", "content": "hi"}]
    proceed, reason, out = await funnel.pre_llm(_Ctx(), messages=msgs)
    assert (proceed, reason, out) == (True, "", msgs)
    assert funnel.calls == 0


def test_llm_funnel_post_llm_meters_and_emits():
    bus = LifecycleBus()
    funnel = LLMFunnel(lifecycle_bus=bus)
    ctx = _Ctx()
    funnel.post_llm(ctx, node_label="n1", model="m1", usage={"tokens": 5}, content="c")
    funnel.post_llm(ctx, node_label="n1", model="m1")
    assert funnel.calls == 2
    after = [e for e in bus.replay(session_id="s1", domain="agent") if e.event == "llm_after"]
    assert len(after) == 2
    assert after[0].payload["usage"] == {"tokens": 5}


async def test_funnel_contextless_session_id_is_empty():
    bus = LifecycleBus()
    funnel = ToolFunnel(permissions=PermissionEngine(), lifecycle_bus=bus)
    await funnel.admit(ctx=None, tool_name="t1", args={})
    assert bus.session_count("") == 1


@pytest.mark.parametrize("domain", ["session", "agent", "capability"])
def test_event_domain_values(domain):
    assert EventDomain(domain).value == domain
