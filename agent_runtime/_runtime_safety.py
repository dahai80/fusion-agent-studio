"""Safety approval mixin — extracted from AgentRuntime (audit 0826 P2-4).

Holds the safety-gateway approval + in-graph plan approval methods. Method
bodies are verbatim moves from runtime.py; AgentRuntime inherits this mixin.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

from ._runtime_helpers import logger
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

    async def _await_safety_approval(
        self, ctx: AgentContext, safety_result, category: str, node_label: str
    ) -> AsyncIterator[AgentEvent] | None:
        if not safety_result.requires_approval:
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason or "approved",
                metadata={"action": "approved", "category": category},
                node_id=node_label,
            )
            return

        action_id = safety_result.metadata.get("action_id", "")
        if not action_id:
            action_id = str(__import__("uuid").uuid4())
            safety_result.metadata["action_id"] = action_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
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

        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=safety_result.reason,
            metadata={
                "action": "pending_approval",
                "category": category,
                "action_id": action_id,
                "level": safety_result.metadata.get("level", ""),
                "diff_preview": safety_result.diff_preview.to_dict()
                if safety_result.diff_preview
                else None,
            },
            node_id=node_label,
        )

        try:
            approved = await asyncio.wait_for(future, timeout=self._safety_timeout)
        except asyncio.TimeoutError:
            self._safety_futures.pop(action_id, None)
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
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

        self._safety_futures.pop(action_id, None)

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
