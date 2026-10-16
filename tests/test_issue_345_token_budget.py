"""N8 token budget - LLMFunnel budget gate + metering (issue #345)."""

from agent_runtime.funnels import LifecycleBus, LLMFunnel
from agent_runtime.token_budget import TokenBudget


def _budget(max_tokens: int = 100) -> TokenBudget:
    b = TokenBudget(max_tokens=max_tokens)
    b.record_usage(60, 50)
    return b


async def test_pre_llm_budget_gate_blocks_when_exceeded():
    bus = LifecycleBus()
    f = LLMFunnel(lifecycle_bus=bus)
    b = _budget(100)
    f.set_budget_ref(b)
    ok, reason, msgs = await f.pre_llm({}, node_label="n", messages=[{"role": "user", "content": "hi"}])
    assert ok is False
    assert "budget" in reason
    events = bus.replay(domain="agent")
    assert any(e.event == "budget_exceed" for e in events)
    assert b.spent_tokens == 110


async def test_pre_llm_passes_when_under_budget():
    f = LLMFunnel()
    b = _budget(10000)
    f.set_budget_ref(b)
    ok, reason, msgs = await f.pre_llm({}, messages=[{"role": "user", "content": "hi"}])
    assert ok is True
    assert reason == ""
    assert msgs == [{"role": "user", "content": "hi"}]


async def test_pre_llm_no_budget_passthrough():
    f = LLMFunnel()
    ok, reason, _ = await f.pre_llm({}, messages=[])
    assert ok is True


async def test_post_llm_record_usage_into_budget():
    f = LLMFunnel()
    b = TokenBudget(max_tokens=1000)
    f.set_budget_ref(b)
    f.post_llm({}, usage={"prompt_tokens": 12, "completion_tokens": 34})
    assert b.spent_tokens == 46


async def test_post_llm_no_budget_no_crash():
    f = LLMFunnel()
    f.post_llm({}, usage={"prompt_tokens": 1, "completion_tokens": 1})


async def test_budget_gate_does_not_mutate_messages():
    f = LLMFunnel()
    b = _budget(10)
    f.set_budget_ref(b)
    msgs = [{"role": "user", "content": "hi"}]
    ok, reason, out = await f.pre_llm({}, messages=msgs)
    assert ok is False
    assert out is msgs
