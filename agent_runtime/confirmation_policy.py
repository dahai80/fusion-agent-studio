"""#499: Confirmation policy tiers — always / never / risk-threshold.

OpenHands-parity policy object deciding, per action, whether a pending
action must wait for human confirmation before executing. Layered on top
of the existing safety-gateway approval flow (NOT a replacement): the
safety gateway still hard-blocks deny-level content; the policy only
decides the confirmation step for actions that would otherwise ask (or
would otherwise auto-run under an always-confirm graph).

fusion-harness stays untouched (read-only upstream): the tier object
lives here and wraps the approval decision at the single chokepoint
(_await_safety_approval).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TIERS = ("always", "never", "risk")

# Risk classification (deterministic, code-decided — never LLM-judged).
# Score bands: 0.0 readonly, 0.3 local write, 0.6 network, 0.9 destructive.
_DESTRUCTIVE_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bdrop\s+(table|database)\b",
    r"\btruncate\s+table\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bgit\s+push\s+--force\b",
    r"\bshutdown\b",
    r"\bkill\s+-9\b",
)
_NETWORK_HINTS = ("http://", "https://", "ftp://", "ws://")
_WRITE_TOOLS_HINTS = ("write", "create", "update", "delete", "remove", "exec", "terminal", "shell")


@dataclass
class ConfirmationPolicy:
    tier: str = "risk"
    risk_threshold: float = 0.5
    tool_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.tier not in TIERS:
            raise ValueError(f"confirmation policy tier must be one of {TIERS}, got {self.tier!r}")
        for tool, t in self.tool_overrides.items():
            if t not in TIERS:
                raise ValueError(f"tool override for {tool!r} must be one of {TIERS}, got {t!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "risk_threshold": self.risk_threshold,
            "tool_overrides": dict(self.tool_overrides),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ConfirmationPolicy | None:
        if not data:
            return None
        if isinstance(data, ConfirmationPolicy):
            return data
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                tier=str(data.get("tier", "risk")),
                risk_threshold=float(data.get("risk_threshold", 0.5)),
                tool_overrides={
                    str(k): str(v) for k, v in (data.get("tool_overrides") or {}).items()
                },
            )
        except (TypeError, ValueError) as e:
            logger.warning("confirmation policy parse failed: %s", e)
            return None

    def effective_tier(self, tool_name: str = "") -> str:
        if tool_name and tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        return self.tier

    def decide(
        self,
        requires_approval: bool,
        risk_score: float = 0.0,
        tool_name: str = "",
    ) -> tuple[str, str, float]:
        # -> ("confirm" | "auto", effective_tier, risk_score)
        tier = self.effective_tier(tool_name)
        if tier == "always":
            return "confirm", tier, risk_score
        if tier == "never":
            return "auto", tier, risk_score
        # risk tier: confirm only at/above the threshold, regardless of
        # what the safety gateway would have asked — low risk auto-runs,
        # high risk confirms even when the gateway would have auto-passed.
        if risk_score >= self.risk_threshold:
            return "confirm", tier, risk_score
        return "auto", tier, risk_score


def score_tool_risk(
    tool_name: str, params: dict[str, Any] | None = None, readonly: bool | None = None
) -> float:
    # Deterministic band scorer: destructive command text > network >
    # write-classified tool > readonly. Text scan covers params (command,
    # url, content, path) so e.g. a readonly-looking tool carrying
    # "rm -rf" still scores destructive.
    params = params or {}
    text = " ".join(str(v) for v in params.values() if isinstance(v, (str, int, float)))
    lowered = f"{tool_name} {text}".lower()
    for pat in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, lowered):
            return 0.9
    for hint in _NETWORK_HINTS:
        if hint in text:
            return 0.6
    if readonly is True:
        return 0.0
    for hint in _WRITE_TOOLS_HINTS:
        if hint in tool_name.lower():
            return 0.3
    if readonly is False:
        return 0.3
    return 0.0


def policy_from_ctx(ctx) -> ConfirmationPolicy | None:
    # Graph-level policy is stashed into ctx.metadata at dispatch entry
    # (mirrors graph.plan_mode handling) so per-exec, concurrency-safe.
    raw = (getattr(ctx, "metadata", None) or {}).get("confirmation_policy")
    return ConfirmationPolicy.from_dict(raw)
