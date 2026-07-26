"""Tests for ConditionEngine — condition expression evaluation."""

import pytest
from agent_runtime.runtime import ConditionEngine
from agent_runtime.context import AgentContext
from agent_runtime.variable_manager import VariableManager


@pytest.fixture
def engine():
    return ConditionEngine()


@pytest.fixture
def ctx():
    return AgentContext()


@pytest.fixture
def variables():
    v = VariableManager()
    v.set("score", 85)
    v.set("name", "test-agent")
    v.set("mode", "production")
    return v


class TestConditionEngineLiterals:
    def test_true_literal(self, engine, ctx, variables):
        assert engine.evaluate("true", ctx, variables) == "true"

    def test_false_literal(self, engine, ctx, variables):
        assert engine.evaluate("false", ctx, variables) == "false"

    def test_true_case_insensitive(self, engine, ctx, variables):
        assert engine.evaluate("True", ctx, variables) == "true"
        assert engine.evaluate("TRUE", ctx, variables) == "true"

    def test_empty_expression(self, engine, ctx, variables):
        assert engine.evaluate("", ctx, variables) == "false"

    def test_whitespace_only(self, engine, ctx, variables):
        assert engine.evaluate("   ", ctx, variables) == "false"


class TestConditionEngineContextChecks:
    def test_has_tool_calls_true(self, engine, variables):
        ctx = AgentContext()
        ctx.add_message("assistant", "", tool_calls=[{"id": "tc1", "function": {"name": "test", "arguments": "{}"}}])
        assert engine.evaluate("has_tool_calls", ctx, variables) == "true"

    def test_has_tool_calls_false(self, engine, ctx, variables):
        assert engine.evaluate("has_tool_calls", ctx, variables) == "false"

    def test_has_error_true(self, engine, variables):
        ctx = AgentContext()
        ctx.error = "something went wrong"
        assert engine.evaluate("has_error", ctx, variables) == "true"

    def test_has_error_false(self, engine, ctx, variables):
        assert engine.evaluate("has_error", ctx, variables) == "false"

    def test_has_result_true(self, engine, variables):
        ctx = AgentContext()
        ctx.add_message("tool", "result content", tool_call_id="tc1")
        assert engine.evaluate("has_result", ctx, variables) == "true"

    def test_has_result_false(self, engine, ctx, variables):
        assert engine.evaluate("has_result", ctx, variables) == "false"


class TestConditionEngineComparisons:
    def test_iteration_gte(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 5
        assert engine.evaluate("iteration >= 3", ctx, variables) == "true"
        assert engine.evaluate("iteration >= 5", ctx, variables) == "true"
        assert engine.evaluate("iteration >= 6", ctx, variables) == "false"

    def test_iteration_lt(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 3
        assert engine.evaluate("iteration < 5", ctx, variables) == "true"
        assert engine.evaluate("iteration < 3", ctx, variables) == "false"

    def test_message_count_eq(self, engine, variables):
        ctx = AgentContext()
        ctx.add_message("user", "hello")
        ctx.add_message("assistant", "hi")
        assert engine.evaluate("message_count == 2", ctx, variables) == "true"

    def test_variable_comparison(self, engine, variables):
        ctx = AgentContext()
        assert engine.evaluate("score >= 80", ctx, variables) == "true"
        assert engine.evaluate("score < 80", ctx, variables) == "false"

    def test_string_equality(self, engine, variables):
        ctx = AgentContext()
        assert engine.evaluate('mode == "production"', ctx, variables) == "true"
        assert engine.evaluate('mode == "development"', ctx, variables) == "false"


class TestConditionEngineLogical:
    def test_or_true(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 10
        assert engine.evaluate("iteration >= 5 or has_error", ctx, variables) == "true"

    def test_or_both_false(self, engine, ctx, variables):
        assert engine.evaluate("has_tool_calls or has_error", ctx, variables) == "false"

    def test_and_true(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 5
        assert engine.evaluate("iteration >= 3 and iteration < 10", ctx, variables) == "true"

    def test_and_one_false(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 5
        assert engine.evaluate("iteration >= 3 and has_error", ctx, variables) == "true" == "false" or engine.evaluate("iteration >= 3 and has_error", ctx, variables) == "false"

    def test_not(self, engine, ctx, variables):
        assert engine.evaluate("not has_error", ctx, variables) == "true"
        assert engine.evaluate("not true", ctx, variables) == "false"


class TestConditionEngineStringContainment:
    def test_in_variable(self, engine, variables):
        ctx = AgentContext()
        assert engine.evaluate('"prod" in mode', ctx, variables) == "true"
        assert engine.evaluate('"dev" in mode', ctx, variables) == "false"


class TestConditionEngineVariableRef:
    def test_variable_truthy(self, engine, ctx, variables):
        assert engine.evaluate("name", ctx, variables) == "true"

    def test_nonexistent_variable(self, engine, ctx, variables):
        assert engine.evaluate("nonexistent_var", ctx, variables) == "false"


class TestConditionEngineComplex:
    def test_compound_expression(self, engine, variables):
        ctx = AgentContext()
        ctx.iteration_count = 3
        assert engine.evaluate("iteration >= 1 and not has_error", ctx, variables) == "true"

    def test_multiple_or(self, engine, variables):
        ctx = AgentContext()
        ctx.error = "timeout"
        assert engine.evaluate("has_tool_calls or has_error or has_result", ctx, variables) == "true"
