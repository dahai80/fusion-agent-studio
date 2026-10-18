from __future__ import annotations

import time

import pytest

import agent_runtime.hooks as hooks_mod
from agent_runtime.hooks import HookConfig, HookEngine, HookEvent, HookResult, hook


@pytest.fixture
def dec_guard():
    saved = list(hooks_mod._DECORATOR_REGISTRY)
    yield
    hooks_mod._DECORATOR_REGISTRY[:] = saved


def test_hook_event_26_members_and_new_names():
    members = list(HookEvent)
    assert len(members) == 26
    names = {m.value for m in members}
    for expected in (
        "SESSION_START",
        "SESSION_END",
        "STOP",
        "PRE_COMPACT",
        "SUBAGENT_START",
        "SUBAGENT_STOP",
        "USER_PROMPT_SUBMIT",
        "GRAPH_BEFORE",
        "GRAPH_AFTER",
        "NODE_BEFORE",
        "NODE_AFTER",
        "TOOL_DENY",
        "LLM_BEFORE",
        "LLM_AFTER",
        "APPROVAL_REQUEST",
        "APPROVAL_RESOLVE",
        "COMPACT_BEFORE",
        "COMPACT_AFTER",
        "BUDGET_EXCEED",
        "PRE_TOOL_USE",
        "POST_TOOL_USE",
        "POST_TOOL_USE_FAILURE",
        "ERROR",
        "CHECKPOINT",
        "TEAM_MESSAGE",
        "PERSONA_RELOADED",
    ):
        assert expected in names, f"missing hook event {expected}"


def test_hook_event_str_enum_identity():
    for m in HookEvent:
        assert isinstance(m, str)
        assert m.value == m.name


async def test_decorator_registers_into_engine(dec_guard):
    calls = []

    @hook("PRE_TOOL_USE", matcher="write*")
    def _deco_block(payload):
        calls.append(payload.get("tool_name"))
        return {"decision": "block", "reason": "deco-block"}

    eng = HookEngine(auto_register_decorators=False)
    added = eng.flush_decorator_registry()
    assert added == 1
    registered = eng._hooks["PRE_TOOL_USE"]
    assert len(registered) == 1
    assert registered[0].matcher == "write*"
    assert eng._decorator_consumed == 1

    res = await eng.fire("PRE_TOOL_USE", {"tool_name": "write_file"}, tool_name="write_file")
    assert res.decision == "block" and res.reason == "deco-block"
    assert calls == ["write_file"]

    res2 = await eng.fire("PRE_TOOL_USE", {}, tool_name="file_read")
    assert res2.decision == "approve"
    assert calls == ["write_file"]


def test_decorator_flush_twice_no_duplicates(dec_guard):
    @hook("SESSION_START", matcher="*")
    def _deco(payload):
        return {}

    eng = HookEngine(auto_register_decorators=False)
    assert eng.flush_decorator_registry() == 1
    assert eng.flush_decorator_registry() == 0
    assert len(eng._hooks["SESSION_START"]) == 1


def test_decorator_flush_to_second_engine_first_unaffected(dec_guard):
    @hook("PRE_TOOL_USE", matcher="*")
    def _deco(payload):
        return {"decision": "approve"}

    eng1 = HookEngine(auto_register_decorators=False)
    assert eng1.flush_decorator_registry() == 1
    assert len(eng1._hooks["PRE_TOOL_USE"]) == 1

    eng2 = HookEngine(auto_register_decorators=True)
    assert eng2._decorator_consumed == 1
    assert len(eng2._hooks["PRE_TOOL_USE"]) == 1

    assert eng2.flush_decorator_registry() == 0
    assert len(eng1._hooks["PRE_TOOL_USE"]) == 1
    assert len(eng2._hooks["PRE_TOOL_USE"]) == 1


async def test_max_fire_ms_budget_skips_slow_tail():
    eng = HookEngine(auto_register_decorators=False, max_fire_ms=50)
    calls = []

    def slow_first(payload):
        time.sleep(0.2)
        calls.append("first")
        return {"decision": "block", "reason": "first"}

    def second(payload):
        calls.append("second")
        return {"decision": "approve"}

    eng.register(HookConfig(event="PRE_TOOL_USE", callback=slow_first))
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=second))
    start = time.monotonic()
    res = await eng.fire("PRE_TOOL_USE", {}, tool_name="x")
    elapsed = time.monotonic() - start
    assert calls == ["first"]
    assert res.decision == "block"
    assert elapsed < 1.0


def test_hook_max_ms_env_and_override(monkeypatch):
    monkeypatch.delenv("FUSION_HOOK_MAX_MS", raising=False)
    assert HookEngine(auto_register_decorators=False).max_fire_ms == 50.0
    monkeypatch.setenv("FUSION_HOOK_MAX_MS", "1234")
    assert HookEngine(auto_register_decorators=False).max_fire_ms == 1234.0
    assert HookEngine(auto_register_decorators=False, max_fire_ms=7).max_fire_ms == 7


async def test_hook_raise_fail_open():
    eng = HookEngine(auto_register_decorators=False)
    seen = []

    def bad(payload):
        raise RuntimeError("boom")

    def good(payload):
        seen.append("good")
        return {"decision": "approve"}

    eng.register(HookConfig(event="PRE_TOOL_USE", callback=bad))
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=good))
    res = await eng.fire("PRE_TOOL_USE", {}, tool_name="x")
    assert res.decision == "approve"
    assert seen == ["good"]


async def test_no_matched_hooks_returns_default_approve():
    eng = HookEngine(auto_register_decorators=False)
    res = await eng.fire("NODE_BEFORE", {}, tool_name="")
    assert isinstance(res, HookResult)
    assert res.decision == "approve" and res.continue_loop is True


async def test_continue_loop_false_aggregates():
    eng = HookEngine(auto_register_decorators=False)
    eng.register(HookConfig(event="STOP", callback=lambda p: {"continue_loop": False}))
    res = await eng.fire("STOP", {})
    assert res.continue_loop is False
    assert res.decision == "approve"
