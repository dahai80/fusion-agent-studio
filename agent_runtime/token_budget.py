"""Token budget — session-level token spending limit and cost tracking."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "default": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
}


@dataclass
class TokenBudget:
    max_tokens: int = 0
    spent_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    pricing: dict[str, dict[str, float]] = field(
        default_factory=lambda: dict(_DEFAULT_PRICING)
    )
    # N8: day-level and graph-level caps are independent of the run cap.
    day_max_tokens: int = 0
    day_spent_tokens: int = 0
    day_key: str = ""
    graph_max_tokens: int = 0
    graph_spent_tokens: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "spent_tokens": self.spent_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "pricing": self.pricing,
            "day_max_tokens": self.day_max_tokens,
            "day_spent_tokens": self.day_spent_tokens,
            "day_key": self.day_key,
            "graph_max_tokens": self.graph_max_tokens,
            "graph_spent_tokens": dict(self.graph_spent_tokens),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenBudget:
        return cls(
            max_tokens=data.get("max_tokens", 0),
            spent_tokens=data.get("spent_tokens", 0),
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            pricing=data.get("pricing", dict(_DEFAULT_PRICING)),
            day_max_tokens=data.get("day_max_tokens", 0),
            day_spent_tokens=data.get("day_spent_tokens", 0),
            day_key=data.get("day_key", ""),
            graph_max_tokens=data.get("graph_max_tokens", 0),
            graph_spent_tokens=dict(data.get("graph_spent_tokens") or {}),
        )

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._today()
        if self.day_key != today:
            if self.day_key:
                logger.info(
                    "token budget day rollover %s -> %s (spent=%d)",
                    self.day_key,
                    today,
                    self.day_spent_tokens,
                )
            self.day_key = today
            self.day_spent_tokens = 0

    def record_usage(
        self, prompt_tokens: int, completion_tokens: int, graph_id: str = ""
    ) -> None:
        total = int(prompt_tokens or 0) + int(completion_tokens or 0)
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.spent_tokens = self.prompt_tokens + self.completion_tokens
        self._roll_day()
        self.day_spent_tokens += total
        if graph_id:
            self.graph_spent_tokens[graph_id] = (
                self.graph_spent_tokens.get(graph_id, 0) + total
            )
        logger.debug(
            "Token usage recorded: prompt=%d completion=%d total=%d day=%d graph=%s",
            prompt_tokens,
            completion_tokens,
            self.spent_tokens,
            self.day_spent_tokens,
            graph_id or "-",
        )

    def is_exceeded(self, graph_id: str = "") -> bool:
        self._roll_day()
        if self.max_tokens > 0 and self.spent_tokens >= self.max_tokens:
            return True
        if self.day_max_tokens > 0 and self.day_spent_tokens >= self.day_max_tokens:
            return True
        if (
            graph_id
            and self.graph_max_tokens > 0
            and self.graph_spent_tokens.get(graph_id, 0) >= self.graph_max_tokens
        ):
            return True
        return False

    def remaining(self, graph_id: str = "") -> int:
        caps = []
        if self.max_tokens > 0:
            caps.append(self.max_tokens - self.spent_tokens)
        if self.day_max_tokens > 0:
            self._roll_day()
            caps.append(self.day_max_tokens - self.day_spent_tokens)
        if graph_id and self.graph_max_tokens > 0:
            caps.append(
                self.graph_max_tokens - self.graph_spent_tokens.get(graph_id, 0)
            )
        if not caps:
            return -1
        return max(0, min(caps))

    def estimate_cost(self, model: str = "default") -> float:
        rates = self.pricing.get(
            model, self.pricing.get("default", _DEFAULT_PRICING["default"])
        )
        prompt_cost = (self.prompt_tokens / 1000.0) * rates.get("prompt_per_1k", 0.0)
        completion_cost = (self.completion_tokens / 1000.0) * rates.get(
            "completion_per_1k", 0.0
        )
        return prompt_cost + completion_cost

    def status(self, model: str = "default", graph_id: str = "") -> dict[str, Any]:
        self._roll_day()
        return {
            "max_tokens": self.max_tokens,
            "spent_tokens": self.spent_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "remaining": self.remaining(graph_id),
            "exceeded": self.is_exceeded(graph_id),
            "estimated_cost": self.estimate_cost(model),
            "day_max_tokens": self.day_max_tokens,
            "day_spent_tokens": self.day_spent_tokens,
            "day_key": self.day_key,
            "graph_max_tokens": self.graph_max_tokens,
            "graph_spent_tokens": dict(self.graph_spent_tokens),
        }
