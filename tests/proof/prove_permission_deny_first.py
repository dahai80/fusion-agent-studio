from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.funnels import ToolFunnel
from agent_runtime.permissions import (
    PermissionEngine,
    PermissionRule,
)
from agent_runtime.safety import SafetyAction

logger = logging.getLogger("prove_permission_deny_first")


class Verdict:
    def __init__(self, action, approval=False, reason="proof"):
        self.action = action
        self.requires_approval = approval
        self.reason = reason


class SafetyStub:
    def __init__(self):
        self.calls = 0

    def evaluate_action(self, **kwargs):
        self.calls += 1
        return Verdict(SafetyAction.BLOCK, approval=False)


class Ctx:
    def __init__(self, plan_mode=False):
        self.session_id = "perm-proof"
        self.plan_mode = plan_mode


def prove_layer_scan_order():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool="t1", layer="global"), layer="global")
    eng.add_rule(PermissionRule(action="deny", tool="t1", layer="node"), layer="node")
    d = eng.evaluate("t1", {})
    assert d.decision == "deny" and d.rule_source == "rule:node:t1", d

    eng2 = PermissionEngine(mode="approved")
    eng2.add_rule(PermissionRule(action="deny", tool="t2", layer="global"), layer="global")
    eng2.add_rule(PermissionRule(action="allow", tool="t2", layer="graph"), layer="graph")
    d2 = eng2.evaluate("t2", {})
    assert d2.decision == "deny" and d2.rule_source == "rule:global:t2", d2
    logger.info("PASS layer scan order node > graph > global")


def prove_deny_beats_ask_allow():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool="t1"), layer="graph")
    eng.add_rule(PermissionRule(action="deny", tool="t1"), layer="graph")
    assert eng.evaluate("t1", {}).decision == "deny"
    eng2 = PermissionEngine(mode="approved")
    eng2.add_rule(PermissionRule(action="allow", tool="t2"), layer="graph")
    eng2.add_rule(PermissionRule(action="ask", tool="t2"), layer="graph")
    d = eng2.evaluate("t2", {})
    assert d.decision == "ask" and d.rule_source == "rule:ask:graph", d
    logger.info("PASS deny beats ask beats allow")


def prove_content_scan():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool=".*"), layer="global")
    d = eng.evaluate("t1", {"cmd": "sudo rm -rf /data"})
    assert d.decision == "deny" and d.rule_source == "content_scan", d
    logger.info("PASS dangerous content scan deny")


def prove_plan_gate():
    eng = PermissionEngine(mode="approved")
    ctx = Ctx(plan_mode=True)
    d = eng.evaluate("write_tool", {}, ctx=ctx)
    assert d.decision == "deny" and d.rule_source == "plan_mode", d
    d2 = eng.evaluate("write_tool", {}, ctx=ctx, readonly_hint=True)
    assert d2.decision == "allow" and d2.rule_source == "heuristic_readonly", d2
    logger.info("PASS plan mode gate + readonly passthrough")


def prove_mode_matrix():
    eng = PermissionEngine(rules=[PermissionRule(action="deny", tool="t1")], mode="bypass")
    d = eng.evaluate("t1", {})
    assert d.decision == "allow" and d.rule_source == "mode:bypass", d

    eng2 = PermissionEngine(mode="approved")
    d2 = eng2.evaluate("anything", {})
    assert d2.decision == "allow" and d2.rule_source == "mode:approved", d2

    eng3 = PermissionEngine(rules=[PermissionRule(action="allow", tool="other")])
    d3 = eng3.evaluate("unlisted", {})
    assert d3.decision == "ask" and d3.rule_source == "mode:default", d3
    logger.info("PASS mode matrix bypass/approved/armed-default")


async def prove_funnel_deny_first_integration():
    engine = PermissionEngine(
        rules=[PermissionRule(action="deny", tool="dangerous_tool", layer="global")], mode="approved"
    )
    safety = SafetyStub()
    funnel = ToolFunnel(permissions=engine, safety_gateway=safety)
    out = await funnel.admit(ctx=Ctx(), tool_name="dangerous_tool", args={})
    assert not out.allowed and out.blocked_by == "permission", out.to_dict()
    assert safety.calls == 0
    assert out.decision_log[-1]["stage"] == "permission"
    logger.info("PASS funnel deny-first: safety gateway never invoked")


async def main():
    prove_layer_scan_order()
    prove_deny_beats_ask_allow()
    prove_content_scan()
    prove_plan_gate()
    prove_mode_matrix()
    await prove_funnel_deny_first_integration()
    logger.info("ALL PERMISSION DENY-FIRST PROOFS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
