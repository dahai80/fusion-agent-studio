"""Task persistence — generic Task records backed by SQLite.

模式参考 triggers.CronManager: 同目录 ~/.fusion-agent-studio/ 下独立 db,
INSERT OR REPLACE upsert, check_same_thread=False, to_thread 异步写.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _task_ttl() -> float:
    raw = os.environ.get("FUSION_TASK_TTL", str(30 * 24 * 3600)).strip()
    try:
        return float(raw)
    except ValueError:
        logger.warning("FUSION_TASK_TTL invalid '%s', fallback 30d", raw)
        return float(30 * 24 * 3600)


def _lazy_load_enabled() -> bool:
    return os.environ.get("FUSION_TASK_LAZY_LOAD", "").strip().lower() in ("1", "true", "yes")

# Task 状态机: pending(已提交待触发) -> running(执行中) -> completed/failed/canceled
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELED = "canceled"

# 触发类型: immediate(立即) / cron(周期) / run_at(一次性定时)
TRIGGER_IMMEDIATE = "immediate"
TRIGGER_CRON = "cron"
TRIGGER_RUN_AT = "run_at"

_VALID_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELED,
}
_VALID_TRIGGERS = {TRIGGER_IMMEDIATE, TRIGGER_CRON, TRIGGER_RUN_AT}

# 列读取顺序(显式 SELECT, 保证 from_row 位置稳定, 不受 ALTER 追列影响).
_TASK_COLUMNS = [
    "task_id", "title", "description", "agent_id", "graph_id", "trigger",
    "cron_expression", "run_at", "cron_job_id", "input", "status", "priority",
    "project_id", "artifact_ids", "last_result", "last_error", "retry_count",
    "max_retries", "created_at", "updated_at", "last_run_at",
]


@dataclass
class Task:
    # 通用 Task 记录: 关联 agent/graph, 可 immediate/cron/run_at 触发, 持久化产物与结果.
    task_id: str = ""
    title: str = ""
    description: str = ""
    agent_id: str = ""
    graph_id: str = ""
    trigger: str = TRIGGER_IMMEDIATE
    cron_expression: str = ""
    run_at: float = 0.0
    cron_job_id: str = ""
    input: str = ""
    status: str = TASK_STATUS_PENDING
    priority: int = 0
    project_id: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    last_result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    retry_count: int = 0
    max_retries: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_run_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "agent_id": self.agent_id,
            "graph_id": self.graph_id,
            "trigger": self.trigger,
            "cron_expression": self.cron_expression,
            "run_at": self.run_at,
            "cron_job_id": self.cron_job_id,
            "input": self.input,
            "status": self.status,
            "priority": self.priority,
            "project_id": self.project_id,
            "artifact_ids": list(self.artifact_ids),
            "last_result": dict(self.last_result),
            "last_error": self.last_error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
        }

    @classmethod
    def from_row(cls, row: tuple) -> Task:
        # row 顺序与 _init_db 列定义一致.
        artifact_ids = []
        if row[13]:
            try:
                artifact_ids = json.loads(row[13])
                if not isinstance(artifact_ids, list):
                    artifact_ids = []
            except Exception as e:
                # 审计 L-3: 静默吞 JSON 解析错 -> 空列表, 调试无法定位坏数据.
                logger.warning("task_store.from_row: bad artifact_ids JSON (task=%s): %s", row[0], e)
                artifact_ids = []
        last_result = {}
        if row[14]:
            try:
                decoded = json.loads(row[14])
                if isinstance(decoded, dict):
                    last_result = decoded
            except Exception as e:
                logger.warning("task_store.from_row: bad last_result JSON (task=%s): %s", row[0], e)
                last_result = {}
        return cls(
            task_id=row[0],
            title=row[1],
            description=row[2],
            agent_id=row[3],
            graph_id=row[4],
            trigger=row[5],
            cron_expression=row[6],
            run_at=row[7],
            cron_job_id=row[8],
            input=row[9],
            status=row[10],
            priority=row[11],
            project_id=row[12] or "",
            artifact_ids=artifact_ids,
            last_result=last_result,
            last_error=row[15] or "",
            retry_count=row[16],
            max_retries=row[17],
            created_at=row[18],
            updated_at=row[19],
            last_run_at=row[20],
        )


class TaskStore:
    # SQLite 持久化 Task 记录. db_path 空则不落库(仅内存, 测试可用).
    def __init__(self, db_path: str = ""):
        self._tasks: dict[str, Task] = {}
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # 审计 A-6/R-1/3M-1/3M-3: 跨线程共享单连接无锁无 WAL 致写竞态 + _id_seq 非线程安全.
        # Lock 串行化所有 DB 操作与 _id_seq 自增; WAL 让读不堵写; busy_timeout 等锁.
        self._write_lock = threading.RLock()
        # 自增序号, 配合毫秒时间戳生成唯一 task_id, 避免同毫秒并发提交撞 id.
        self._id_seq = 0
        self._lazy_load = _lazy_load_enabled()
        if db_path:
            self._init_db(db_path)
            if self._lazy_load:
                with self._write_lock:
                    cnt = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                self._id_seq = cnt
                logger.info("TaskStore lazy-load enabled, %d tasks on disk (not preloaded)", cnt)
            else:
                self._load_tasks()
                self._id_seq = len(self._tasks)

    def _init_db(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                agent_id TEXT DEFAULT '',
                graph_id TEXT DEFAULT '',
                trigger TEXT DEFAULT 'immediate',
                cron_expression TEXT DEFAULT '',
                run_at REAL DEFAULT 0,
                cron_job_id TEXT DEFAULT '',
                input TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                project_id TEXT DEFAULT '',
                artifact_ids TEXT DEFAULT '[]',
                last_result TEXT DEFAULT '{}',
                last_error TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0,
                updated_at REAL DEFAULT 0,
                last_run_at REAL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id)"
        )
        # 老库迁移: CREATE IF NOT EXISTS 不会补列, 先 ALTER 补 project_id, 再建其索引 (#141 priority-2).
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT DEFAULT ''")
            logger.info("Migrated tasks table: added project_id column")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)"
        )
        self._conn.commit()
        logger.info("TaskStore DB initialized: %s", db_path)

    def _load_tasks(self) -> None:
        if not self._conn:
            return
        with self._write_lock:
            rows = self._conn.execute(
                "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks"
            ).fetchall()
            for row in rows:
                task = Task.from_row(row)
                self._tasks[task.task_id] = task
        logger.info("Loaded %d tasks from DB", len(self._tasks))

    def _save_task(self, task: Task) -> None:
        if not self._conn:
            return
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (task_id, title, description, agent_id, graph_id, trigger,
                    cron_expression, run_at, cron_job_id, input, status, priority,
                    project_id, artifact_ids, last_result, last_error, retry_count, max_retries,
                    created_at, updated_at, last_run_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.title,
                    task.description,
                    task.agent_id,
                    task.graph_id,
                    task.trigger,
                    task.cron_expression,
                    task.run_at,
                    task.cron_job_id,
                    task.input,
                    task.status,
                    task.priority,
                    task.project_id,
                    json.dumps(task.artifact_ids, ensure_ascii=False),
                    json.dumps(task.last_result, ensure_ascii=False),
                    task.last_error,
                    task.retry_count,
                    task.max_retries,
                    task.created_at,
                    task.updated_at,
                    task.last_run_at,
                ),
            )
            self._conn.commit()

    def _delete_task(self, task_id: str) -> None:
        if not self._conn:
            return
        with self._write_lock:
            self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            self._conn.commit()

    def reap_expired(self) -> int:
        if not self._conn:
            return 0
        ttl = _task_ttl()
        if ttl <= 0:
            return 0
        now = time.time()
        cutoff = now - ttl
        done_statuses = (TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_CANCELED)
        placeholders = ",".join("?" for _ in done_statuses)
        with self._write_lock:
            rows = self._conn.execute(
                f"SELECT task_id FROM tasks WHERE status IN ({placeholders}) "
                f"AND created_at > 0 AND created_at < ?",
                (*done_statuses, cutoff),
            ).fetchall()
            if not rows:
                return 0
            ids = [r[0] for r in rows]
            self._conn.executemany(
                "DELETE FROM tasks WHERE task_id = ?", [(tid,) for tid in ids]
            )
            self._conn.commit()
            for tid in ids:
                self._tasks.pop(tid, None)
        logger.info("reaped %d expired tasks (ttl=%.0fs)", len(ids), ttl)
        return len(ids)

    def submit(self, task: Task) -> Task:
        # 新建/覆盖提交. 自动补 task_id/时间戳; trigger/status 走校验回退默认.
        # task_id 用 毫秒+自增序号 避免同毫秒并发提交撞 id (INSERT OR REPLACE 会覆盖).
        if not task.created_at:
            task.created_at = time.time()
        task.updated_at = time.time()
        if task.trigger not in _VALID_TRIGGERS:
            logger.warning("invalid trigger=%s, fallback immediate", task.trigger)
            task.trigger = TRIGGER_IMMEDIATE
        if task.status not in _VALID_STATUSES:
            task.status = TASK_STATUS_PENDING
        # 审计 P0: _id_seq 自增 + dict 写 + _save_task 必须原子, 否则并发提交撞 id 覆盖.
        self.reap_expired()
        with self._write_lock:
            if not task.task_id:
                self._id_seq += 1
                task.task_id = f"task_{int(time.time() * 1000)}_{self._id_seq}"
            self._tasks[task.task_id] = task
            self._save_task(task)
        logger.info(
            "Task submitted: %s trigger=%s graph=%s status=%s",
            task.task_id, task.trigger, task.graph_id, task.status,
        )
        return task

    def get(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        if not self._lazy_load or not self._conn:
            return None
        with self._write_lock:
            row = self._conn.execute(
                "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        task = Task.from_row(row)
        self._tasks[task.task_id] = task
        return task

    def list(
        self,
        status: str = "",
        agent_id: str = "",
        project_id: str = "",
        limit: int = 100,
    ) -> list[dict]:
        self.reap_expired()
        if self._lazy_load and self._conn:
            return self._list_from_db(status, agent_id, project_id, limit)
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        if agent_id:
            tasks = [t for t in tasks if t.agent_id == agent_id]
        if project_id:
            tasks = [t for t in tasks if t.project_id == project_id]
        tasks.sort(key=lambda t: (t.priority, t.created_at), reverse=True)
        if limit > 0:
            tasks = tasks[:limit]
        return [t.to_dict() for t in tasks]

    def _list_from_db(
        self,
        status: str,
        agent_id: str,
        project_id: str,
        limit: int,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT " + ", ".join(_TASK_COLUMNS)
            + " FROM tasks" + where
            + " ORDER BY priority DESC, created_at DESC"
        )
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Task.from_row(row).to_dict() for row in rows]

    def update_status(
        self,
        task_id: str,
        status: str,
        last_result: dict | None = None,
        last_error: str = "",
    ) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if status not in _VALID_STATUSES:
            logger.warning("invalid status=%s, ignore", status)
            return False
        task.status = status
        task.updated_at = time.time()
        if status == TASK_STATUS_RUNNING:
            task.last_run_at = task.updated_at
        if last_result is not None:
            task.last_result = last_result
        if last_error:
            task.last_error = last_error
        self._save_task(task)
        logger.info(
            "Task %s status -> %s (result=%d keys, error=%d chars)",
            task_id, status, len(task.last_result), len(task.last_error),
        )
        return True

    def cancel(self, task_id: str) -> bool:
        # 仅 pending/running 可取消; completed/failed/canceled 幂等返 False.
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status in (TASK_STATUS_COMPLETED, TASK_STATUS_CANCELED):
            return False
        return self.update_status(task_id, TASK_STATUS_CANCELED)

    def rerun(self, task_id: str) -> Task | None:
        # 重置为 pending, retry_count+1, 供调度/前端再次拉起.
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = TASK_STATUS_PENDING
        task.retry_count += 1
        task.last_error = ""
        task.updated_at = time.time()
        self._save_task(task)
        logger.info("Task %s rerun queued, retry_count=%d", task_id, task.retry_count)
        return task

    def add_artifacts(self, task_id: str, artifact_ids: list[str]) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        for aid in artifact_ids:
            if aid and aid not in task.artifact_ids:
                task.artifact_ids.append(aid)
        task.updated_at = time.time()
        self._save_task(task)
        return True

    def delete(self, task_id: str) -> bool:
        task = self._tasks.pop(task_id, None)
        if not task:
            return False
        self._delete_task(task_id)
        logger.info("Task deleted: %s", task_id)
        return True

    def projects(self) -> list[dict]:
        # 聚合 distinct project_id 及其任务数/状态分布 (#141 priority-2 多 Task 看板).
        buckets: dict[str, dict[str, Any]] = {}
        for t in self._tasks.values():
            pid = t.project_id or ""
            if not pid:
                continue
            b = buckets.setdefault(
                pid,
                {"project_id": pid, "total": 0, "pending": 0, "running": 0,
                 "completed": 0, "failed": 0, "canceled": 0},
            )
            b["total"] += 1
            if t.status in b:
                b[t.status] += 1
        result = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)
        logger.info("Aggregated %d projects from %d tasks", len(result), len(self._tasks))
        return result

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
