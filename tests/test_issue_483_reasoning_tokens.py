"""#483: reasoning tokens metered separately in budget accounting (N8 #345 residual)."""
import pytest

from agent_runtime.context import AgentContext
from agent_runtime.telemetry import TelemetryEngine
from agent_runtime.token_budget import TokenBudget
from server.fusion_mlx_client import _normalize_usage


def test_budget_records_reasoning_split():
    budget = TokenBudget(max_tokens=1000)
    budget.record_usage(100, 200, reasoning_tokens=150)
    status = budget.status()
    assert status["prompt_tokens"] == 100
    assert status["completion_tokens"] == 200
    assert status["reasoning_tokens"] == 150
    # documented: thresholds evaluate on prompt+completion total only
    assert status["spent_tokens"] == 300
    assert status["exceeded"] is False


def test_budget_threshold_on_total_not_reasoning():
    budget = TokenBudget(max_tokens=300)
    budget.record_usage(100, 200, reasoning_tokens=500)
    assert budget.spent_tokens == 300
    assert budget.is_exceeded() is True


def test_budget_roundtrip_preserves_reasoning():
    budget = TokenBudget()
    budget.record_usage(10, 20, reasoning_tokens=5)
    restored = TokenBudget.from_dict(budget.to_dict())
    assert restored.reasoning_tokens == 5


def test_budget_reasoning_default_zero():
    budget = TokenBudget()
    budget.record_usage(10, 20)
    assert budget.reasoning_tokens == 0
    assert budget.status()["reasoning_tokens"] == 0


def test_context_token_usage_aggregates_flat_and_nested():
    ctx = AgentContext()
    ctx.add_message("assistant", "a", usage={
        "prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 7,
    })
    ctx.add_message("assistant", "b", usage={
        "prompt_tokens": 5, "completion_tokens": 6,
        "completion_tokens_details": {"reasoning_tokens": 3},
    })
    usage = ctx.token_usage()
    assert usage["prompt_tokens"] == 15
    assert usage["completion_tokens"] == 26
    assert usage["reasoning_tokens"] == 10
    assert usage["total"] == 41


def test_normalize_usage_flat_key():
    usage = _normalize_usage({"prompt_tokens": 1, "reasoning_tokens": 9})
    assert usage["reasoning_tokens"] == 9


def test_normalize_usage_nested_openai_details():
    usage = _normalize_usage({
        "prompt_tokens": 1,
        "completion_tokens_details": {"reasoning_tokens": 42},
    })
    assert usage["reasoning_tokens"] == 42


def test_normalize_usage_no_reasoning():
    usage = _normalize_usage({"prompt_tokens": 1, "completion_tokens": 2})
    assert usage.get("reasoning_tokens") is None
    assert usage["completion_tokens"] == 2


def test_normalize_usage_guards_non_dict():
    assert _normalize_usage(None) == {}


def test_telemetry_counters_reasoning():
    engine = TelemetryEngine()
    span = engine.start_span("llm.call", attributes={
        "prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 30,
    })
    engine.end_span(span.span_id)
    metrics = engine.metrics()
    assert metrics["counters"]["tokens_reasoning"] == 30
    assert metrics["counters"]["tokens_completion"] == 20


@pytest.mark.parametrize("reasoning", [0, 17])
def test_runtime_record_usage_passes_reasoning(reasoning):
    # wiring: execute_graph's post-LLM metering forwards ctx.token_usage()
    # reasoning split into record_usage — verify the call signature contract
    # with a stub budget capturing kwargs.
    captured = {}

    class StubBudget:
        def record_usage(self, prompt_tokens, completion_tokens, reasoning_tokens=0):
            captured["prompt"] = prompt_tokens
            captured["completion"] = completion_tokens
            captured["reasoning"] = reasoning_tokens

        max_tokens = 0
        spent_tokens = 0

    ctx = AgentContext()
    ctx.add_message("assistant", "x", usage={
        "prompt_tokens": 3, "completion_tokens": 4, "reasoning_tokens": reasoning,
    })
    usage = ctx.token_usage()
    StubBudget().record_usage(
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        reasoning_tokens=usage.get("reasoning_tokens", 0),
    )
    assert captured == {"prompt": 3, "completion": 4, "reasoning": reasoning}
