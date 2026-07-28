"""Token budget — session-level token spending limit and cost tracking."""

from __future__ import annotations

import logging
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
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: dict(_DEFAULT_PRICING))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "spent_tokens": self.spent_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "pricing": self.pricing,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenBudget:
        return cls(
            max_tokens=data.get("max_tokens", 0),
            spent_tokens=data.get("spent_tokens", 0),
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            pricing=data.get("pricing", dict(_DEFAULT_PRICING)),
        )

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.spent_tokens = self.prompt_tokens + self.completion_tokens
        logger.debug(
            "Token usage recorded: prompt=%d completion=%d total=%d",
            prompt_tokens, completion_tokens, self.spent_tokens,
        )

    def is_exceeded(self) -> bool:
        if self.max_tokens <= 0:
            return False
        return self.spent_tokens >= self.max_tokens

    def remaining(self) -> int:
        if self.max_tokens <= 0:
            return -1
        return max(0, self.max_tokens - self.spent_tokens)

    def estimate_cost(self, model: str = "default") -> float:
        rates = self.pricing.get(model, self.pricing.get("default", _DEFAULT_PRICING["default"]))
        prompt_cost = (self.prompt_tokens / 1000.0) * rates.get("prompt_per_1k", 0.0)
        completion_cost = (self.completion_tokens / 1000.0) * rates.get("completion_per_1k", 0.0)
        return prompt_cost + completion_cost

    def status(self, model: str = "default") -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "spent_tokens": self.spent_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "remaining": self.remaining(),
            "exceeded": self.is_exceeded(),
            "estimated_cost": self.estimate_cost(model),
        }
