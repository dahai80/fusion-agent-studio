import logging

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class SafetyDispatcher(SubDispatcher):
    def get_handlers(self) -> dict:
        return {
            "safety.check": self._handle_safety_check,
            "safety.evaluate_action": self._handle_safety_evaluate_action,
            "safety.approve_action": self._handle_safety_approve_action,
            "safety.reject_action": self._handle_safety_reject_action,
            "safety.get_pending_actions": self._handle_safety_get_pending_actions,
            "safety.add_policy": self._handle_safety_add_policy,
            "safety.approve": self._handle_safety_approve,
            "safety.reject": self._handle_safety_reject,
            "safety.classify_action": self._handle_safety_classify_action,
            "safety.set_auto_mode": self._handle_safety_set_auto_mode,
            "safety.set_network_policy": self._handle_safety_set_network_policy,
            "safety.get_network_policy": self._handle_safety_get_network_policy,
        }

    def _get_safety(self):
        if self._daemon._safety is None:
            from ..safety import SafetyGateway
            self._daemon._safety = SafetyGateway()
            logger.info("SafetyGuard created (L1)")
        return self._daemon._safety

    def _get_gateway(self):
        gateway = self._daemon._safety
        if gateway is not None:
            return gateway
        runtime = self._daemon._get_runtime()
        if hasattr(runtime, "_safety") and runtime._safety is not None:
            return runtime._safety
        return self._get_safety()

    async def _handle_safety_check(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"status": "error", "message": "content parameter required"}
        guard = self._get_safety()
        verdict = guard.check(content, context=params.get("context", ""))
        return {"verdict": verdict.to_dict()}

    async def _handle_safety_evaluate_action(self, params: dict) -> dict:
        category = params.get("category", "")
        if not category:
            return {"status": "error", "message": "category parameter required"}
        guard = self._get_safety()
        verdict = guard.evaluate_action(
            category=category,
            content=params.get("content", ""),
            context=params.get("context", ""),
        )
        return {"verdict": verdict.to_dict()}

    async def _handle_safety_approve_action(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if not action_id:
            return {"status": "error", "message": "action_id parameter required"}
        guard = self._get_safety()
        ok = guard.approve_action(action_id)
        return {"approved": ok}

    async def _handle_safety_reject_action(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if not action_id:
            return {"status": "error", "message": "action_id parameter required"}
        guard = self._get_safety()
        ok = guard.reject_action(action_id)
        return {"rejected": ok}

    async def _handle_safety_get_pending_actions(self, params: dict) -> dict:
        guard = self._get_safety()
        actions = guard.get_pending_actions()
        return {"actions": actions}

    async def _handle_safety_add_policy(self, params: dict) -> dict:
        from ..safety import SafetyLevel, SafetyPolicy
        category = params.get("category", "")
        if not category:
            return {"status": "error", "message": "category parameter required"}
        level_str = params.get("default_level", "L2")
        try:
            level = SafetyLevel(level_str)
        except ValueError:
            level = SafetyLevel.L2
        policy = SafetyPolicy(
            category=category,
            description=params.get("description", ""),
            default_level=level,
            requires_diff=params.get("requires_diff", False),
        )
        guard = self._get_safety()
        guard.add_policy(policy)
        logger.info("safety.add_policy: category=%s level=%s", category, level_str)
        return {"added": True, "category": category}

    async def _handle_safety_approve(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if self._daemon._runtime and hasattr(self._daemon._runtime, "approve_action"):
            ok = self._daemon._runtime.approve_action(action_id)
            return {"status": "ok" if ok else "not_found", "action_id": action_id}
        return {"status": "error", "message": "No runtime available"}

    async def _handle_safety_reject(self, params: dict) -> dict:
        action_id = params.get("action_id", "")
        if self._daemon._runtime and hasattr(self._daemon._runtime, "reject_action"):
            ok = self._daemon._runtime.reject_action(action_id)
            return {"status": "ok" if ok else "not_found", "action_id": action_id}
        return {"status": "error", "message": "No runtime available"}

    async def _handle_safety_classify_action(self, params: dict) -> dict:
        gateway = self._get_gateway()
        action = params.get("action", "")
        context = params.get("context", "")
        result = gateway.classify_action(action, context)
        return result

    async def _handle_safety_set_auto_mode(self, params: dict) -> dict:
        gateway = self._get_gateway()
        enabled = params.get("enabled", True)
        threshold = params.get("threshold", 0.2)
        gateway.set_auto_mode(enabled, threshold)
        return {"auto_mode": enabled, "threshold": threshold}

    async def _handle_safety_set_network_policy(self, params: dict) -> dict:
        gateway = self._get_gateway()
        gateway.set_network_policy(params)
        return {"set": True}

    async def _handle_safety_get_network_policy(self, params: dict) -> dict:
        gateway = self._get_gateway()
        return gateway.get_network_policy()
