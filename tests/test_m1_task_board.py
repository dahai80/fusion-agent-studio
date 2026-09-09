"""M1-1 task_board dual state machine contract tests (#300).

Tests: derive_column (all status×review combinations), is_task_open contract
(both consumer paths), dequeue concurrency safety, move_task history, migration v3.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_runtime.task_board import (
    COLUMN_APPROVED,
    COLUMN_ARCHIVED,
    COLUMN_IN_PROGRESS,
    COLUMN_REVIEW,
    COLUMN_TODO,
    derive_column,
    is_task_open,
)
from agent_runtime.task_store import (
    REVIEW_STATE_APPROVED,
    REVIEW_STATE_NEEDS_FIX,
    REVIEW_STATE_NONE,
    REVIEW_STATE_REVIEW,
    TASK_STATUS_CANCELED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
    TaskStore,
)


def _make_task(status=TASK_STATUS_PENDING, review=REVIEW_STATE_NONE, owner=""):
    return Task(status=status, review_state=review, owner_agent=owner)


class TestDeriveColumn:
    def test_pending_no_owner_todo(self):
        assert derive_column(_make_task(TASK_STATUS_PENDING, REVIEW_STATE_NONE, "")) == COLUMN_TODO

    def test_pending_with_owner_todo(self):
        # pending 有 owner 仍未开始 -> todo (边界: 认领前)
        assert (
            derive_column(_make_task(TASK_STATUS_PENDING, REVIEW_STATE_NONE, "agent1"))
            == COLUMN_TODO
        )

    def test_in_progress(self):
        assert (
            derive_column(_make_task(TASK_STATUS_RUNNING, REVIEW_STATE_NONE, "agent1"))
            == COLUMN_IN_PROGRESS
        )

    def test_completed_review(self):
        assert (
            derive_column(_make_task(TASK_STATUS_COMPLETED, REVIEW_STATE_REVIEW, ""))
            == COLUMN_REVIEW
        )

    def test_completed_needs_fix(self):
        assert (
            derive_column(_make_task(TASK_STATUS_COMPLETED, REVIEW_STATE_NEEDS_FIX, ""))
            == COLUMN_REVIEW
        )

    def test_completed_approved(self):
        assert (
            derive_column(_make_task(TASK_STATUS_COMPLETED, REVIEW_STATE_APPROVED, ""))
            == COLUMN_APPROVED
        )

    def test_completed_none_falls_back_todo(self):
        # completed + review=none: 推导不出列 (未进评审态) -> todo 兜底
        assert (
            derive_column(_make_task(TASK_STATUS_COMPLETED, REVIEW_STATE_NONE, "")) == COLUMN_TODO
        )

    def test_canceled_archived(self):
        assert (
            derive_column(_make_task(TASK_STATUS_CANCELED, REVIEW_STATE_NONE, ""))
            == COLUMN_ARCHIVED
        )

    def test_failed_archived(self):
        # failed 不入看板 (归档)
        assert derive_column(_make_task(TASK_STATUS_FAILED, REVIEW_STATE_NONE, "")) == COLUMN_TODO

    def test_dict_input(self):
        task_dict = {
            "status": TASK_STATUS_RUNNING,
            "review_state": REVIEW_STATE_NONE,
            "owner_agent": "a",
        }
        assert derive_column(task_dict) == COLUMN_IN_PROGRESS

    def test_all_combinations_covered(self):
        # 契约: status×review 全组合都有确定列, 无 None 返回
        for status in [
            TASK_STATUS_PENDING,
            TASK_STATUS_RUNNING,
            TASK_STATUS_COMPLETED,
            TASK_STATUS_FAILED,
            TASK_STATUS_CANCELED,
        ]:
            for review in [
                REVIEW_STATE_NONE,
                REVIEW_STATE_REVIEW,
                REVIEW_STATE_NEEDS_FIX,
                REVIEW_STATE_APPROVED,
            ]:
                col = derive_column(_make_task(status, review, ""))
                assert col in (
                    COLUMN_TODO,
                    COLUMN_IN_PROGRESS,
                    COLUMN_REVIEW,
                    COLUMN_APPROVED,
                    COLUMN_ARCHIVED,
                ), f"uncovered: {status}/{review} -> {col}"


class TestIsTaskOpen:
    def test_pending_open(self):
        assert is_task_open(_make_task(TASK_STATUS_PENDING)) is True

    def test_in_progress_open(self):
        assert is_task_open(_make_task(TASK_STATUS_RUNNING)) is True

    def test_completed_not_open(self):
        assert is_task_open(_make_task(TASK_STATUS_COMPLETED)) is False

    def test_canceled_not_open(self):
        assert is_task_open(_make_task(TASK_STATUS_CANCELED)) is False

    def test_failed_not_open(self):
        assert is_task_open(_make_task(TASK_STATUS_FAILED)) is False

    def test_dict_input(self):
        assert is_task_open({"status": TASK_STATUS_PENDING}) is True
        assert is_task_open({"status": TASK_STATUS_COMPLETED}) is False


class TestMigrationV3:
    def test_migration_adds_columns(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        cols = {row[1] for row in store._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for col in [
            "review_state",
            "attempt_token",
            "owner_role",
            "owner_agent",
            "resource_lease_id",
            "evidence_ref",
            "team",
        ]:
            assert col in cols, f"missing column {col}"
        store.close()

    def test_migration_creates_tables(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "task_history" in tables
        assert "idempotency_index" in tables
        store.close()

    def test_migration_idempotent_rerun(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store1 = TaskStore(db_path=db)
        version1 = store1._conn.execute("PRAGMA user_version").fetchone()[0]
        store1.close()
        store2 = TaskStore(db_path=db)
        version2 = store2._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version1 == version2 == 3
        store2.close()


class TestDequeue:
    def test_dequeue_claims_first_pending(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        t1 = Task(title="t1", team="ops", status=TASK_STATUS_PENDING, priority=1)
        t2 = Task(title="t2", team="ops", status=TASK_STATUS_PENDING, priority=5)
        store.submit(t1)
        store.submit(t2)
        claimed = store.dequeue("ops", "agent1")
        assert claimed is not None
        # priority DESC -> t2 (priority=5) 先领
        assert claimed.title == "t2"
        assert claimed.owner_agent == "agent1"
        assert claimed.status == TASK_STATUS_RUNNING
        assert claimed.attempt_token != ""
        store.close()

    def test_dequeue_empty_returns_none(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        assert store.dequeue("ops", "agent1") is None
        store.close()

    def test_dequeue_team_isolation(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="ops_task", team="ops", status=TASK_STATUS_PENDING))
        store.submit(Task(title="fin_task", team="finance", status=TASK_STATUS_PENDING))
        claimed = store.dequeue("finance", "agent1")
        assert claimed.title == "fin_task"
        assert store.dequeue("finance", "agent1") is None
        # ops 任务仍 pending
        ops = store.dequeue("ops", "agent2")
        assert ops.title == "ops_task"
        store.close()

    def test_dequeue_skips_owned(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(
            Task(title="claimed", team="ops", status=TASK_STATUS_PENDING, owner_agent="other")
        )
        assert store.dequeue("ops", "agent1") is None
        store.close()


class TestMoveTask:
    def test_move_writes_history(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops", status=TASK_STATUS_PENDING))
        tid = list(store._tasks.keys())[0]
        store.dequeue("ops", "agent1")
        store.move_task(tid, TASK_STATUS_COMPLETED, REVIEW_STATE_REVIEW, "agent1", "done")
        rows = store._conn.execute(
            "SELECT from_status, to_status, from_review, to_review, actor, reason FROM task_history WHERE task_id=?",
            (tid,),
        ).fetchall()
        assert len(rows) == 2  # dequeue + move
        last = rows[-1]
        assert last[0] == TASK_STATUS_RUNNING
        assert last[1] == TASK_STATUS_COMPLETED
        assert last[2] == REVIEW_STATE_NONE
        assert last[3] == REVIEW_STATE_REVIEW
        assert last[4] == "agent1"
        assert last[5] == "done"
        store.close()

    def test_move_invalid_status_rejected(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops"))
        tid = list(store._tasks.keys())[0]
        result = store.move_task(tid, "bogus", "", "agent", "")
        assert result is None
        store.close()


class TestIdempotencyIndex:
    def test_submit_writes_index(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops", idempotency_key="key1"))
        row = store._conn.execute(
            "SELECT idempotency_key, task_id FROM idempotency_index WHERE idempotency_key=?",
            ("key1",),
        ).fetchone()
        assert row is not None
        assert row[0] == "key1"
        store.close()

    def test_find_by_idempotency(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops", idempotency_key="key1"))
        found = store.find_by_idempotency("key1")
        assert found is not None
        assert found.idempotency_key == "key1"
        assert store.find_by_idempotency("nonexistent") is None
        store.close()


class TestSetEvidenceClaimLease:
    def test_set_evidence(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops"))
        tid = list(store._tasks.keys())[0]
        assert store.set_evidence(tid, "out/ops/evidence/exec_xyz.jsonl") is True
        task = store.get(tid)
        assert task.evidence_ref == "out/ops/evidence/exec_xyz.jsonl"
        store.close()

    def test_claim_lease(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="t1", team="ops"))
        tid = list(store._tasks.keys())[0]
        assert store.claim_lease(tid, "lease_abc") is True
        task = store.get(tid)
        assert task.resource_lease_id == "lease_abc"
        store.close()


class TestListByColumn:
    def test_list_by_column_groups(self, tmp_path):
        db = str(tmp_path / "test_tasks.db")
        store = TaskStore(db_path=db)
        store.submit(Task(title="todo1", team="ops", status=TASK_STATUS_PENDING))
        store.submit(Task(title="todo2", team="ops", status=TASK_STATUS_PENDING))
        store.submit(
            Task(
                title="done1",
                team="ops",
                status=TASK_STATUS_COMPLETED,
                review_state=REVIEW_STATE_APPROVED,
            )
        )
        store.submit(Task(title="canceled1", team="ops", status=TASK_STATUS_CANCELED))
        cols = store.list_by_column("ops")
        assert len(cols["todo"]) == 2
        assert len(cols["approved"]) == 1
        assert cols["approved"][0]["title"] == "done1"
        # canceled 归档不入板
        assert "archived" not in cols or len(cols.get("archived", [])) == 0
        store.close()
