import logging

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class PlannerDispatcher(SubDispatcher):
    def get_handlers(self) -> dict:
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
        }

    def _get_planner(self):
        if not hasattr(self, "_planner") or self._planner is None:
            from ..planner import PlannerEngine
            self._planner = PlannerEngine(gateway=self._daemon._gateway)
            logger.info("PlannerEngine created (gateway=%s)", "enabled" if self._daemon._gateway._default_client else "stub")
        return self._planner

    async def _handle_planner_create_plan(self, params: dict) -> dict:
        task = params.get("task", "")
        if not task:
            return {"status": "error", "message": "task parameter required"}
        planner = self._get_planner()
        plan = await planner.create_plan(
            task=task,
            context=params.get("context", ""),
            files=params.get("files"),
        )
        logger.info("planner.create_plan: plan_id=%s steps=%d risk=%s", plan.id, len(plan.steps), plan.overall_risk)
        return {"plan": plan.to_dict()}

    async def _handle_planner_get_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        plan = planner.get_plan(plan_id)
        if plan is None:
            return {"status": "error", "message": f"Plan not found: {plan_id}"}
        return {"plan": plan.to_dict()}

    async def _handle_planner_approve_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        ok = planner.approve_plan(plan_id)
        return {"approved": ok}

    async def _handle_planner_reject_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        reason = params.get("reason", "")
        planner = self._get_planner()
        ok = planner.reject_plan(plan_id, reason=reason)
        return {"rejected": ok}

    async def _handle_planner_execute_step(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        step_id = params.get("step_id", "")
        planner = self._get_planner()
        try:
            step = planner.execute_step(plan_id, step_id)
            return {"step": step.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_execute_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        try:
            plan = planner.execute_plan(plan_id)
            return {"plan": plan.to_dict()}
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    async def _handle_planner_list_plans(self, params: dict) -> dict:
        status = params.get("status", "")
        planner = self._get_planner()
        plans = planner.list_plans(status=status)
        return {"plans": [p.to_dict() for p in plans]}

    async def _handle_planner_cancel_plan(self, params: dict) -> dict:
        plan_id = params.get("plan_id", "")
        planner = self._get_planner()
        ok = planner.cancel_plan(plan_id)
        return {"cancelled": ok}

    async def _handle_verify_verify(self, params: dict) -> dict:
        from ..verifier import VerificationEngine
        task = params.get("task", "")
        output = params.get("output", "")
        criteria = params.get("criteria", "")
        context = params.get("context", "")
        max_attempts = params.get("max_attempts", 3)
        gateway = self._daemon._gateway
        engine = VerificationEngine(gateway=gateway, max_attempts=max_attempts)
        result = await engine.verify(task=task, output=output, criteria=criteria, context=context, max_attempts=max_attempts)
        return result.to_dict()

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
