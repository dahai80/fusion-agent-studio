"""Safety approval mixin — extracted from AgentRuntime (audit 0826 P2-4).

Holds the safety-gateway approval + in-graph plan approval methods. Method
bodies are verbatim moves from runtime.py; AgentRuntime inherits this mixin.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator

from ._runtime_helpers import logger
from .confirmation_policy import policy_from_ctx, score_tool_risk
from .context import AgentContext, AgentEvent, AgentEventType


class _SafetyApprovalMixin:
    def approve_action(self, action_id: str) -> bool:
        if self.safety_gateway:
            ok = self.safety_gateway.approve_action(action_id)
            if ok and action_id in self._safety_futures:
                fut = self._safety_futures.pop(action_id, None)
                if fut and not fut.done():
                    fut.set_result(True)
            logger.info("Runtime approve_action: action_id=%s ok=%s", action_id, ok)
            return ok
        if action_id in self._safety_futures:
            fut = self._safety_futures.pop(action_id, None)
            if fut and not fut.done():
                fut.set_result(True)
            return True
        return False

    def reject_action(self, action_id: str) -> bool:
        if self.safety_gateway:
            ok = self.safety_gateway.reject_action(action_id)
            if ok and action_id in self._safety_futures:
                fut = self._safety_futures.pop(action_id, None)
                if fut and not fut.done():
                    fut.set_result(False)
            logger.info("Runtime reject_action: action_id=%s ok=%s", action_id, ok)
            return ok
        if action_id in self._safety_futures:
            fut = self._safety_futures.pop(action_id, None)
            if fut and not fut.done():
                fut.set_result(False)
            return True
        return False

    def approve_plan_in_graph(self, plan_id: str) -> bool:
        # C6: resolve the in-graph planner approval future for plan_id.
        fut = self._plan_futures.pop(plan_id, None)
        if fut and not fut.done():
            fut.set_result(True)
            logger.info("approve_plan_in_graph: plan_id=%s approved", plan_id)
            return True
        logger.warning("approve_plan_in_graph: no pending future plan_id=%s", plan_id)
        return False

    def reject_plan_in_graph(self, plan_id: str) -> bool:
        # C6: reject the in-graph planner approval future for plan_id.
        fut = self._plan_futures.pop(plan_id, None)
        if fut and not fut.done():
            fut.set_result(False)
            logger.info("reject_plan_in_graph: plan_id=%s rejected", plan_id)
            return True
        logger.warning("reject_plan_in_graph: no pending future plan_id=%s", plan_id)
        return False

    def _record_approval_slo(self, t0: float) -> None:
        # N11 #350: approval wait latency into telemetry SLO baselines.
        engine = getattr(self, "telemetry_engine", None)
        if engine is None:
            return
        try:
            engine.record_slo("approval", time.time() - t0)
        except Exception as e:
            logger.warning("approval SLO record failed: %s", e)

    def _confirmation_policy_meta(
        self,
        ctx: AgentContext,
        requires_approval: bool,
        tool_name: str = "",
        params: dict | None = None,
    ) -> dict | None:
        # #499: confirmation policy tier decision — {"tier", "risk_score",
        # "decision"}. None when no graph-level policy is active (legacy
        # behavior). Deterministic risk scorer, never LLM-judged.
        policy = policy_from_ctx(ctx)
        if policy is None:
            return None
        risk_score = score_tool_risk(tool_name, params)
        decision, tier, risk_score = policy.decide(requires_approval, risk_score, tool_name)
        logger.info(
            "confirmation policy tier=%s tool=%s risk=%.2f decision=%s",
            tier,
            tool_name or "-",
            risk_score,
            decision,
        )
        return {"tier": tier, "risk_score": risk_score, "decision": decision}

    def _policy_escalation_verdict(self, ctx: AgentContext, tool_name: str, params: dict | None = None):
        # #499: AlwaysConfirm / ConfirmRisk(at/above threshold) escalate
        # actions the funnel auto-passed into the human approval flow.
        # Returns a synthesized SafetyVerdict for _await_safety_approval,
        # or None when the policy lets the action run.
        meta = self._confirmation_policy_meta(ctx, False, tool_name, params)
        if meta is None or meta["decision"] != "confirm":
            return None
        from .safety import SafetyAction, SafetyVerdict

        return SafetyVerdict(
            action=SafetyAction.PREVIEW,
            reason=(
                f"confirmation policy tier '{meta['tier']}' requires approval "
                f"for {tool_name} (risk {meta['risk_score']:.2f})"
            ),
            requires_approval=True,
            metadata={"level": "L2", "source": "confirmation_policy"},
        )

    async def _await_safety_approval(
        self,
        ctx: AgentContext,
        safety_result,
        category: str,
        node_label: str,
        tool_name: str = "",
        params: dict | None = None,
    ) -> AsyncIterator[AgentEvent] | None:
        # #499: the graph-level confirmation policy decides the confirmation
        # step on top of the safety verdict. "auto" -> approve without asking
        # ("never" tier, or "risk" below threshold); "confirm" -> the ask flow
        # below (also escalates actions the safety flow would auto-pass).
        policy_meta = self._confirmation_policy_meta(
            ctx, safety_result.requires_approval, tool_name, params
        )
        if policy_meta is not None and policy_meta["decision"] == "auto":
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason or "auto-approved by confirmation policy",
                metadata={
                    "action": "approved",
                    "category": category,
                    "confirmation_policy": policy_meta,
                },
                node_id=node_label,
            )
            return
        escalated = bool(
            policy_meta is not None
            and policy_meta["decision"] == "confirm"
            and not safety_result.requires_approval
        )
        if not safety_result.requires_approval and not escalated:
            approved_meta: dict = {"action": "approved", "category": category}
            if policy_meta is not None:
                approved_meta["confirmation_policy"] = policy_meta
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason or "approved",
                metadata=approved_meta,
                node_id=node_label,
            )
            return
        if escalated:
            # Auto-passed verdicts carry a "nothing to ask" reason; the
            # pending card must say what the policy is asking about.
            safety_result.reason = (
                f"confirmation policy tier '{policy_meta['tier']}' requires "
                f"approval for {tool_name or category} "
                f"(risk {policy_meta['risk_score']:.2f})"
            )
            safety_result.requires_approval = True

        action_id = safety_result.metadata.get("action_id", "")
        if not action_id:
            action_id = str(__import__("uuid").uuid4())
            safety_result.metadata["action_id"] = action_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        # N11 #350: approval latency = request -> resolve/reject/timeout.
        slo_t0 = time.time()
        self._safety_futures[action_id] = future
        # 审计 A-1 Tier1: 登记归属 exec, 异常路径 reap 兜底.
        self._register_exec_future(ctx.session_id, action_id)

        if self.safety_gateway:
            with self.safety_gateway._lock:
                self.safety_gateway._pending_action_approvals.setdefault(
                    action_id,
                    {
                        "category": category,
                        "content": safety_result.reason,
                        "level": safety_result.metadata.get("level", "L2"),
                        "status": "pending",
                    },
                )
            self.safety_gateway._pending_approvals[action_id] = future

        pending_meta = {
            "action": "pending_approval",
            "category": category,
            "action_id": action_id,
            "level": safety_result.metadata.get("level", ""),
            "diff_preview": safety_result.diff_preview.to_dict()
            if safety_result.diff_preview
            else None,
        }
        if policy_meta is not None:
            pending_meta["confirmation_policy"] = policy_meta
        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=safety_result.reason,
            metadata=pending_meta,
            node_id=node_label,
        )
        # #479: APPROVAL_REQUEST — organic fire point at ask time, right
        # after the pending event surfaces and the future is registered.
        await self._fire_observability_hook(
            "APPROVAL_REQUEST",
            {
                "action_id": action_id,
                "category": category,
                "level": safety_result.metadata.get("level", ""),
                "node_id": node_label,
            },
        )

        try:
            approved = await asyncio.wait_for(future, timeout=self._safety_timeout)
            self._record_approval_slo(slo_t0)
        except asyncio.TimeoutError:
            self._safety_futures.pop(action_id, None)
            self._record_approval_slo(slo_t0)
            logger.warning("Safety approval timed out: action_id=%s", action_id)
            # 审计 P1-21/R-4: headless/CI 无人工审批必挂满 timeout. 原 timeout
            # 路径恒报 ERROR 终止 — 自动化执行 (cron/task) 卡死. 读 env disposition:
            # FUSION_SAFETY_AUTO_APPROVE=1 -> 超时视同批准继续;
            # FUSION_SAFETY_AUTO_REJECT=1 -> 超时视同拒绝 (报错终止, 原默认行为);
            # FUSION_SAFETY_FAIL_FAST=1 -> 立即失败不等 timeout (timeout 设 0).
            # 默认 auto_reject (向后兼容: 报错终止).
            disposition = os.environ.get("FUSION_SAFETY_TIMEOUT_DISPOSITION", "reject").strip().lower()
            yield AgentEvent(
                type=AgentEventType.SAFETY_TIMEOUT,
                content=f"Safety approval timed out for {category}",
                metadata={
                    "action_id": action_id,
                    "category": category,
                    "disposition": disposition,
                },
                node_id=node_label,
            )
            if disposition == "approve":
                logger.info(
                    "Safety timeout auto-approve (FUSION_SAFETY_TIMEOUT_DISPOSITION=approve): %s",
                    action_id,
                )
                approved = True
            else:
                ctx.error = f"SafetyGateway: approval timed out for {category}"
                await self._fire_observability_hook(
                    "APPROVAL_RESOLVE",
                    {"action_id": action_id, "category": category, "decision": "timeout"},
                )
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

        self._safety_futures.pop(action_id, None)
        # #479: APPROVAL_RESOLVE — organic fire point after the future
        # settles (approved / rejected; timeout handled above).
        await self._fire_observability_hook(
            "APPROVAL_RESOLVE",
            {
                "action_id": action_id,
                "category": category,
                "decision": "approved" if approved else "rejected",
            },
        )

        if not approved:
            ctx.error = f"SafetyGateway: {category} rejected — {safety_result.reason}"
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason,
                metadata={
                    "action": "rejected",
                    "category": category,
                    "action_id": action_id,
                },
                node_id=node_label,
            )
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=safety_result.reason or "approved",
            metadata={
                "action": "approved",
                "category": category,
                "action_id": action_id,
            },
            node_id=node_label,
        )
