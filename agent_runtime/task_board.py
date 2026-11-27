"""Task board column derivation + isTaskOpen contract predicate (M1-1).

Board columns are derived from two orthogonal dimensions, status + review_state (not stored
independently), preventing double-write inconsistency.
is_task_open is the single contract predicate, consumed by both "board completion notify Lead"
and the "anti-restatement guard".
Changes must be kept in sync, backed by contract tests.
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
    # Derived column (source plan §5.1): avoids status/column double-write inconsistency.
    # task = Task instance or dict (with status/review_state/owner_agent).
    status = task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
    review_state = (
        task.get("review_state")
        if isinstance(task, dict)
        else getattr(task, "review_state", REVIEW_STATE_NONE)
    )
    owner_agent = (
        task.get("owner_agent") if isinstance(task, dict) else getattr(task, "owner_agent", "")
    )
    if status == TASK_STATUS_CANCELED:
        return COLUMN_ARCHIVED
    if status == TASK_STATUS_PENDING and not owner_agent:
        return COLUMN_TODO
    if status == TASK_STATUS_RUNNING:
        return COLUMN_IN_PROGRESS
    if status == TASK_STATUS_COMPLETED and review_state in (
        REVIEW_STATE_REVIEW,
        REVIEW_STATE_NEEDS_FIX,
    ):
        return COLUMN_REVIEW
    if status == TASK_STATUS_COMPLETED and review_state == REVIEW_STATE_APPROVED:
        return COLUMN_APPROVED
    return COLUMN_TODO


def is_task_open(task) -> bool:
    # Contract predicate (source plan §5.1): shared by board completion notify Lead
    # and the anti-restatement guard. Changes must be kept in sync, backed by contract
    # tests. pending/in_progress = open (needs Lead attention).
    status = task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
    return status in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING)
