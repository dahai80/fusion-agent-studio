"""Multi-agent orchestrator — coordinates parallel and sequential agent execution."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph

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
        mlx_client: "FusionMLXClient",
        tool_registry: "ToolRegistry",
    ):
        self.mlx = mlx_client
        self.tools = tool_registry

    async def sequential(
        self,
        agents: list[AgentConfig],
        initial_input: str,
    ) -> OrchestrationResult:
        """Execute agents sequentially — each agent's output feeds the next.

        Args:
            agents: List of agent configurations to run in order.
            initial_input: The initial input for the first agent.

        Returns:
            OrchestrationResult with all agent outputs.
        """
        result = OrchestrationResult()
        import time
        start = time.time()
        current_input = initial_input

        for agent_config in agents:
            try:
                from .runtime import AgentRuntime
                runtime = AgentRuntime(self.mlx, self.tools)

                ctx = AgentContext()
                ctx.metadata["agent_name"] = agent_config.name

                events = []
                async for event in runtime.execute_graph(agent_config.graph, current_input, ctx):
                    events.append(event)

                # Collect final output
                final_content = ""
                for ev in reversed(events):
                    if ev.type == AgentEventType.RESULT:
                        final_content = ev.content
                        break
                    if ev.type == AgentEventType.END:
                        break

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
    ) -> OrchestrationResult:
        """Execute agents in parallel — all agents run simultaneously.

        Args:
            agents: List of agent configurations to run in parallel.
            input_template: Optional input template. Each agent receives
                the same input.

        Returns:
            OrchestrationResult with all agent outputs.
        """
        result = OrchestrationResult()
        import time
        start = time.time()
        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent agents

        async def run_single(agent_config: AgentConfig) -> dict[str, Any]:
            async with semaphore:
                try:
                    from .runtime import AgentRuntime
                    runtime = AgentRuntime(self.mlx, self.tools)

                    ctx = AgentContext()
                    ctx.metadata["agent_name"] = agent_config.name

                    events = []
                    async for event in runtime.execute_graph(agent_config.graph, input_template, ctx):
                        events.append(event)

                    final_content = ""
                    for ev in reversed(events):
                        if ev.type == AgentEventType.RESULT:
                            final_content = ev.content
                            break
                        if ev.type == AgentEventType.END:
                            break

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
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total": 0},
                        "duration": 0.0,
                        "error": str(e),
                    }

        tasks = [run_single(agent) for agent in agents]
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
        """Master agent decomposes a task, workers execute sub-tasks, master summarizes.

        Args:
            master: The master agent that decomposes and summarizes.
            workers: Worker agents that execute sub-tasks.
            task: The overall task description.

        Returns:
            OrchestrationResult with master's decomposition, worker results, and summary.
        """
        result = OrchestrationResult()
        import time
        start = time.time()

        # 1. Master decomposes the task
        from .runtime import AgentRuntime
        master_runtime = AgentRuntime(self.mlx, self.tools)
        master_ctx = AgentContext()

        decompose_prompt = (
            f"Decompose the following task into {len(workers)} sub-tasks, "
            f"one for each worker. Return a JSON array of sub-task descriptions:\n\n{task}"
        )

        master_events = []
        async for event in master_runtime.execute_graph(master.graph, decompose_prompt, master_ctx):
            master_events.append(event)

        # Extract sub-tasks from master output
        sub_tasks = self._extract_sub_tasks(master_ctx, len(workers))

        # 2. Workers execute sub-tasks in parallel
        worker_results = await self.parallel(
            workers, input_template="",
        )

        # 3. Master summarizes
        summary_prompt = (
            f"Original task: {task}\n\n"
            f"Worker results:\n{json.dumps(worker_results.results, indent=2, ensure_ascii=False)}\n\n"
            "Provide a comprehensive summary of the results."
        )

        summary_ctx = AgentContext()
        summary_events = []
        async for event in master_runtime.execute_graph(master.graph, summary_prompt, summary_ctx):
            summary_events.append(event)

        # Collect summary
        summary = ""
        for ev in reversed(summary_events):
            if ev.type == AgentEventType.RESULT:
                summary = ev.content
                break
            if ev.type == AgentEventType.END:
                break

        result.results = [
            {
                "phase": "decomposition",
                "output": [e.to_dict() for e in master_events],
                "sub_tasks": sub_tasks,
            },
            *worker_results.results,
            {
                "phase": "summary",
                "output": summary,
                "events": [e.to_dict() for e in summary_events],
            },
        ]
        result.summary = summary
        result.total_duration = time.time() - start
        return result

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
                    # Fallback: split by lines
                    lines = [l.strip() for l in content.split("\n") if l.strip()]
                    return lines[:expected_count]
        # Fallback: generate generic sub-tasks
        return [f"Sub-task {i+1}" for i in range(expected_count)]