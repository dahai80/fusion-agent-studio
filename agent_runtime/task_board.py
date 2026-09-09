"""Task board column derivation + isTaskOpen contract predicate (M1-1).

看板列由 status + review_state 两正交维度推导 (非独立存储), 杜绝双写不一致.
is_task_open 是单一契约谓词, 被"看板完成通知 Lead"与"防重述守卫"两处消费.
修改须同步, 配契约测试.
"""

from __future__ import annotations

import logging

from .task_store import (
    REVIEW_STATE_APPROVED,
    REVIEW_STATE_NEEDS_FIX,
    REVIEW_STATE_NONE,
    REVIEW_STATE_REVIEW,
    TASK_STATUS_CANCELED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
)

logger = logging.getLogger(__name__)

COLUMN_TODO = "todo"
COLUMN_IN_PROGRESS = "in_progress"
COLUMN_REVIEW = "review"
COLUMN_APPROVED = "approved"
COLUMN_ARCHIVED = "archived"


def derive_column(task) -> str:
    # 推导式列 (源方案 §5.1): 避免 status/列双写不一致.
    # task = Task 实例或 dict (含 status/review_state/owner_agent).
    status = task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
    review_state = task.get("review_state") if isinstance(task, dict) else getattr(task, "review_state", REVIEW_STATE_NONE)
    owner_agent = task.get("owner_agent") if isinstance(task, dict) else getattr(task, "owner_agent", "")
    if status == TASK_STATUS_CANCELED:
        return COLUMN_ARCHIVED
    if status == TASK_STATUS_PENDING and not owner_agent:
        return COLUMN_TODO
    if status == TASK_STATUS_RUNNING:
        return COLUMN_IN_PROGRESS
    if status == TASK_STATUS_COMPLETED and review_state in (REVIEW_STATE_REVIEW, REVIEW_STATE_NEEDS_FIX):
        return COLUMN_REVIEW
    if status == TASK_STATUS_COMPLETED and review_state == REVIEW_STATE_APPROVED:
        return COLUMN_APPROVED
    return COLUMN_TODO


def is_task_open(task) -> bool:
    # 契约谓词 (源方案 §5.1): 看板完成通知 Lead 与防重述守卫共享此判定.
    # 修改须同步, 配契约测试. pending/in_progress = open (需 Lead 关注).
    status = task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
    return status in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING)
