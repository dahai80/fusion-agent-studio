from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .context import AgentContext, AgentEventType

logger = logging.getLogger(__name__)


class WorkflowPattern(str, Enum):
    PIPELINE = "pipeline"
    PARALLEL_BARRIER = "parallel_barrier"
    LOOP_UNTIL_DRY = "loop_until_dry"
    LOOP_UNTIL_BUDGET = "loop_until_budget"
    ADVERSARIAL_VERIFY = "adversarial_verify"
    JUDGE_PANEL = "judge_panel"


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkflowPhase:
    name: str
    pattern: WorkflowPattern = WorkflowPattern.PIPELINE
    agent_configs: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pattern": self.pattern.value,
            "agent_configs": self.agent_configs,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowPhase:
        return cls(
            name=data.get("name", ""),
            pattern=WorkflowPattern(data.get("pattern", "pipeline")),
            agent_configs=data.get("agent_configs", []),
            config=data.get("config", {}),
        )


@dataclass
class WorkflowRun:
    id: str = ""
    workflow_id: str = ""
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_phase: int = 0
    phase_results: list[dict] = field(default_factory=list)
    final_result: dict = field(default_factory=dict)
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    metadata: dict = field(default_factory=dict)
    _pause_event: asyncio.Event | None = field(default=None, repr=False)
    _cancel_flag: bool = field(default=False, repr=False)

    def __post_init__(self):
        if not self.id:
            self.id = f"wf_run_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()
        if self._pause_event is None:
            self._pause_event = asyncio.Event()
            self._pause_event.set()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "current_phase": self.current_phase,
            "phase_results": self.phase_results,
            "final_result": self.final_result,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowRun:
        return cls(
            id=data.get("id", ""),
            workflow_id=data.get("workflow_id", ""),
            status=WorkflowStatus(data.get("status", "pending")),
            current_phase=data.get("current_phase", 0),
            phase_results=data.get("phase_results", []),
            final_result=data.get("final_result", {}),
            created_at=data.get("created_at", 0.0),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at", 0.0),
            error=data.get("error", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class WorkflowConfig:
    id: str = ""
    name: str = ""
    phases: list[WorkflowPhase] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    created_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = f"wf_{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "phases": [p.to_dict() for p in self.phases],
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowConfig:
        phases = [WorkflowPhase.from_dict(p) for p in data.get("phases", [])]
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            phases=phases,
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            created_at=data.get("created_at", 0.0),
            metadata=data.get("metadata", {}),
        )


class WorkflowEngine:
    def __init__(self, llm_gateway=None, tool_registry=None, orchestrator=None):
        self.llm_gateway = llm_gateway
        self.tool_registry = tool_registry
        self.orchestrator = orchestrator
        self._workflows: dict[str, WorkflowConfig] = {}
        self._runs: dict[str, WorkflowRun] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        logger.info(
            "WorkflowEngine init, llm_gateway=%s, orchestrator=%s",
            "provided" if llm_gateway else "none",
            "provided" if orchestrator else "none",
        )

    def create_workflow(
        self, name: str, phases: list[dict], **kwargs
    ) -> WorkflowConfig:
        phase_objects = [WorkflowPhase.from_dict(p) for p in phases]
        wf = WorkflowConfig(
            name=name,
            phases=phase_objects,
            input_schema=kwargs.get("input_schema", {}),
            output_schema=kwargs.get("output_schema", {}),
            metadata=kwargs.get("metadata", {}),
        )
        self._workflows[wf.id] = wf
        logger.info(
            "Created workflow %s id=%s with %d phases", name, wf.id, len(phase_objects)
        )
        return wf

    def get_workflow(self, workflow_id: str) -> WorkflowConfig | None:
        return self._workflows.get(workflow_id)

    def list_workflows(self) -> list[WorkflowConfig]:
        return list(self._workflows.values())

    def delete_workflow(self, workflow_id: str) -> bool:
        if workflow_id in self._workflows:
            del self._workflows[workflow_id]
            logger.info("Deleted workflow %s", workflow_id)
            return True
        return False

    async def execute_workflow(
        self,
        workflow_id: str,
        initial_input: str = "",
        budget: int | None = None,
    ) -> WorkflowRun:
        wf = self._workflows.get(workflow_id)
        if not wf:
            raise ValueError(f"Workflow not found: {workflow_id}")

        run = WorkflowRun(workflow_id=workflow_id, status=WorkflowStatus.RUNNING)
        run.started_at = time.time()
        self._runs[run.id] = run
        logger.info("Starting workflow run %s for workflow %s", run.id, workflow_id)

        try:
            result = await self._execute_phases(wf, run, initial_input, budget)
            run.final_result = result
            run.status = WorkflowStatus.COMPLETED
            run.finished_at = time.time()
            logger.info(
                "Workflow run %s completed in %.2fs",
                run.id,
                run.finished_at - run.started_at,
            )
        except asyncio.CancelledError:
            run.status = WorkflowStatus.CANCELLED
            run.finished_at = time.time()
            logger.info("Workflow run %s cancelled", run.id)
        except Exception as e:
            run.status = WorkflowStatus.FAILED
            run.error = str(e)
            run.finished_at = time.time()
            logger.exception("Workflow run %s failed", run.id)
        return run

    async def _execute_phases(
        self,
        wf: WorkflowConfig,
        run: WorkflowRun,
        initial_input: str,
        budget: int | None,
    ) -> dict:
        current_input = initial_input
        accumulated_results: list[dict] = []
        tokens_used = 0

        for idx, phase in enumerate(wf.phases):
            if run._cancel_flag:
                raise asyncio.CancelledError()

            await run._pause_event.wait()

            run.current_phase = idx
            logger.info(
                "Phase %d/%d '%s' pattern=%s run=%s",
                idx + 1,
                len(wf.phases),
                phase.name,
                phase.pattern.value,
                run.id,
            )

            phase_start = time.time()
            try:
                if phase.pattern == WorkflowPattern.PIPELINE:
                    result = await self._exec_pipeline(phase, current_input)
                elif phase.pattern == WorkflowPattern.PARALLEL_BARRIER:
                    result = await self._exec_parallel_barrier(phase, current_input)
                elif phase.pattern == WorkflowPattern.LOOP_UNTIL_DRY:
                    result = await self._exec_loop_until_dry(phase, current_input)
                elif phase.pattern == WorkflowPattern.LOOP_UNTIL_BUDGET:
                    result = await self._exec_loop_until_budget(
                        phase, current_input, budget, tokens_used
                    )
                elif phase.pattern == WorkflowPattern.ADVERSARIAL_VERIFY:
                    result = await self._exec_adversarial_verify(phase, current_input)
                elif phase.pattern == WorkflowPattern.JUDGE_PANEL:
                    result = await self._exec_judge_panel(phase, current_input)
                else:
                    result = {
                        "output": current_input,
                        "error": f"Unknown pattern: {phase.pattern}",
                    }
            except Exception as e:
                logger.exception("Phase %s failed", phase.name)
                result = {"output": "", "error": str(e)}

            phase_duration = time.time() - phase_start
            result["phase"] = phase.name
            result["duration"] = phase_duration
            accumulated_results.append(result)
            run.phase_results.append(result)

            if result.get("output"):
                current_input = result["output"]
            tokens_used += result.get("tokens", 0)

            if result.get("error") and not phase.config.get("continue_on_error"):
                raise RuntimeError(f"Phase '{phase.name}' failed: {result['error']}")

        return {
            "output": current_input,
            "phases": accumulated_results,
            "total_phases": len(wf.phases),
            "tokens_used": tokens_used,
        }

    async def _exec_pipeline(self, phase: WorkflowPhase, current_input: str) -> dict:
        if not phase.agent_configs:
            return {"output": current_input}

        items = phase.config.get("items", [current_input])
        stages = phase.agent_configs
        results = []

        for item in items:
            stage_input = item
            for stage_idx, agent_cfg in enumerate(stages):
                output = await self._run_agent(agent_cfg, stage_input)
                results.append({"item": item, "stage": stage_idx, "output": output})
                stage_input = output

        final_output = results[-1]["output"] if results else current_input
        return {"output": final_output, "stage_results": results}

    async def _exec_parallel_barrier(
        self, phase: WorkflowPhase, current_input: str
    ) -> dict:
        if not phase.agent_configs:
            return {"output": current_input}

        tasks = []
        for agent_cfg in phase.agent_configs:
            tasks.append(self._run_agent(agent_cfg, current_input))
        outputs = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        combined_output = []
        for i, out in enumerate(outputs):
            if isinstance(out, Exception):
                results.append(
                    {
                        "agent": phase.agent_configs[i].get("name", f"agent_{i}"),
                        "error": str(out),
                    }
                )
            else:
                results.append(
                    {
                        "agent": phase.agent_configs[i].get("name", f"agent_{i}"),
                        "output": out,
                    }
                )
                combined_output.append(out)

        merged = "\n\n".join(combined_output) if combined_output else ""
        return {"output": merged, "agent_results": results}

    async def _exec_loop_until_dry(
        self, phase: WorkflowPhase, current_input: str
    ) -> dict:
        max_dry = phase.config.get("dry_threshold", 2)
        max_iterations = phase.config.get("max_iterations", 10)
        dry_count = 0
        all_results = []
        seen = set()
        iteration = 0

        while dry_count < max_dry and iteration < max_iterations:
            iteration += 1
            if not phase.agent_configs:
                break

            tasks = [self._run_agent(cfg, current_input) for cfg in phase.agent_configs]
            outputs = await asyncio.gather(*tasks, return_exceptions=True)

            new_findings = []
            for i, out in enumerate(outputs):
                if isinstance(out, Exception):
                    continue
                key = out[:100] if isinstance(out, str) else str(out)[:100]
                if key not in seen:
                    seen.add(key)
                    new_findings.append(out)
                    all_results.append(out)

            if not new_findings:
                dry_count += 1
                logger.info("Loop-until-dry: dry round %d/%d", dry_count, max_dry)
            else:
                dry_count = 0
                current_input = "\n".join(new_findings)
                logger.info(
                    "Loop-until-dry: iteration %d found %d new items",
                    iteration,
                    len(new_findings),
                )

        return {
            "output": "\n\n".join(all_results),
            "iterations": iteration,
            "total_findings": len(all_results),
        }

    async def _exec_loop_until_budget(
        self,
        phase: WorkflowPhase,
        current_input: str,
        budget: int | None,
        tokens_used: int,
    ) -> dict:
        if budget is None:
            budget = phase.config.get("default_budget", 50000)

        cost_per_iteration = phase.config.get("cost_per_iteration", 5000)
        max_iterations = budget // max(cost_per_iteration, 1)
        all_results = []
        iteration = 0

        while iteration < max_iterations and tokens_used < budget:
            iteration += 1
            if not phase.agent_configs:
                break

            tasks = [self._run_agent(cfg, current_input) for cfg in phase.agent_configs]
            outputs = await asyncio.gather(*tasks, return_exceptions=True)

            for out in outputs:
                if not isinstance(out, Exception):
                    all_results.append(out)

            current_input = all_results[-1] if all_results else current_input
            tokens_used += cost_per_iteration

        return {
            "output": "\n\n".join(all_results) if all_results else current_input,
            "iterations": iteration,
            "budget_used": tokens_used,
            "budget_total": budget,
        }

    async def _exec_adversarial_verify(
        self, phase: WorkflowPhase, current_input: str
    ) -> dict:
        voter_count = phase.config.get("voter_count", 3)
        threshold = phase.config.get("threshold", 0.6)

        tasks = []
        for i in range(voter_count):
            cfg = (
                phase.agent_configs[i % len(phase.agent_configs)]
                if phase.agent_configs
                else {}
            )
            prompt_prefix = (
                f"You are skeptic #{i + 1}. Try to REFUTE the following claim. "
            )
            tasks.append(self._run_agent(cfg, prompt_prefix + current_input))

        votes = await asyncio.gather(*tasks, return_exceptions=True)

        refuted = 0
        survived = 0
        vote_details = []
        for i, vote in enumerate(votes):
            if isinstance(vote, Exception):
                refuted += 1
                vote_details.append(
                    {"voter": i, "verdict": "refuted", "error": str(vote)}
                )
            else:
                is_refute = (
                    "refuted" in vote.lower() or "false" in vote.lower()
                    if isinstance(vote, str)
                    else False
                )
                if is_refute:
                    refuted += 1
                    vote_details.append({"voter": i, "verdict": "refuted"})
                else:
                    survived += 1
                    vote_details.append({"voter": i, "verdict": "survived"})

        passes = (survived / max(voter_count, 1)) >= threshold
        logger.info(
            "Adversarial verify: %d/%d survived, passes=%s",
            survived,
            voter_count,
            passes,
        )

        return {
            "output": current_input if passes else "",
            "passes": passes,
            "survived": survived,
            "refuted": refuted,
            "votes": vote_details,
        }

    async def _exec_judge_panel(self, phase: WorkflowPhase, current_input: str) -> dict:
        if not phase.agent_configs:
            return {"output": current_input}

        approaches = []
        for cfg in phase.agent_configs:
            output = await self._run_agent(cfg, current_input)
            approaches.append({"agent": cfg.get("name", ""), "output": output})

        judge_cfg = phase.config.get(
            "judge_agent", phase.agent_configs[0] if phase.agent_configs else {}
        )
        scoring_prompt = (
            "Score the following approaches on a 0-10 scale. "
            'Return JSON: {"scores": [{"index": 0, "score": N, "reason": "..."}], "winner": 0}\n\n'
        )
        for i, a in enumerate(approaches):
            scoring_prompt += f"Approach {i}: {a['output'][:500]}\n\n"

        judge_output = await self._run_agent(judge_cfg, scoring_prompt)

        winner_idx = 0
        try:
            if isinstance(judge_output, str):
                parsed = json.loads(judge_output)
                winner_idx = parsed.get("winner", 0)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Judge output not valid JSON, picking first approach")

        winner_idx = min(winner_idx, len(approaches) - 1)
        winner = approaches[winner_idx] if approaches else {"output": current_input}

        return {
            "output": winner.get("output", current_input),
            "approaches": approaches,
            "winner_index": winner_idx,
            "judge_output": judge_output,
        }

    async def _run_agent(self, agent_cfg: dict, input_text: str) -> str:
        if not agent_cfg:
            return input_text

        agent_name = agent_cfg.get("name", "unknown")
        graph_id = agent_cfg.get("graph_id")
        system_prompt = agent_cfg.get("system_prompt", "")

        if graph_id and self.orchestrator:
            try:
                from .runtime import AgentRuntime
                from .graph import AgentGraph

                store = self.orchestrator.__dict__.get("_store")
                if store:
                    graph_data = store.load_graph(graph_id)
                    if graph_data:
                        graph = AgentGraph.from_dict(graph_data)
                    else:
                        graph = AgentGraph(id=graph_id)
                else:
                    graph = AgentGraph(id=graph_id or f"g_{uuid.uuid4().hex[:8]}")

                runtime = AgentRuntime(
                    tool_registry=self.tool_registry,
                    llm_gateway=self.llm_gateway,
                )
                ctx = AgentContext()
                ctx.metadata["agent_name"] = agent_name

                events = []
                async for event in runtime.execute_graph(graph, input_text, ctx):
                    events.append(event)

                output = ""
                for ev in reversed(events):
                    if ev.type == AgentEventType.THINK and ev.content:
                        output = ev.content
                        break
                if not output:
                    for msg in reversed(ctx.messages):
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            output = msg.get("content", "")
                            break

                logger.info(
                    "Agent %s completed, output len=%d", agent_name, len(output)
                )
                return output

            except Exception:
                logger.exception("Agent %s execution failed", agent_name)
                raise

        if self.llm_gateway:
            try:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": input_text})

                response = await self.llm_gateway.chat(
                    messages=messages, temperature=0.3, max_tokens=2048
                )
                content = ""
                if hasattr(response, "content"):
                    content = response.content or ""
                elif isinstance(response, dict):
                    content = response.get("content", "")
                logger.info(
                    "LLM call for agent %s, output len=%d", agent_name, len(content)
                )
                return content
            except Exception:
                logger.exception("LLM call failed for agent %s", agent_name)
                raise

        logger.warning("No graph or LLM gateway available for agent %s", agent_name)
        return input_text

    def pause_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run or run.status != WorkflowStatus.RUNNING:
            return False
        run._pause_event.clear()
        run.status = WorkflowStatus.PAUSED
        logger.info("Paused workflow run %s", run_id)
        return True

    def resume_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run or run.status != WorkflowStatus.PAUSED:
            return False
        run._pause_event.set()
        run.status = WorkflowStatus.RUNNING
        logger.info("Resumed workflow run %s", run_id)
        return True

    def cancel_run(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run or run.status not in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
            return False
        run._cancel_flag = True
        if run.status == WorkflowStatus.PAUSED:
            run._pause_event.set()
        task = self._active_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        logger.info("Cancelled workflow run %s", run_id)
        return True

    def get_run_status(self, run_id: str) -> WorkflowRun | None:
        return self._runs.get(run_id)

    def list_runs(self, workflow_id: str | None = None) -> list[WorkflowRun]:
        if workflow_id:
            return [r for r in self._runs.values() if r.workflow_id == workflow_id]
        return list(self._runs.values())
