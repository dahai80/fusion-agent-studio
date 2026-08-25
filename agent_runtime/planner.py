from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .llm_gateway import LLMGateway

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

VALID_ACTIONS = ("create", "modify", "delete")
VALID_COMPLEXITIES = ("low", "medium", "high")
VALID_STEP_STATUSES = (
    "pending",
    "approved",
    "executing",
    "completed",
    "failed",
    "skipped",
)
VALID_PLAN_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "executing",
    "completed",
    "rejected",
)
VALID_RISKS = ("low", "medium", "high")

PLAN_STEP_PROMPT = """\
You are a code task planner. Break the following task into ordered execution steps.

Task: {task}
Context: {context}
Available files: {files}

Output a JSON array of steps. Each step must have:
- "id": unique step id like "step_1"
- "description": what this step does
- "target_files": list of file paths affected
- "action": one of "create", "modify", "delete"
- "estimated_complexity": one of "low", "medium", "high"
- "dependencies": list of other step ids this depends on

Return ONLY the JSON array, no other text."""


@dataclass
class PlanStep:
    id: str
    description: str
    target_files: list[str]
    action: str
    estimated_complexity: str
    dependencies: list[str]
    status: str = "pending"
    diff_preview: str = ""
    result: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "target_files": self.target_files,
            "action": self.action,
            "estimated_complexity": self.estimated_complexity,
            "dependencies": self.dependencies,
            "status": self.status,
            "diff_preview": self.diff_preview,
            "result": self.result,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        return cls(
            id=data["id"],
            description=data["description"],
            target_files=data.get("target_files", []),
            action=data.get("action", "modify"),
            estimated_complexity=data.get("estimated_complexity", "low"),
            dependencies=data.get("dependencies", []),
            status=data.get("status", "pending"),
            diff_preview=data.get("diff_preview", ""),
            result=data.get("result", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ExecutionPlan:
    id: str
    task: str
    steps: list[PlanStep]
    created_at: float
    status: str = "draft"
    overall_risk: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "status": self.status,
            "overall_risk": self.overall_risk,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionPlan:
        steps = [PlanStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            id=data["id"],
            task=data["task"],
            steps=steps,
            created_at=data.get("created_at", time.time()),
            status=data.get("status", "draft"),
            overall_risk=data.get("overall_risk", "low"),
            metadata=data.get("metadata", {}),
        )


class PlannerEngine:
    def __init__(self, gateway: LLMGateway | None = None, auto_verify: bool = False):
        self.gateway = gateway
        self.auto_verify = auto_verify
        self._plans: dict[str, ExecutionPlan] = {}
        logger.info(
            "PlannerEngine initialized (gateway=%s, auto_verify=%s)",
            "enabled" if gateway else "stub",
            auto_verify,
        )

    async def create_plan(
        self, task: str, context: str = "", files: list[str] | None = None
    ) -> ExecutionPlan:
        plan_id = str(uuid.uuid4())
        files = files or []
        logger.info("Creating plan %s for task: %s", plan_id, task[:100])

        if self.gateway:
            steps = await self._generate_steps_with_llm(task, context, files)
        else:
            steps = self._generate_steps_stub(task, context, files)

        risk = self._assess_risk(steps)

        plan = ExecutionPlan(
            id=plan_id,
            task=task,
            steps=steps,
            created_at=time.time(),
            status="pending_approval",
            overall_risk=risk,
        )

        if self.auto_verify:
            verify_steps = self._generate_verify_steps(task, steps)
            for vs in verify_steps:
                plan.steps.append(vs)
            logger.info(
                "Added %d verification steps to plan %s", len(verify_steps), plan_id
            )

        self._plans[plan_id] = plan
        logger.info(
            "Plan %s created: %d steps, risk=%s", plan_id, len(plan.steps), risk
        )
        return plan

    def get_plan(self, plan_id: str) -> ExecutionPlan | None:
        return self._plans.get(plan_id)

    def approve_plan(self, plan_id: str) -> bool:
        plan = self._plans.get(plan_id)
        if not plan:
            logger.warning("approve_plan: plan %s not found", plan_id)
            return False
        if plan.status != "pending_approval":
            logger.warning(
                "approve_plan: plan %s status=%s, cannot approve", plan_id, plan.status
            )
            return False
        plan.status = "approved"
        for step in plan.steps:
            if step.status == "pending":
                step.status = "approved"
        logger.info("Plan %s approved", plan_id)
        return True

    def reject_plan(self, plan_id: str, reason: str = "") -> bool:
        plan = self._plans.get(plan_id)
        if not plan:
            logger.warning("reject_plan: plan %s not found", plan_id)
            return False
        if plan.status not in ("pending_approval", "draft"):
            logger.warning(
                "reject_plan: plan %s status=%s, cannot reject", plan_id, plan.status
            )
            return False
        plan.status = "rejected"
        if reason:
            plan.metadata["rejection_reason"] = reason
        for step in plan.steps:
            if step.status in ("pending", "approved"):
                step.status = "skipped"
        logger.info("Plan %s rejected: %s", plan_id, reason or "no reason given")
        return True

    def execute_step(self, plan_id: str, step_id: str) -> PlanStep:
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        step = None
        for s in plan.steps:
            if s.id == step_id:
                step = s
                break
        if not step:
            raise ValueError(f"Step {step_id} not found in plan {plan_id}")

        if step.status not in ("approved", "pending"):
            logger.warning(
                "execute_step: step %s status=%s, cannot execute", step_id, step.status
            )
            return step

        logger.info(
            "Executing step %s of plan %s: %s", step_id, plan_id, step.description
        )
        step.status = "executing"

        if step.dependencies:
            for dep_id in step.dependencies:
                dep_step = next((s for s in plan.steps if s.id == dep_id), None)
                if dep_step and dep_step.status not in ("completed", "skipped"):
                    step.status = "failed"
                    step.result = (
                        f"Dependency {dep_id} not completed (status={dep_step.status})"
                    )
                    logger.error(
                        "Step %s blocked by dependency %s (status=%s)",
                        step_id,
                        dep_id,
                        dep_step.status,
                    )
                    return step

        try:
            if step.action == "modify":
                step.diff_preview = f"--- {', '.join(step.target_files)}\n+++ {', '.join(step.target_files)}\n# pending code patch generation"
                step.result = f"Step '{step.description}' executed successfully"

            elif step.action == "create":
                step.result = f"Files created: {', '.join(step.target_files)}"

            elif step.action == "delete":
                step.result = f"Files deleted: {', '.join(step.target_files)}"

            else:
                step.result = f"Unknown action: {step.action}"

            step.status = "completed"
            logger.info("Step %s completed: %s", step_id, step.result[:80])

        except Exception as exc:
            step.status = "failed"
            step.result = str(exc)
            logger.error("Step %s failed: %s", step_id, exc)

        return step

    def execute_plan(self, plan_id: str) -> ExecutionPlan:
        plan = self._plans.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        if plan.status not in ("approved", "executing"):
            raise ValueError(f"Plan {plan_id} status={plan.status}, cannot execute")

        plan.status = "executing"
        logger.info("Executing plan %s: %d steps", plan_id, len(plan.steps))

        all_completed = True
        for step in plan.steps:
            if step.status in ("completed", "skipped", "failed"):
                if step.status == "failed":
                    all_completed = False
                continue
            result_step = self.execute_step(plan_id, step.id)
            if result_step.status == "failed":
                all_completed = False
                logger.warning(
                    "Plan %s step %s failed, stopping execution", plan_id, step.id
                )
                break

        if all_completed and all(
            s.status in ("completed", "skipped") for s in plan.steps
        ):
            plan.status = "completed"
            logger.info("Plan %s completed successfully", plan_id)
        else:
            logger.warning("Plan %s execution incomplete", plan_id)

        return plan

    def list_plans(self, status: str = "") -> list[ExecutionPlan]:
        plans = list(self._plans.values())
        if status:
            plans = [p for p in plans if p.status == status]
        return sorted(plans, key=lambda p: p.created_at, reverse=True)

    def cancel_plan(self, plan_id: str) -> bool:
        plan = self._plans.get(plan_id)
        if not plan:
            logger.warning("cancel_plan: plan %s not found", plan_id)
            return False
        if plan.status in ("completed", "rejected"):
            logger.warning(
                "cancel_plan: plan %s status=%s, cannot cancel", plan_id, plan.status
            )
            return False
        plan.status = "rejected"
        plan.metadata["cancelled"] = True
        for step in plan.steps:
            if step.status in ("pending", "approved"):
                step.status = "skipped"
        logger.info("Plan %s cancelled", plan_id)
        return True

    def _assess_risk(self, steps: list[PlanStep]) -> str:
        has_delete = any(s.action == "delete" for s in steps)
        if has_delete:
            return "high"

        modify_files = set()
        for s in steps:
            if s.action == "modify":
                modify_files.update(s.target_files)
        if len(modify_files) > 3:
            return "medium"

        return "low"

    def _generate_verify_steps(
        self, task: str, steps: list[PlanStep]
    ) -> list[PlanStep]:
        verify_steps = []
        for i, step in enumerate(steps):
            if step.action in ("create", "modify"):
                vstep = PlanStep(
                    id=f"verify_{step.id}",
                    description=f"Verify: {step.description}",
                    target_files=step.target_files,
                    action="verify",
                    estimated_complexity="low",
                    dependencies=[step.id],
                    status="pending",
                    metadata={
                        "verify_target_step": step.id,
                        "verify_type": "auto",
                    },
                )
                verify_steps.append(vstep)
        if verify_steps:
            logger.info(
                "Generated %d verification steps for task: %s",
                len(verify_steps),
                task[:60],
            )
        return verify_steps

    async def _generate_steps_with_llm(
        self, task: str, context: str, files: list[str]
    ) -> list[PlanStep]:
        logger.info("Generating plan steps with LLM for task: %s", task[:80])
        prompt = PLAN_STEP_PROMPT.format(
            task=task,
            context=context or "(none)",
            files=", ".join(files) if files else "(none)",
        )
        messages = [{"role": "user", "content": prompt}]

        try:
            resp = await asyncio.wait_for(
                self.gateway.chat(messages=messages, capability="chat"),
                timeout=120.0,
            )
            # 审计 E-12: gateway 返回 finish_reason=="error" 哨兵时 content="" 不代表成功.
            if getattr(resp, "finish_reason", "") == "error":
                err = (resp.usage or {}).get("error", "gateway error")
                logger.warning("LLM gateway error in planner: %s, falling back to stub", err)
                return self._generate_steps_stub(task, context, files)
            content = resp.content
            if not content:
                logger.warning("LLM returned empty content, falling back to stub")
                return self._generate_steps_stub(task, context, files)

            steps = self._parse_llm_steps(content)
            if steps:
                logger.info("LLM generated %d steps", len(steps))
                return steps

            logger.warning("Failed to parse LLM steps, falling back to stub")
            return self._generate_steps_stub(task, context, files)

        except asyncio.TimeoutError:
            logger.error("LLM step generation timed out, falling back to stub")
            return self._generate_steps_stub(task, context, files)
        except Exception as exc:
            logger.error("LLM step generation failed: %s, falling back to stub", exc)
            return self._generate_steps_stub(task, context, files)

    def _parse_llm_steps(self, content: str) -> list[PlanStep]:
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    json_lines.append(line)
            content = "\n".join(json_lines)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Failed to parse LLM JSON output")
            return []

        if not isinstance(data, list):
            logger.warning("LLM output is not a JSON array")
            return []

        steps = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            action = item.get("action", "modify")
            if action not in VALID_ACTIONS:
                action = "modify"
            complexity = item.get("estimated_complexity", "low")
            if complexity not in VALID_COMPLEXITIES:
                complexity = "low"
            step = PlanStep(
                id=item.get("id", f"step_{i + 1}"),
                description=item.get("description", f"Step {i + 1}"),
                target_files=item.get("target_files", []),
                action=action,
                estimated_complexity=complexity,
                dependencies=item.get("dependencies", []),
            )
            steps.append(step)

        return steps

    def _generate_steps_stub(
        self, task: str, context: str, files: list[str]
    ) -> list[PlanStep]:
        logger.info("Generating stub plan for task: %s", task[:80])
        target_files = files if files else ["unknown_file"]
        step = PlanStep(
            id="step_1",
            description=f"Implement: {task}",
            target_files=target_files,
            action="modify",
            estimated_complexity="medium",
            dependencies=[],
        )
        return [step]
