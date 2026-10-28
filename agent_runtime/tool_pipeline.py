# Issue #5 AC4 (fusion-harness): segmented ToolPipeline is the single
# admission path for every tool call. The funnel choke point
# (PersonaScopedFunnel) runs the kernel ToolPipeline on every admit: the
# policy stage delegates back into the funnel chain (hooks -> permission ->
# safety -> budget), guards run after, approval is stage 3.
#
# Interactive approval is TWO pipeline admits, keeping the user in the loop
# without a side door:
#   phase 1: admit(interactive=True) — an ask surfaces as
#     needs_approval + the verdict (safety_result / escalation) attached;
#     the pipeline approval stage fails closed (no responder wired).
#   the call site drives the legacy interactive ask (async-generator
#     _await_safety_approval — events stream to the consumer live, the
#     user's approve/reject resolves the future).
#   phase 2: on approve, admit(interactive=True, approved=True) re-runs all
#     stages with a pre-resolved approver; execution happens only after the
#     pipeline allows. A decline never re-admits — nothing executes.
# A call site that approved and executed WITHOUT the phase-2 admit would be
# a side door around stage 3; every execution path passes the pipeline.
#
# Non-interactive sites (parallel fan-out, register/unregister) pass no
# approver; asks fail closed and map back to the legacy needs_approval
# contract, so those call sites keep their existing degrade behavior.
from __future__ import annotations

import logging

from fusion_harness import PermissionDecision, ToolPipeline
from fusion_harness.funnels import FunnelOutcome

from .persona_gate import PersonaScopedFunnel

logger = logging.getLogger(__name__)


class _PreApprovedApprover:
    # Phase-2 responder: the user already approved through the interactive
    # flow; stage 3 is answered with the recorded decision. Gates (stages
    # 1-2) still re-run — a gate that flipped to deny since phase 1 wins.
    def __init__(self, reason: str = "user approved via interactive flow"):
        self.reason = reason

    def request(self, tool_name: str, args: dict | None = None) -> bool:
        logger.info("pipeline approval: pre-approved by user tool=%s", tool_name)
        return True


class _FunnelPolicyAdapter:
    # Pipeline stage-1 policy: delegates to the funnel admission chain via
    # the PersonaScopedFunnel base admit (direct class call — calling
    # self.funnel.admit would recurse through the pipeline subclass).
    # Per-call instance: outcome/escalation are call-scoped state, safe
    # under concurrent execs sharing one funnel.

    def __init__(self, funnel, runtime, node_label: str, interactive: bool):
        self.funnel = funnel
        self.runtime = runtime
        self.node_label = node_label
        self.interactive = interactive
        self.outcome: FunnelOutcome | None = None
        self.escalation = None

    async def evaluate(self, tool_name: str, args: dict | None = None, ctx=None, **_):
        outcome = await PersonaScopedFunnel.admit(
            self.funnel,
            ctx=ctx,
            tool_name=tool_name,
            args=args,
            node_label=self.node_label,
        )
        self.outcome = outcome
        if not outcome.allowed and not outcome.needs_approval:
            return PermissionDecision(decision="deny", reason=outcome.reason)
        if outcome.needs_approval:
            return PermissionDecision(decision="ask", reason=outcome.reason)
        # #499 escalation runs inside the pipeline on interactive sites only
        # (main tool-call loop + tool node); parallel/register/unregister
        # keep their call-site checks, preserving legacy behavior. The
        # verdict rides the outcome so the call site can drive the ask.
        if self.interactive:
            try:
                self.escalation = self.runtime._policy_escalation_verdict(ctx, tool_name, args)
            except Exception as e:
                logger.warning("pipeline escalation check failed tool=%s err=%s", tool_name, e)
                self.escalation = None
            if self.escalation is not None:
                outcome.escalation = self.escalation
                return PermissionDecision(decision="ask", reason=self.escalation.reason)
        return PermissionDecision(decision="allow")


class PipelineScopedFunnel(PersonaScopedFunnel):
    """Funnel whose admit() only ever runs the kernel ToolPipeline (#5 AC4)."""

    def __init__(self, *, runtime, **kwargs):
        super().__init__(**kwargs)
        self._runtime = runtime

    async def admit(
        self, ctx=None, tool_name: str = "", args: dict | None = None, **kwargs
    ) -> FunnelOutcome:
        interactive = bool(kwargs.pop("interactive", False))
        approved = bool(kwargs.pop("approved", False))
        node_label = str(kwargs.get("node_label", ""))
        # Phase 2 (approved=True) suppresses escalation re-asks — the user
        # already answered; gates (hooks/permission/safety/budget) still run.
        adapter = _FunnelPolicyAdapter(
            self, self._runtime, node_label, interactive and not approved
        )
        # Phase 1 (approved=False): no responder — an ask fails closed and
        # surfaces as needs_approval for the interactive flow. Phase 2
        # (approved=True): the recorded user decision answers stage 3.
        approver = _PreApprovedApprover() if approved else None
        pipeline = ToolPipeline(permission_engine=adapter)
        out = await pipeline.run(
            ctx=ctx,
            tool_name=tool_name,
            args=args,
            node_label=node_label,
            approver=approver,
            admission_only=True,
        )
        return self._to_outcome(adapter, out, tool_name)

    def _to_outcome(
        self,
        adapter: _FunnelPolicyAdapter,
        out,
        tool_name: str = "",
    ) -> FunnelOutcome:
        fo = adapter.outcome
        if out.allowed:
            return fo if fo is not None else FunnelOutcome(allowed=True, args={})
        if out.blocked_by == "policy":
            # Funnel-chain deny (hook/permission/safety/budget/persona) —
            # return the funnel outcome untouched so every legacy deny
            # branch (TOOL_DENY hook, gate-specific messages) is preserved.
            return (
                fo
                if fo is not None
                else FunnelOutcome(allowed=False, blocked_by="permission", reason=out.reason)
            )
        if out.blocked_by == "approval":
            # Unresolved ask (phase 1, or non-interactive site): preserve the
            # legacy needs_approval contract — the interactive call site
            # drives the ask and re-admits on approve; parallel degrades to
            # sequential-retry; register/unregister block; a permission ask
            # with no backend renders the no-backend error.
            result = (
                fo
                if fo is not None
                else FunnelOutcome(allowed=False, blocked_by="permission", reason=out.reason)
            )
            result.needs_approval = True
            return result
        if out.blocked_by.startswith("guard:"):
            logger.warning("pipeline guard denied tool=%s guard=%s", tool_name, out.blocked_by)
            return FunnelOutcome(
                allowed=False,
                blocked_by=out.blocked_by,
                reason=out.reason,
                args=fo.args if fo is not None else {},
            )
        return (
            fo
            if fo is not None
            else FunnelOutcome(
                allowed=False, blocked_by=out.blocked_by or "pipeline", reason=out.reason
            )
        )
