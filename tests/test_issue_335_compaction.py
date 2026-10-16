from __future__ import annotations

import logging

from agent_runtime.compactor import (
    CompactionConfig,
    Compactor,
    RetentionList,
)
from agent_runtime.funnels import LifecycleBus, LLMFunnel

logger = logging.getLogger(__name__)


class _Ctx:
    def __init__(self, session_id="cs0", plan_mode=False):
        self.session_id = session_id
        self.plan_mode = plan_mode


def _small_compactor(**overrides):
    params = dict(
        context_window=200,
        warning_buffer=100,
        error_buffer=50,
        manual_buffer=10,
        keep_recent_rounds=1,
    )
    params.update(overrides)
    return Compactor(CompactionConfig(**params))


def _big_messages():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "U" * 400},
        {"role": "assistant", "content": "A" * 400},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "final"},
    ]


def test_estimate_tokens_counts_tool_calls():
    c = Compactor()
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "my_tool", "arguments": '{"a": "b"}'}}
        ]},
    ]
    est = c.estimate_tokens(msgs)
    assert est == max(1, (len("my_tool") + len('{"a": "b"}')) // 4)


def test_should_compact_level_matrix():
    c = Compactor(
        CompactionConfig(
            context_window=200,
            warning_buffer=100,
            error_buffer=40,
            manual_buffer=150,
            keep_recent_rounds=1,
        )
    )
    assert c.should_compact([{"role": "user", "content": "x"}]) == "none"
    m_manual = [{"role": "user", "content": "a" * 200}]
    assert c.should_compact(m_manual) == "manual"
    m_warn = [{"role": "user", "content": "a" * 400}]
    assert c.should_compact(m_warn) == "warning"
    m_err = [{"role": "user", "content": "a" * 1000}]
    assert c.should_compact(m_err) == "error"


def test_retention_list_extracts_sections():
    msgs = [
        {"role": "user", "content": "TODO: fix the bug\nconstraint: keep it fast\npending task in_progress"},
        {"role": "user", "content": "def helper():\nclass Foo:"},
    ]
    rl = RetentionList.from_messages(msgs)
    assert rl.todos and rl.constraints and rl.active_tasks and rl.interfaces
    assert rl.non_empty() is True
    rendered = rl.render()
    assert "[Retention List]" in rendered
    assert "TODOs:" in rendered and "Interfaces:" in rendered


def test_retention_dedup_and_cap():
    msgs = [
        {"role": "user", "content": "TODO: a\nTODO: a\nTODO: b"},
    ]
    rl = RetentionList.from_messages(msgs, max_per_section=1)
    assert rl.todos == ["TODO: a"]
    assert rl.non_empty() is True


def test_retention_empty_when_no_markers():
    rl = RetentionList.from_messages([{"role": "user", "content": "plain text"}])
    assert rl.non_empty() is False


def test_compact_idempotent_no_duplicate_retention_header():
    c = _small_compactor()
    msgs = _big_messages()
    once = c.compact(msgs, level="error")
    twice = c.compact(once, level="error")
    headers = [m for m in twice if m.get("role") == "system" and str(m.get("content", "")).startswith("[Retention List]")]
    assert len(headers) <= 1


async def test_llm_funnel_compaction_changes_messages_and_emits():
    bus = LifecycleBus()
    funnel = LLMFunnel(compactor=_small_compactor(), lifecycle_bus=bus)
    msgs = _big_messages()
    proceed, reason, out = await funnel.pre_llm(_Ctx(session_id="cs1"), messages=msgs)
    assert proceed is True
    assert out is not msgs
    assert any("[Compacted prior context]" in m.get("content", "") for m in out if m.get("role") == "system")
    assert out[-1]["content"] == "final"
    events = [e.event for e in bus.replay(session_id="cs1", domain="agent")]
    assert "compact_before" in events and "compact_after" in events


async def test_llm_funnel_compactor_raise_fail_open():
    class _BadCompactor:
        def should_compact(self, messages):
            return "warning"

        def compact(self, messages, level):
            raise RuntimeError("compact kaput")

    bus = LifecycleBus()
    funnel = LLMFunnel(compactor=_BadCompactor(), lifecycle_bus=bus)
    msgs = [{"role": "user", "content": "hello"}]
    proceed, reason, out = await funnel.pre_llm(_Ctx(session_id="cs2"), messages=msgs)
    assert proceed is True and reason == ""
    assert out == msgs
    events = [e.event for e in bus.replay(session_id="cs2", domain="agent")]
    assert "compact_before" in events and "compact_after" in events


async def test_llm_funnel_should_compact_raise_fail_open():
    class _BadShould:
        def should_compact(self, messages):
            raise RuntimeError("should kaput")

    bus = LifecycleBus()
    funnel = LLMFunnel(compactor=_BadShould(), lifecycle_bus=bus)
    msgs = [{"role": "user", "content": "hello"}]
    proceed, reason, out = await funnel.pre_llm(_Ctx(session_id="cs3"), messages=msgs)
    assert proceed is True and reason == ""
    assert out == msgs
    events = [e.event for e in bus.replay(session_id="cs3", domain="agent")]
    assert events == ["llm_before"]


def test_reactive_strip_keeps_system_and_tail():
    c = Compactor()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "keep1"},
        {"role": "assistant", "content": "keep2"},
    ]
    out = c.reactive_strip(msgs)
    assert out[0]["content"] == "sys"
    assert out[-1]["content"] == "keep2"
    assert any("Compacted" in m.get("content", "") for m in out)


def test_hard_compact_keeps_last_round_only():
    c = Compactor(
        CompactionConfig(
            context_window=200,
            warning_buffer=100,
            error_buffer=40,
            manual_buffer=10,
            keep_recent_rounds=3,
        )
    )
    out = c.compact(_big_messages(), level="error")
    assert out[-1]["content"] == "final"
    assert any("hard-compact" in m.get("content", "") for m in out if m.get("role") == "system")


def test_microcompact_truncates_tool_role():
    c = Compactor(CompactionConfig(tool_result_head=10, tool_result_tail=10))
    out = c._microcompact([{"role": "tool", "content": "T" * 300}])
    assert "truncated" in out[0]["content"]
    assert len(out[0]["content"]) < 300

