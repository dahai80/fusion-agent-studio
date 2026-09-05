"""#294: ConditionEngine bareword literal resolution + TypeError traceability.

Covers:
- bareword-as-string fallthrough contract (unquoted non-true/false/numeric
  right-hand token resolved as str literal).
- quoted "true"/"5" disambiguation (literal string, not bool/numeric).
- TypeError swallow now logs a warning (traceability for DAG gate misroutes).
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime.context import AgentContext
from agent_runtime.runtime import ConditionEngine
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
    v.set("publish_status", "publish_ok")
    v.set("status", "publish_ok")
    v.set("items", ["a", "b"])
    return v


class TestIssue294BarewordResolution:
    def test_bareword_resolves_as_string(self, engine, ctx, variables):
        # `publish_status == publish_ok`: right bareword -> str "publish_ok"
        assert engine.evaluate("publish_status == publish_ok", ctx, variables) == "true"

    def test_bareword_mismatch_is_false(self, engine, ctx, variables):
        assert engine.evaluate("status == done", ctx, variables) == "false"

    def test_quoted_true_is_string_not_bool(self, engine, ctx, variables):
        # Quoted "true" stays str; variable holding str "true" compares equal.
        variables.set("flag", "true")
        assert engine.evaluate('flag == "true"', ctx, variables) == "true"

    def test_bareword_true_is_bool(self, engine, ctx, variables):
        # Unquoted true -> bool True; variable holding bool compares equal.
        variables.set("enabled", True)
        assert engine.evaluate("enabled == true", ctx, variables) == "true"

    def test_quoted_numeric_is_string(self, engine, ctx, variables):
        variables.set("code", "5")
        assert engine.evaluate('code == "5"', ctx, variables) == "true"


class TestIssue294TypeErrorTraceability:
    def test_type_error_swallowed_returns_false(self, engine, ctx, variables):
        # list vs str: == does not raise, but >= would. Use ordering op on
        # incompatible types to force TypeError path.
        variables.set("items", ["a", "b"])
        variables.set("scalar", "done")
        # list >= str -> TypeError -> "false"
        assert engine.evaluate("items >= scalar", ctx, variables) == "false"

    def test_type_error_logs_warning(self, engine, ctx, variables, caplog):
        variables.set("items", ["a", "b"])
        variables.set("scalar", "done")
        with caplog.at_level(logging.WARNING, logger="agent_runtime.runtime"):
            engine.evaluate("items >= scalar", ctx, variables)
        assert any(
            "TypeError swallowed" in rec.message for rec in caplog.records
        ), "TypeError swallow must log a warning"

    def test_compare_logs_resolved_types_debug(self, engine, ctx, variables, caplog):
        with caplog.at_level(logging.DEBUG, logger="agent_runtime.runtime"):
            engine.evaluate("publish_status == publish_ok", ctx, variables)
        assert any(
            "condition compare" in rec.message for rec in caplog.records
        ), "compare must debug-log resolved types"

    def test_bareword_logs_debug(self, engine, ctx, variables, caplog):
        with caplog.at_level(logging.DEBUG, logger="agent_runtime.runtime"):
            engine.evaluate("status == publish_ok", ctx, variables)
        assert any(
            "bareword" in rec.message for rec in caplog.records
        ), "bareword fallthrough must debug-log"
