"""Shared module-level helpers for the runtime + its mixin modules.

Extracted from runtime.py (audit 0826 P2-4 god-object split) to break the
circular import that would arise if mixin modules imported these symbols
directly from runtime.py. This module is a leaf — no back-imports.
"""
from __future__ import annotations

import logging
import os

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
