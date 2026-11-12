"""E1 (#672 plan): session-level budget enforcement — budget as a first-class
run object.

Managed Agents semantics: cost/budget live on the run object, enforcement
stops the run when a dimension is exceeded, and a webhook fires
(run.budget_reached). Dimensions: max_tokens (prompt+completion per run),
max_cost_usd (estimated from token usage via a per-model rate table),
max_wall_s (wall-clock). Enforcement point is the graph loop right after
each llm node, mirroring the TokenBudget stop path so parking (run registry
usage_limit frames) works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Estimated USD cost per 1M tokens: (input, output). Local-first models
# default to 0 — cost is an estimate for gating, not billing. Override with
# FUSION_RUN_COST_RATES='{"model-prefix": [in, out], ...}' (prefix match,
# first hit wins, "default" catches the rest).
DEFAULT_COST_RATES: dict[str, tuple[float, float]] = {
    "default": (0.0, 0.0),
}


def _cost_rates() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("FUSION_RUN_COST_RATES", "")
    if not raw:
        return DEFAULT_COST_RATES
    try:
        parsed = json.loads(raw)
        rates = {}
        for key, val in parsed.items():
            if isinstance(val, (list, tuple)) and len(val) == 2:
                rates[str(key)] = (float(val[0]), float(val[1]))
        return rates or DEFAULT_COST_RATES
    except (ValueError, TypeError) as e:
        logger.warning("FUSION_RUN_COST_RATES parse failed, using defaults: %s", e)
        return DEFAULT_COST_RATES


def estimate_run_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _cost_rates()
    key = "default"
    for prefix in rates:
        if prefix != "default" and model and model.startswith(prefix):
            key = prefix
            break
    in_rate, out_rate = rates[key]
    return (prompt_tokens * in_rate + completion_tokens * out_rate) / 1_000_000.0


@dataclass
class RunBudget:
    # All dims optional; 0/None = unlimited. At least one must be set for
    # enforcement to be active.
    max_tokens: int = 0
    max_cost_usd: float = 0.0
    max_wall_s: float = 0.0

    def active(self) -> bool:
        return self.max_tokens > 0 or self.max_cost_usd > 0 or self.max_wall_s > 0

    def to_dict(self) -> dict:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_wall_s": self.max_wall_s,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "RunBudget":
        if not isinstance(data, dict):
            return cls()
        try:
            return cls(
                max_tokens=int(data.get("max_tokens", 0) or 0),
                max_cost_usd=float(data.get("max_cost_usd", 0.0) or 0.0),
                max_wall_s=float(data.get("max_wall_s", 0.0) or 0.0),
            )
        except (TypeError, ValueError) as e:
            logger.warning("RunBudget.from_dict invalid values, ignoring: %s", e)
            return cls()


@dataclass
class RunBudgetStatus:
    exceeded: bool = False
    dims: list[str] = field(default_factory=list)
    spent_tokens: int = 0
    spent_cost_usd: float = 0.0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "exceeded": self.exceeded,
            "dims": list(self.dims),
            "spent_tokens": self.spent_tokens,
            "spent_cost_usd": round(self.spent_cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 3),
        }


def check_run_budget(
    budget: RunBudget,
    usage: dict,
    elapsed_s: float,
    model: str = "",
) -> RunBudgetStatus:
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    reasoning = int(usage.get("reasoning_tokens", 0) or 0)
    # reasoning tokens are billed as completion on serving engines.
    total_tokens = prompt + completion + reasoning
    cost = estimate_run_cost(model, prompt, completion + reasoning)
    dims: list[str] = []
    if budget.max_tokens > 0 and total_tokens >= budget.max_tokens:
        dims.append("max_tokens")
    if budget.max_cost_usd > 0 and cost >= budget.max_cost_usd:
        dims.append("max_cost_usd")
    if budget.max_wall_s > 0 and elapsed_s >= budget.max_wall_s:
        dims.append("max_wall_s")
    return RunBudgetStatus(
        exceeded=bool(dims),
        dims=dims,
        spent_tokens=total_tokens,
        spent_cost_usd=cost,
        elapsed_s=elapsed_s,
    )


class RunBudgetTracker:
    # Per-run wall-clock anchor + last status. Lives on the runtime for the
    # duration of one execute_graph call; not shared across concurrent runs
    # (ctx.run_budget carries the budget, tracker rides the same ctx).
    def __init__(self, budget: RunBudget):
        self.budget = budget
        self.started_at = time.time()
        self.last_status: RunBudgetStatus | None = None

    def check(self, usage: dict, model: str = "") -> RunBudgetStatus:
        status = check_run_budget(
            self.budget, usage, time.time() - self.started_at, model=model
        )
        self.last_status = status
        return status
