"""Shared module-level helpers for the runtime + its mixin modules.

Extracted from runtime.py (audit 0826 P2-4 god-object split) to break the
circular import that would arise if mixin modules imported these symbols
directly from runtime.py. This module is a leaf — no back-imports.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from .context import AgentContext
from .variable_manager import VariableManager

logger = logging.getLogger("agent_runtime.runtime")

_MAX_TOOL_CALL_CHAIN = 10
_MAX_RETRY_CONTEXT_MESSAGES = 20


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _max_sub_graph_depth() -> int:
    # 审计 E-16: 子图递归深度上限. 默认 8, FUSION_SUB_GRAPH_MAX_DEPTH 调.
    raw = os.environ.get("FUSION_SUB_GRAPH_MAX_DEPTH", "").strip()
    try:
        val = int(raw) if raw else 8
        return val if val > 0 else 8
    except ValueError:
        return 8


def _parallel_branch_concurrency() -> int:
    # 审计 P3-3: 并行节点分支并发上限. 0 = 不限 (回退 gather 全并发).
    # 默认 0 保持向后兼容; 生产设 FUSION_PARALLEL_BRANCH_CONCURRENCY=N 节流,
    # 避免宽 fan-out 瞬时打爆 LLM/工具后端 (LLM 调用另由 P1-4 信号量兜底).
    raw = os.environ.get("FUSION_PARALLEL_BRANCH_CONCURRENCY", "").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


class ConditionEngine:
    """Evaluates condition expressions against agent context.

    Supports:
    - Boolean literals: true, false
    - Context checks: has_tool_calls, has_error, has_result
    - Comparisons: iteration >= N, token_count > N, etc.
    - Variable references: {{ var }} comparisons
    - Logical operators: and, or, not
    - String containment: "text" in content
    """

    def evaluate(self, expr: str, ctx: AgentContext, variables: VariableManager) -> str:
        expr = expr.strip()
        if not expr:
            return "false"

        expr_lower = expr.lower()

        if expr_lower == "true":
            return "true"
        if expr_lower == "false":
            return "false"

        if re.search(r"\bor\b", expr_lower):
            parts = re.split(r"\s+or\s+", expr, flags=re.IGNORECASE)
            return (
                "true"
                if any(self.evaluate(p, ctx, variables) == "true" for p in parts)
                else "false"
            )

        if re.search(r"\band\b", expr_lower):
            parts = re.split(r"\s+and\s+", expr, flags=re.IGNORECASE)
            return (
                "true"
                if all(self.evaluate(p, ctx, variables) == "true" for p in parts)
                else "false"
            )

        if expr_lower.startswith("not "):
            inner = self.evaluate(expr[4:], ctx, variables)
            return "false" if inner == "true" else "true"

        if expr_lower == "has_tool_calls":
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("tool_calls"):
                    return "true"
            return "false"

        if expr_lower == "has_error":
            return "true" if ctx.error else "false"

        if expr_lower == "has_result":
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "tool":
                    return "true"
            return "false"

        comp_match = re.match(r"(\w+)\s*(>=|<=|!=|==|>|<)\s*(.+)", expr)
        if comp_match:
            left_name = comp_match.group(1)
            op = comp_match.group(2)
            right_raw = comp_match.group(3).strip()
            left_val = self._resolve_value(left_name, ctx, variables)
            right_val = self._resolve_literal(right_raw, variables)
            return self._compare(left_val, op, right_val)

        in_match = re.match(r'["\'](.+?)["\']\s+in\s+(\w+)', expr)
        if in_match:
            needle = in_match.group(1)
            haystack_name = in_match.group(2)
            haystack_val = str(self._resolve_value(haystack_name, ctx, variables))
            return "true" if needle in haystack_val else "false"

        var_val = variables.get(expr, "")
        if var_val:
            return "true" if var_val else "false"

        return "false"

    def _resolve_value(self, name: str, ctx: AgentContext, variables: VariableManager) -> Any:
        if name == "iteration":
            return ctx.iteration_count
        if name == "token_count":
            usage = ctx.token_usage()
            return usage.get("total", 0)
        if name == "prompt_tokens":
            return ctx.token_usage().get("prompt_tokens", 0)
        if name == "completion_tokens":
            return ctx.token_usage().get("completion_tokens", 0)
        if name == "message_count":
            return len(ctx.messages)
        if name == "error":
            return ctx.error
        var_val = variables.get(name, None)
        if var_val is not None:
            return var_val
        return 0

    def _resolve_literal(self, raw: str, variables: VariableManager) -> Any:
        raw = raw.strip()
        if raw.startswith("{{") and raw.endswith("}}"):
            var_name = raw[2:-2].strip()
            return variables.get(var_name, 0)
        if raw.startswith('"') or raw.startswith("'"):
            return raw[1:-1]
        # #212: bool 字面量归一 (大小写不敏感) -> Python bool, 否则裸 "true" 串
        # 与工具输出的 Python bool 比较永远判假.
        low = raw.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        # #294: bareword-as-string fallthrough is intentional contract. An
        # unquoted right-hand token that is not {{var}}/quoted/true/false/int/
        # float is treated as a string literal (e.g. `status == publish_ok` ->
        # str "publish_ok"). Authors wanting the literal string "true"/"5"
        # MUST quote it to avoid collision with bool/numeric coercion.
        logger.debug(
            "condition literal bareword resolved as str: %r (quote to disambiguate)",
            raw,
        )
        return raw

    @staticmethod
    def _coerce_pair(left: Any, right: Any) -> tuple[Any, Any]:
        # #212: 跨类型比较归一. 工具输出 Python bool/int, condition 字面量可能
        # 是 "true"/"5" 字符串 -> 严格相等永远假. 归一两边同类型再比.
        # bool 先于 int (bool 是 int 子类), 避免被 int 分支吞掉.
        if isinstance(left, bool) or isinstance(right, bool):
            if isinstance(left, bool) and isinstance(right, bool):
                return left, right
            if isinstance(left, bool) and isinstance(right, str):
                rl = right.strip().lower()
                if rl == "true":
                    return left, True
                if rl == "false":
                    return left, False
            if isinstance(right, bool) and isinstance(left, str):
                ll = left.strip().lower()
                if ll == "true":
                    return True, right
                if ll == "false":
                    return False, right
            return left, right
        # 数字串 vs 数字: "5" == 5, "5" >= 3
        if isinstance(left, (int, float)) and isinstance(right, str):
            try:
                return left, float(right) if "." in right else int(right)
            except ValueError:
                return left, right
        if isinstance(right, (int, float)) and isinstance(left, str):
            try:
                return float(left) if "." in left else int(left), right
            except ValueError:
                return left, right
        return left, right

    def _compare(self, left: Any, op: str, right: Any) -> str:
        # #212: 先归一再比, 避免跨类型严格相等静默判假.
        left, right = self._coerce_pair(left, right)
        # #294: debug-trace resolved types so graph authors can inspect what
        # each side actually evaluated to (aids bareword/quoting questions).
        logger.debug(
            "condition compare: %r(%s) %s %r(%s)",
            left,
            type(left).__name__,
            op,
            right,
            type(right).__name__,
        )
        try:
            if op == "==":
                return "true" if left == right else "false"
            if op == "!=":
                return "true" if left != right else "false"
            if op == ">=":
                return "true" if left >= right else "false"
            if op == "<=":
                return "true" if left <= right else "false"
            if op == ">":
                return "true" if left > right else "false"
            if op == "<":
                return "true" if left < right else "false"
        except TypeError:
            # #294: surface swallowed comparison failures so DAG gate
            # misroutes (e.g. list/dict tool output vs scalar) are
            # traceable in daemon logs instead of silent false-branch.
            logger.warning(
                "condition compare TypeError swallowed -> false: "
                "%r(%s) %s %r(%s)",
                left,
                type(left).__name__,
                op,
                right,
                type(right).__name__,
            )
            return "false"
        return "false"
