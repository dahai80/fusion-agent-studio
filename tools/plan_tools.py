"""Plan-mode tools — ExitPlanMode gating primitive (C6).

ExitPlanMode is the plan->execution transition: the agent calls it once the
plan is ready for approval. Runtime detects the sentinel result and flips
plan_mode off, ending the read-only explore phase.
"""

from __future__ import annotations

import logging

from .base import BaseTool

logger = logging.getLogger(__name__)

EXIT_PLAN_MODE_SENTINEL = "__EXIT_PLAN_MODE__"


class ExitPlanModeTool(BaseTool):
    name = "exit_plan_mode"
    description = (
        "Transition from the read-only planning phase to execution. "
        "Call this once you have presented a complete plan and are ready "
        "for approval. After approval, write tools become available. "
        "Pass the plan content as the summary."
    )
    parameters = {
        "plan": {
            "type": "string",
            "description": "The complete plan to present for approval.",
        },
        "ready": {
            "type": "boolean",
            "description": "Whether the plan is ready for approval (default true).",
        },
    }

    async def execute(self, plan: str = "", ready: bool = True, **kwargs) -> str:
        logger.info(
            "ExitPlanMode called: ready=%s plan_len=%d", ready, len(plan)
        )
        if not ready:
            return "Plan not ready. Continue exploring in read-only mode."
        if not plan:
            return "Error: plan parameter required for exit_plan_mode"
        return f"{EXIT_PLAN_MODE_SENTINEL}{plan}"
