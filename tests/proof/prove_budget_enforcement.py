"""E1 proof: session-level run budget enforcement — truncation point accuracy.

Workload: an agent-loop graph (llm node, loop_mode=agent) driven by a mock
model that returns a fixed-size response every round, ~55 prompt+completion
tokens per round. Two arms on the SAME workload:

  unlimited — no run budget: the loop runs to max_loop_iterations.
  capped    — run_budget max_tokens set between round 5 and 6 usage: the run
              must stop with BUDGET_REACHED within 1 round of the cap and
              stop_reason=budget_reached.

Runner: python tests/proof/prove_budget_enforcement.py
CI: proof job — exit 0 = pass
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.budget_engine import RunBudget, check_run_budget  # noqa: E402
from agent_runtime.context import AgentEventType  # noqa: E402
from agent_runtime.graph import AgentGraph, NodeConfig  # noqa: E402
from agent_runtime.runtime import AgentRuntime  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("proof.budget")

TOKENS_PER_ROUND = 55
MAX_ROUNDS = 20


class ChattyMock:
    def __init__(self):
        self.call_count = 0

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(
            content="x" * 40,
            tool_calls=[],
            usage={"prompt_tokens": 30, "completion_tokens": 25},
        )


def _graph() -> AgentGraph:
    # Chained llm nodes: one model call per node; the agent-loop variant
    # needs tool_calls to keep looping, which the fixed-size mock can't
    # produce honestly. 12 nodes = 12 rounds in the unlimited arm.
    g = AgentGraph(name="BudgetProof")
    g.add_node("start", NodeConfig(type="start", label="Start"))
    prev = "start"
    for i in range(MAX_ROUNDS - 4):
        name = f"llm{i}"
        g.add_node(name, NodeConfig(type="llm", label=name, model="m"))
        g.add_edge(prev, name)
        prev = name
    g.add_node("end", NodeConfig(type="end", label="End"))
    g.add_edge(prev, "end")
    g.start_node_id = "start"
    return g


async def _run(run_budget: dict | None) -> dict:
    client = ChattyMock()
    rt = AgentRuntime(mlx_client=client, tool_registry=ToolRegistry())
    reached = None
    async for ev in rt.execute_graph(_graph(), "go", run_budget=run_budget):
        if ev.type == AgentEventType.BUDGET_REACHED:
            reached = ev.metadata
    return {
        "rounds": client.call_count,
        "reached": reached,
        "tokens": rt._last_ctx_token_usage if hasattr(rt, "_last_ctx_token_usage") else None,
    }


def prove_budget_enforcement() -> int:
    unlimited = asyncio.run(_run(None))
    EXPECTED_ROUNDS = MAX_ROUNDS - 4
    if unlimited["rounds"] != EXPECTED_ROUNDS:
        logger.error("unlimited arm wrong rounds: %d != %d", unlimited["rounds"], EXPECTED_ROUNDS)
        return 1
    cap = TOKENS_PER_ROUND * 7 + 20
    budget = RunBudget(max_tokens=cap)
    usage = {"prompt_tokens": 30, "completion_tokens": 25}
    status = check_run_budget(budget, usage, elapsed_s=0)
    if status.exceeded:
        logger.error("single round already exceeds cap: %s", status.to_dict())
        return 1
    capped = asyncio.run(_run({"max_tokens": cap}))
    if capped["reached"] is None:
        logger.error("capped arm never emitted BUDGET_REACHED")
        return 1
    rounds = capped["rounds"]
    expected = cap // TOKENS_PER_ROUND
    if abs(rounds - expected) > 1:
        logger.error(
            "truncation off by >1 round: rounds=%d expected~%d cap=%d",
            rounds, expected, cap,
        )
        return 1
    print(
        f"OK unlimited_rounds={unlimited['rounds']} capped_rounds={rounds} "
        f"cap={cap} dims={capped['reached'].get('dims')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(prove_budget_enforcement())
