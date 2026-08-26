"""Multi-agent orchestrator — coordinates parallel, sequential, and master-worker execution.

Supports six patterns:
- Sequential: agents run one after another, output chains forward
- Parallel: agents run simultaneously with shared input
- Master-Worker: master decomposes task, workers execute sub-tasks, master summarizes
- Handoff: agents pass control along a chain with full context transfer
- Broadcast/Plaza: one input fans out to all agents, results are collected and merged
- Supervisor: a supervisor agent monitors and routes tasks to worker agents
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .compactor import Compactor
from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph
from .llm_gateway import LLMGateway

if TYPE_CHECKING:
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for a single agent in multi-agent orchestration."""

    name: str
    graph: AgentGraph
    system_prompt: str = ""
    model: str = ""


@dataclass
class HandoffContext:
    """Context passed between agents in a handoff chain."""

    sender: str
    receiver: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class OrchestrationResult:
    """Result of a multi-agent orchestration run."""

    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_duration: float = 0.0
    summary: str = ""


class MultiAgentOrchestrator:
    """Coordinates multiple agents running in parallel or sequence."""

    def __init__(
        self,
        mlx_client: "FusionMLXClient | None" = None,
        tool_registry: "ToolRegistry | None" = None,
        max_concurrent: int = 5,
        llm_gateway: LLMGateway | None = None,
        swarm_router=None,
        plaza=None,
        fmp=None,
        safety_gateway=None,
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        self.max_concurrent = max_concurrent
        self.swarm_router = swarm_router
        self.plaza = plaza
        self.fmp = fmp
        # 审计 P2/dim3: team.orchestrate 起的子 AgentRuntime 原不传 safety_gateway
        # -> 编排模式全无安全门 (注入/危险工具/审批全失效). 透传到每个子 runtime.
        self.safety_gateway = safety_gateway

        if llm_gateway:
            self.llm_gateway = llm_gateway
        elif mlx_client:
            gw = LLMGateway()
            gw.set_default_client(mlx_client)
            self.llm_gateway = gw
        else:
            self.llm_gateway = LLMGateway()

        self.compactor = Compactor()
        if hasattr(self.llm_gateway, "set_compactor"):
            self.llm_gateway.set_compactor(self.compactor)
        self._limits = {"max_concurrent": max_concurrent, "max_depth": 10}

        logger.info(
            "MultiAgentOrchestrator init, mlx_client=%s, llm_gateway=%s",
            "provided" if mlx_client else "none",
            "provided" if llm_gateway else ("auto-from-mlx" if mlx_client else "empty"),
        )

    async def sequential(
        self,
        agents: list[AgentConfig],
        initial_input: str,
    ) -> OrchestrationResult:
        """Execute agents sequentially — each agent's output feeds the next."""
        result = OrchestrationResult()
        start = time.time()
        current_input = initial_input

        for agent_config in agents:
            try:
                from .runtime import AgentRuntime

                runtime = AgentRuntime(
                    tool_registry=self.tools,
                    llm_gateway=self.llm_gateway,
                    safety_gateway=self.safety_gateway,
                )
                ctx = AgentContext()
                ctx.metadata["agent_name"] = agent_config.name

                events = []
                async for event in runtime.execute_graph(
                    agent_config.graph, current_input, ctx
                ):
                    events.append(event)

                final_content = self._extract_final_output(events, ctx)
                agent_result = {
                    "agent": agent_config.name,
                    "output": final_content,
                    "events": [e.to_dict() for e in events],
                    "usage": ctx.token_usage(),
                    "duration": ctx.elapsed_seconds(),
                }
                result.results.append(agent_result)
                current_input = final_content

            except Exception as e:
                logger.exception("Agent %s failed", agent_config.name)
                result.errors.append(f"{agent_config.name}: {e}")

        result.total_duration = time.time() - start
        return result

    async def parallel(
        self,
        agents: list[AgentConfig],
        input_template: str = "",
        per_agent_inputs: list[str] | None = None,
    ) -> OrchestrationResult:
        """Execute agents in parallel — all agents run simultaneously.

        Args:
            agents: List of agent configurations.
            input_template: Shared input for all agents.
            per_agent_inputs: Optional per-agent inputs (overrides input_template).
        """
        result = OrchestrationResult()
        start = time.time()
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def run_single(
            agent_config: AgentConfig, agent_input: str
        ) -> dict[str, Any]:
            async with semaphore:
                try:
                    from .runtime import AgentRuntime

                    runtime = AgentRuntime(
                        tool_registry=self.tools, llm_gateway=self.llm_gateway
                    )
                    ctx = AgentContext()
                    ctx.metadata["agent_name"] = agent_config.name

                    events = []
                    async for event in runtime.execute_graph(
                        agent_config.graph, agent_input, ctx
                    ):
                        events.append(event)

                    final_content = self._extract_final_output(events, ctx)
                    return {
                        "agent": agent_config.name,
                        "output": final_content,
                        "events": [e.to_dict() for e in events],
                        "usage": ctx.token_usage(),
                        "duration": ctx.elapsed_seconds(),
                        "error": "",
                    }
                except Exception as e:
                    logger.exception("Agent %s failed", agent_config.name)
                    return {
                        "agent": agent_config.name,
                        "output": "",
                        "events": [],
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total": 0,
                        },
                        "duration": 0.0,
                        "error": str(e),
                    }

        tasks = []
        for i, agent in enumerate(agents):
            if per_agent_inputs and i < len(per_agent_inputs):
                agent_input = per_agent_inputs[i]
            else:
                agent_input = input_template
            tasks.append(run_single(agent, agent_input))

        results = await asyncio.gather(*tasks)

        for r in results:
            if r["error"]:
                result.errors.append(f"{r['agent']}: {r['error']}")
            result.results.append(r)

        result.total_duration = time.time() - start
        return result

    async def master_worker(
        self,
        master: AgentConfig,
        workers: list[AgentConfig],
        task: str,
    ) -> OrchestrationResult:
        """Master agent decomposes a task, workers execute sub-tasks, master summarizes."""
        result = OrchestrationResult()
        start = time.time()

        from .runtime import AgentRuntime

        master_runtime = AgentRuntime(
            tool_registry=self.tools,
            llm_gateway=self.llm_gateway,
            safety_gateway=self.safety_gateway,
        )
        master_ctx = AgentContext()

        decompose_prompt = (
            f"Decompose the following task into {len(workers)} sub-tasks, "
            f"one for each worker. Return a JSON array of sub-task descriptions.\n\n{task}"
        )

        master_events = []
        async for event in master_runtime.execute_graph(
            master.graph, decompose_prompt, master_ctx
        ):
            master_events.append(event)

        sub_tasks = self._extract_sub_tasks(master_ctx, len(workers))
        logger.info(
            "Master decomposed task into %d sub-tasks: %s", len(sub_tasks), sub_tasks
        )

        worker_results = await self.parallel(
            workers,
            per_agent_inputs=sub_tasks,
        )

        summary_prompt = (
            f"Original task: {task}\n\n"
            f"Worker results:\n"
            + json.dumps(
                [
                    {"worker": r["agent"], "output": r["output"]}
                    for r in worker_results.results
                ],
                indent=2,
                ensure_ascii=False,
            )
            + "\n\nProvide a comprehensive summary of the results."
        )

        summary_ctx = AgentContext()
        summary_events = []
        async for event in master_runtime.execute_graph(
            master.graph, summary_prompt, summary_ctx
        ):
            summary_events.append(event)

        summary = self._extract_final_output(summary_events, summary_ctx)

        result.results = [
            {
                "phase": "decomposition",
                "output": self._extract_final_output(master_events, master_ctx),
                "sub_tasks": sub_tasks,
            },
            *worker_results.results,
            {
                "phase": "summary",
                "output": summary,
            },
        ]
        result.errors = worker_results.errors
        result.summary = summary
        result.total_duration = time.time() - start
        return result

    async def handoff(
        self,
        agents: list[AgentConfig],
        initial_input: str,
    ) -> OrchestrationResult:
        """Execute agents in a handoff chain — each agent passes control forward with context."""
        result = OrchestrationResult()
        start = time.time()

        from .runtime import AgentRuntime

        accumulated_context = initial_input
        handoff_chain: list[HandoffContext] = []
        task_id = f"handoff_{uuid.uuid4().hex[:8]}"

        for i, agent_config in enumerate(agents):
            try:
                runtime = AgentRuntime(
                    tool_registry=self.tools,
                    llm_gateway=self.llm_gateway,
                    safety_gateway=self.safety_gateway,
                )
                ctx = AgentContext()
                ctx.metadata["agent_name"] = agent_config.name
                ctx.metadata["handoff_index"] = i

                if handoff_chain:
                    handoff_summary = self._format_handoff_history(handoff_chain)
                    agent_input = f"{accumulated_context}\n\n{handoff_summary}"
                else:
                    agent_input = accumulated_context

                events = []
                async for event in runtime.execute_graph(
                    agent_config.graph, agent_input, ctx
                ):
                    events.append(event)

                final_content = self._extract_final_output(events, ctx)

                handoff_ctx = HandoffContext(
                    sender=agent_config.name,
                    receiver=agents[i + 1].name if i + 1 < len(agents) else "__end__",
                    content=final_content,
                    metadata={"handoff_index": i},
                    timestamp=time.time(),
                )
                handoff_chain.append(handoff_ctx)

                result.results.append(
                    {
                        "agent": agent_config.name,
                        "output": final_content,
                        "events": [e.to_dict() for e in events],
                        "usage": ctx.token_usage(),
                        "duration": ctx.elapsed_seconds(),
                        "handoff_to": handoff_ctx.receiver,
                    }
                )

                accumulated_context = final_content

                if self.swarm_router and i + 1 < len(agents):
                    from .swarm_router import (
                        HandoffContext as SwarmHandoffContext,
                    )
                    from .swarm_router import (
                        SwarmAgent,
                    )

                    if not self.swarm_router.get_agent(agent_config.name):
                        self.swarm_router.register_agent(
                            SwarmAgent(id=agent_config.name, name=agent_config.name)
                        )
                    nxt = agents[i + 1]
                    if not self.swarm_router.get_agent(nxt.name):
                        self.swarm_router.register_agent(
                            SwarmAgent(id=nxt.name, name=nxt.name)
                        )
                    swarm_ctx = SwarmHandoffContext(
                        conversation=[
                            {"role": agent_config.name, "content": final_content}
                        ],
                        hop_count=i,
                        task_id=task_id,
                    )
                    if (
                        self.swarm_router.handoff(
                            agent_config.name, nxt.name, swarm_ctx
                        )
                        is None
                    ):
                        logger.info(
                            "SwarmRouter blocked handoff at hop %d, stopping chain",
                            i + 1,
                        )
                        if result.results:
                            result.results[-1]["swarm_blocked"] = True
                        break

                if self._is_handoff_complete(final_content):
                    logger.info(
                        "Agent %s completed task, stopping handoff chain",
                        agent_config.name,
                    )
                    break

            except Exception as e:
                logger.exception("Agent %s failed in handoff chain", agent_config.name)
                result.errors.append(f"{agent_config.name}: {e}")
                break

        result.total_duration = time.time() - start
        return result

    async def broadcast(
        self,
        agents: list[AgentConfig],
        input_text: str,
        merge_strategy: str = "concat",
    ) -> OrchestrationResult:
        """Broadcast same input to all agents, collect and merge results.

        Also known as Plaza pattern.

        Args:
            agents: List of agent configurations.
            input_text: The input to broadcast to all agents.
            merge_strategy: How to merge results: "concat", "best", or "json".
        """
        result = OrchestrationResult()
        start = time.time()

        channel_name = None
        if self.plaza:
            channel_name = f"broadcast_{uuid.uuid4().hex[:8]}"
            try:
                self.plaza.create_channel(channel_name, [a.name for a in agents])
            except Exception as e:
                logger.warning("Plaza create_channel failed: %s", e)
                channel_name = None

        parallel_result = await self.parallel(agents, input_template=input_text)
        result.errors = parallel_result.errors

        outputs = {}
        for r in parallel_result.results:
            outputs[r["agent"]] = r.get("output", "")
            if self.plaza and channel_name:
                try:
                    self.plaza.broadcast(channel_name, r["agent"], r.get("output", ""))
                except Exception as e:
                    logger.warning(
                        "Plaza broadcast record failed for %s: %s", r["agent"], e
                    )

        merged = self._merge_outputs(outputs, merge_strategy)

        result.results = parallel_result.results
        result.summary = merged
        result.total_duration = time.time() - start
        return result

    async def supervisor(
        self,
        supervisor_config: AgentConfig,
        workers: list[AgentConfig],
        task: str,
        max_rounds: int = 5,
    ) -> OrchestrationResult:
        """Supervisor agent routes tasks to workers and evaluates results.

        The supervisor analyzes the task, assigns it to the best-fit worker,
        reviews the worker's output, and either accepts it or re-routes.

        Args:
            supervisor_config: The supervisor agent that routes and evaluates.
            workers: Available worker agents.
            task: The initial task description.
            max_rounds: Maximum routing rounds before forcing completion.
        """
        result = OrchestrationResult()
        start = time.time()

        from .runtime import AgentRuntime

        worker_map = {w.name: w for w in workers}
        current_task = task
        routing_history: list[dict[str, Any]] = []

        for round_num in range(1, max_rounds + 1):
            logger.info("Supervisor round %d/%d", round_num, max_rounds)

            supervisor_runtime = AgentRuntime(
                tool_registry=self.tools,
                llm_gateway=self.llm_gateway,
                safety_gateway=self.safety_gateway,
            )
            supervisor_ctx = AgentContext()
            supervisor_ctx.metadata["agent_name"] = supervisor_config.name
            supervisor_ctx.metadata["round"] = round_num

            worker_names = ", ".join(worker_map.keys())
            route_prompt = (
                f"You are a supervisor. Available workers: [{worker_names}].\n\n"
                f"Current task: {current_task}\n\n"
            )
            if routing_history:
                route_prompt += "Previous routing:\n"
                for h in routing_history:
                    route_prompt += f"  - Routed to {h['worker']}: {h['status']}\n"
                route_prompt += "\n"
            route_prompt += (
                "Respond with a JSON object:\n"
                '{"worker": "<name>", "instruction": "<what to tell the worker>", '
                '"done": true/false}\n'
                'If the task is complete, set done=true and worker to "__end__".'
            )

            route_events = []
            async for event in supervisor_runtime.execute_graph(
                supervisor_config.graph, route_prompt, supervisor_ctx
            ):
                route_events.append(event)

            route_output = self._extract_final_output(route_events, supervisor_ctx)
            route_decision = self._parse_route_decision(route_output)

            logger.info("Supervisor decided: %s", route_decision)

            if route_decision.get("done") or route_decision.get("worker") == "__end__":
                result.results.append(
                    {
                        "phase": f"round_{round_num}",
                        "agent": supervisor_config.name,
                        "output": route_output,
                        "action": "completed",
                    }
                )
                result.summary = route_decision.get("instruction", route_output)
                break

            worker_name = route_decision.get("worker", "")
            worker_instruction = route_decision.get("instruction", current_task)

            if worker_name not in worker_map:
                logger.warning(
                    "Supervisor chose unknown worker '%s', picking first", worker_name
                )
                worker_name = next(iter(worker_map))

            worker_config = worker_map[worker_name]

            try:
                worker_runtime = AgentRuntime(
                    tool_registry=self.tools,
                    llm_gateway=self.llm_gateway,
                    safety_gateway=self.safety_gateway,
                )
                worker_ctx = AgentContext()
                worker_ctx.metadata["agent_name"] = worker_name
                worker_ctx.metadata["supervisor_round"] = round_num

                worker_events = []
                async for event in worker_runtime.execute_graph(
                    worker_config.graph, worker_instruction, worker_ctx
                ):
                    worker_events.append(event)

                worker_output = self._extract_final_output(worker_events, worker_ctx)

                routing_history.append(
                    {
                        "worker": worker_name,
                        "instruction": worker_instruction,
                        "output": worker_output[:200],
                        "status": "completed",
                    }
                )

                result.results.append(
                    {
                        "phase": f"round_{round_num}",
                        "agent": worker_name,
                        "output": worker_output,
                        "events": [e.to_dict() for e in worker_events],
                        "usage": worker_ctx.token_usage(),
                        "duration": worker_ctx.elapsed_seconds(),
                        "routed_by": supervisor_config.name,
                    }
                )

                current_task = worker_output

            except Exception as e:
                logger.exception(
                    "Worker %s failed in supervisor round %d", worker_name, round_num
                )
                routing_history.append(
                    {
                        "worker": worker_name,
                        "instruction": worker_instruction,
                        "output": "",
                        "status": f"error: {e}",
                    }
                )
                result.errors.append(f"{worker_name} (round {round_num}): {e}")

        else:
            result.results.append(
                {
                    "phase": "supervisor_timeout",
                    "agent": supervisor_config.name,
                    "output": "Max routing rounds reached",
                    "action": "timeout",
                }
            )

        result.total_duration = time.time() - start
        return result

    def _format_handoff_history(self, chain: list[HandoffContext]) -> str:
        """Format the handoff chain history into a readable string."""
        parts = ["--- Handoff History ---"]
        for h in chain:
            parts.append(f"[{h.sender} -> {h.receiver}]")
            parts.append(h.content[:500])
            parts.append("")
        return "\n".join(parts)

    def _is_handoff_complete(self, content: str) -> bool:
        """Check if agent output indicates task completion (no further handoff)."""
        completion_markers = ["[COMPLETE]", "[DONE]", "[TASK_COMPLETE]", "[NO_HANDOFF]"]
        content_upper = content.upper()
        return any(marker in content_upper for marker in completion_markers)

    def _merge_outputs(self, outputs: dict[str, str], strategy: str) -> str:
        """Merge agent outputs according to the specified strategy."""
        if strategy == "concat":
            parts = []
            for name, output in outputs.items():
                parts.append(f"=== {name} ===\n{output}")
            return "\n\n".join(parts)
        elif strategy == "best":
            if not outputs:
                return ""
            return max(outputs.values(), key=len)
        elif strategy == "json":
            return json.dumps(outputs, ensure_ascii=False, indent=2)
        else:
            logger.warning(
                "Unknown merge strategy '%s', falling back to concat", strategy
            )
            return self._merge_outputs(outputs, "concat")

    def _parse_route_decision(self, output: str) -> dict[str, Any]:
        """Parse supervisor output into a route decision dict."""
        try:
            json_match = output
            if "```" in output:
                lines = output.split("\n")
                json_lines = []
                in_block = False
                for line in lines:
                    if line.strip().startswith("```"):
                        in_block = not in_block
                        continue
                    if in_block:
                        json_lines.append(line)
                json_match = "\n".join(json_lines)
            import re

            brace_match = re.search(r"\{[^{}]*\}", json_match, re.DOTALL)
            if brace_match:
                json_match = brace_match.group()
            parsed = json.loads(json_match)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        logger.warning("Failed to parse route decision from: %s", output[:200])
        return {"worker": "", "instruction": "", "done": False}

    def _extract_final_output(self, events: list[AgentEvent], ctx: AgentContext) -> str:
        """Extract the final output content from agent events."""
        for ev in reversed(events):
            if ev.type == AgentEventType.THINK and ev.content:
                return ev.content
            if ev.type == AgentEventType.END:
                break
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                return msg.get("content", "")
        return ""

    def _extract_sub_tasks(self, ctx: AgentContext, expected_count: int) -> list[str]:
        """Extract sub-task descriptions from master agent output."""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content", "")
                try:
                    tasks = json.loads(content)
                    if isinstance(tasks, list):
                        return [str(t) for t in tasks[:expected_count]]
                except (json.JSONDecodeError, TypeError):
                    lines = [
                        line.strip() for line in content.split("\n") if line.strip()
                    ]
                    return lines[:expected_count]
        return [f"Sub-task {i + 1}" for i in range(expected_count)]

    def set_limits(
        self, max_concurrent: int | None = None, max_depth: int | None = None
    ) -> dict:
        if max_concurrent is not None:
            self._limits["max_concurrent"] = max_concurrent
            self.max_concurrent = max_concurrent
        if max_depth is not None:
            self._limits["max_depth"] = max_depth
        logger.info("Orchestrator limits set: %s", self._limits)
        return dict(self._limits)

    def get_limits(self) -> dict:
        return dict(self._limits)
