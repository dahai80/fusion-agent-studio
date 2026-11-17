"""Sub-dispatcher: PlannerDispatcher."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class PlannerDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "planner.create_plan": self._handle_planner_create_plan,
            "planner.get_plan": self._handle_planner_get_plan,
            "planner.approve_plan": self._handle_planner_approve_plan,
            "planner.reject_plan": self._handle_planner_reject_plan,
            "planner.execute_step": self._handle_planner_execute_step,
            "planner.execute_plan": self._handle_planner_execute_plan,
            "planner.list_plans": self._handle_planner_list_plans,
            "planner.cancel_plan": self._handle_planner_cancel_plan,
            "verify.verify": self._handle_verify_verify,
            "verify.adversarial_verify": self._handle_verify_adversarial_verify,
            "review.triage": self._handle_review_triage,
        }

    async def _handle_review_triage(self, params: dict) -> dict:
        # #730: guided triage — one LLM call sorts the pending artifact set
        # into ordered chapters; deterministic fallback when LLM is absent
        # or fails. Pure read surface: no plan/run state is mutated.
        items = params.get("items")
        if not isinstance(items, list) or not items:
            return {"status": "error", "message": "items must be a non-empty list"}
        from agent_runtime.review_triage import ReviewTriageEngine

        engine = ReviewTriageEngine(gateway=self._daemon._gateway)
        try:
            manifest = await engine.triage(items)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        logger.info(
            "review.triage: items=%d sorted=%s chapters=%d",
            manifest["item_count"],
            manifest["sorted"],
            len(manifest["chapters"]),
        )
        return {"manifest": manifest}

    async def _handle_planner_create_plan(self, params: dict) -> dict:
        task = params.get("task", "")
        if not task:
            return {"status": "error", "message": "task parameter required"}
        planner = self._daemon._get_planner()
        plan = await planner.create_plan(
            task=task,
            context=params.get("context", ""),
            files=params.get("files"),
        )
        logger.info(
            "planner.create_plan: plan_id=%s steps=%d risk=%s",
            plan.id,
            len(plan.steps),
            plan.overall_risk,
        )
        return {"plan": plan.to_dict()}

    async def _handle_planner_get_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._daemon._get_planner()
        plan = planner.get_plan(plan_id)
        if plan is None:
            return {"status": "error", "message": f"Plan not found: {plan_id}"}
        return {"plan": plan.to_dict()}

    async def _handle_planner_approve_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._daemon._get_planner()
        # #731: comments-aware web review round-trip. review (optional) is
        # {comments: [{anchor?, comment, verdict?}], verdict, source} — the
        # web review module's structured feedback shape. Non-approve verdicts
        # persist the review and leave the plan pending so the revising agent
        # reads the anchored comments via planner.get_plan on its next turn.
        review = params.get("review")
        if review is not None:
            if not isinstance(review, dict):
                return {"status": "error", "message": "review must be an object"}
            comments = review.get("comments", [])
            if not isinstance(comments, list) or not all(
                isinstance(c, dict) and isinstance(c.get("comment", ""), str)
                for c in comments
            ):
                return {
                    "status": "error",
                    "message": "review.comments must be a list of objects with a comment field",
                }
            verdict = str(review.get("verdict", "approve"))
            if verdict not in ("approve", "request_changes", "question"):
                return {
                    "status": "error",
                    "message": "review.verdict must be approve|request_changes|question",
                }
            review = dict(review, verdict=verdict)
        if review is not None and review["verdict"] != "approve":
            attached = planner.attach_review(plan_id, review)
            logger.info(
                "planner.approve_plan: plan_id=%s verdict=%s attached=%s",
                plan_id,
                review["verdict"],
                attached,
            )
            return {
                "approved": False,
                "review_attached": attached,
                "status": "changes_requested" if attached else "error",
            }
        ok = planner.approve_plan(plan_id)
        if ok and review is not None:
            planner.attach_review(plan_id, review)
        # C6: also resolve any in-graph approval future (planner node blocking).
        rt = self._daemon._get_runtime()
        if hasattr(rt, "approve_plan_in_graph"):
            rt.approve_plan_in_graph(plan_id)
        logger.info("planner.approve_plan: plan_id=%s ok=%s", plan_id, ok)
        return {"approved": ok}

    async def _handle_planner_reject_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        reason = params.get("reason", "")
        planner = self._daemon._get_planner()
        ok = planner.reject_plan(plan_id, reason=reason)
        # C6: also resolve any in-graph approval future (planner node blocking).
        rt = self._daemon._get_runtime()
        if hasattr(rt, "reject_plan_in_graph"):
            rt.reject_plan_in_graph(plan_id)
        logger.info("planner.reject_plan: plan_id=%s ok=%s", plan_id, ok)
        return {"rejected": ok}

    async def _handle_planner_execute_step(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        step_id = params.get("step_id", "")
        planner = self._daemon._get_planner()
        try:
            step = planner.execute_step(plan_id, step_id)
            return {"step": step.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_execute_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._daemon._get_planner()
        try:
            plan = planner.execute_plan(plan_id)
            return {"plan": plan.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_list_plans(self, params: dict) -> dict:
        status = params.get("status", "")
        planner = self._daemon._get_planner()
        plans = planner.list_plans(status=status)
        return {"plans": [p.to_dict() for p in plans]}

    async def _handle_planner_cancel_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._daemon._get_planner()
        ok = planner.cancel_plan(plan_id)
        return {"cancelled": ok}

    # ── Verify handlers ──

    async def _handle_verify_verify(self, params: dict) -> dict:
        from ..verifier import VerificationEngine

        task = params.get("task", "")
        output = params.get("output", "")
        criteria = params.get("criteria", "")
        context = params.get("context", "")
        max_attempts = params.get("max_attempts", 3)
        gateway = self._daemon._gateway
        engine = VerificationEngine(gateway=gateway, max_attempts=max_attempts)
        result = await engine.verify(
            task=task,
            output=output,
            criteria=criteria,
            context=context,
            max_attempts=max_attempts,
        )
        return result.to_dict()

    # ── RAG handlers ──

    async def _handle_verify_adversarial_verify(self, params: dict) -> dict:
        from ..verifier import VerificationEngine

        gateway = self._daemon._gateway
        engine = VerificationEngine(gateway=gateway)
        claim = params.get("claim", "")
        context = params.get("context", "")
        voter_count = params.get("voter_count", 3)
        threshold = params.get("threshold", 0.6)
        result = await engine.adversarial_verify(claim, context, voter_count, threshold)
        return result
