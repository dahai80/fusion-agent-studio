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
    # 审计 P2-16/3M-1-leg3: 默认开启懒加载, 防 10 万历史 task 启动全载致内存膨胀.
    # 显式 FUSION_TASK_LAZY_LOAD=0 关闭 (回退 eager 全载, 兼容旧部署).
    val = os.environ.get("FUSION_TASK_LAZY_LOAD", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _task_max_concurrency() -> int:
    # #239: task 执行并发上限, task.health 上报给 fusion-event 做反向背压.
    # 默认 5 (对齐 fusion-event tokenBucketMax). cron run_loop 当前无信号量,
    # 此值是声明式上限 (运维契约), 非 hard gate.
    raw = os.environ.get("FUSION_TASK_CONCURRENCY", "5").strip()
    try:
        val = int(raw)
    except ValueError:
        logger.warning("FUSION_TASK_CONCURRENCY invalid '%s', fallback 5", raw)
        return 5
    return val if val > 0 else 5


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

# 评审态 (正交维度 2, M1-1): none/review/needs_fix/approved
REVIEW_STATE_NONE = "none"
REVIEW_STATE_REVIEW = "review"
REVIEW_STATE_NEEDS_FIX = "needs_fix"
REVIEW_STATE_APPROVED = "approved"
_VALID_REVIEW_STATES = {
    REVIEW_STATE_NONE,
    REVIEW_STATE_REVIEW,
    REVIEW_STATE_NEEDS_FIX,
    REVIEW_STATE_APPROVED,
}

# 列读取顺序(显式 SELECT, 保证 from_row 位置稳定, 不受 ALTER 追列影响).
_TASK_COLUMNS = [
    "task_id",
    "title",
    "description",
    "agent_id",
    "graph_id",
    "trigger",
    "cron_expression",
    "run_at",
    "cron_job_id",
    "input",
    "status",
    "priority",
    "project_id",
    "artifact_ids",
    "last_result",
    "last_error",
    "retry_count",
    "max_retries",
    "created_at",
    "updated_at",
    "last_run_at",
    "idempotency_key",
    "review_state",
    "attempt_token",
    "owner_role",
    "owner_agent",
    "resource_lease_id",
    "evidence_ref",
    "team",
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
    idempotency_key: str = ""
    # M1-1 双状态机扩展 (正交维度 2 + 归属/租约/证据)
    review_state: str = REVIEW_STATE_NONE
    attempt_token: str = ""
    owner_role: str = ""
    owner_agent: str = ""
    resource_lease_id: str = ""
    evidence_ref: str = ""
    team: str = "default"

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
            "idempotency_key": self.idempotency_key,
            "review_state": self.review_state,
            "attempt_token": self.attempt_token,
            "owner_role": self.owner_role,
            "owner_agent": self.owner_agent,
            "resource_lease_id": self.resource_lease_id,
            "evidence_ref": self.evidence_ref,
            "team": self.team,
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
                logger.warning(
                    "task_store.from_row: bad artifact_ids JSON (task=%s): %s", row[0], e
                )
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
            idempotency_key=row[21] if len(row) > 21 and row[21] else "",
            review_state=row[22] if len(row) > 22 and row[22] else REVIEW_STATE_NONE,
            attempt_token=row[23] if len(row) > 23 and row[23] else "",
            owner_role=row[24] if len(row) > 24 and row[24] else "",
            owner_agent=row[25] if len(row) > 25 and row[25] else "",
            resource_lease_id=row[26] if len(row) > 26 and row[26] else "",
            evidence_ref=row[27] if len(row) > 27 and row[27] else "",
            team=row[28] if len(row) > 28 and row[28] else "default",
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
        # #238: 上次 submit 是否命中幂等去重 (RPC 层读此标志回写 deduped).
        self.last_submit_deduped = False
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
                last_run_at REAL DEFAULT 0,
                idempotency_key TEXT DEFAULT ''
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent_id)")
        self._conn.commit()
        # 审计 3M-6: schema 版本管理. PRAGMA user_version 门禁有序迁移.
        self._run_schema_migrations(self._conn)
        logger.info("TaskStore DB initialized: %s", db_path)

    def _run_schema_migrations(self, conn) -> None:
        migrations = [
            self._migration_v1_project_id,
            self._migration_v2_idempotency_key,
            self._migration_v3_team_state,
        ]
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for idx, migrate in enumerate(migrations, start=1):
            if current >= idx:
                continue
            logger.info("task_store schema migration v%d (from v%d)", idx, current)
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {idx}")
            conn.commit()
            current = idx

    def _migration_v1_project_id(self, conn) -> None:
        # 老库迁移: CREATE IF NOT EXISTS 不会补列, ALTER 补 project_id, 再建其索引
        # (#141 priority-2). 幂等: 探列存在再 ALTER.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT DEFAULT ''")
            logger.info("Migrated tasks table: added project_id column")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")

    def _migration_v2_idempotency_key(self, conn) -> None:
        # #238: task.submit 幂等去重. ALTER 补 idempotency_key 列 + 唯一索引
        # (空键不冲突, 多空行允许). 幂等: 探列存在再 ALTER.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "idempotency_key" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN idempotency_key TEXT DEFAULT ''")
            logger.info("Migrated tasks table: added idempotency_key column")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency "
            "ON tasks(idempotency_key) WHERE idempotency_key != ''"
        )

    def _migration_v3_team_state(self, conn) -> None:
        # M1-1 双状态机: review_state + attempt_token + owner/lease/evidence/team 列,
        # task_history 表 (迁移审计) + idempotency_index 表 (跨状态查重).
        # 幂等: 探列存在再 ALTER.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        new_cols = [
            ("review_state", "TEXT DEFAULT 'none'"),
            ("attempt_token", "TEXT DEFAULT ''"),
            ("owner_role", "TEXT DEFAULT ''"),
            ("owner_agent", "TEXT DEFAULT ''"),
            ("resource_lease_id", "TEXT DEFAULT ''"),
            ("evidence_ref", "TEXT DEFAULT ''"),
            ("team", "TEXT DEFAULT 'default'"),
        ]
        for col_name, col_def in new_cols:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_def}")
                logger.info("Migrated tasks table: added %s column", col_name)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_team_status ON tasks(team, status)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_agent) WHERE owner_agent != ''"
        )
        # 迁移历史表: 谁何时为何迁移 (from->to status/review).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                from_status TEXT DEFAULT '',
                to_status TEXT DEFAULT '',
                from_review TEXT DEFAULT '',
                to_review TEXT DEFAULT '',
                actor TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                ts REAL DEFAULT 0,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id, ts)"
        )
        # 幂等索引独立表: 跨状态查重 (替代 tasks 上偏索引, 支持查重查询不耦合主表).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_index (
                idempotency_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                created_at REAL DEFAULT 0
            )
            """
        )
        logger.info("Migrated tasks table: v3 team_state (task_history + idempotency_index)")

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

    def _get_task(self, task_id: str) -> Task | None:
        # 审计 P2-12: lazy-load 模式下 task 仅在磁盘, 内存 dict miss -> update_status/
        # cancel/rerun/add_artifacts/delete 恒 False (盲区). 缓存 miss 时按 id 从磁盘
        # 单行加载进 dict, 后续写操作正常. eager 模式行为不变 (dict 已满).
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        if not self._conn or not self._lazy_load:
            return None
        with self._write_lock:
            row = self._conn.execute(
                "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        task = Task.from_row(row)
        self._tasks[task_id] = task
        logger.info("lazy-loaded task %s from disk into cache", task_id)
        return task

    def _save_task(self, task: Task) -> None:
        if not self._conn:
            return
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (task_id, title, description, agent_id, graph_id, trigger,
                    cron_expression, run_at, cron_job_id, input, status, priority,
                    project_id, artifact_ids, last_result, last_error, retry_count, max_retries,
                    created_at, updated_at, last_run_at, idempotency_key,
                    review_state, attempt_token, owner_role, owner_agent,
                    resource_lease_id, evidence_ref, team)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    task.idempotency_key,
                    task.review_state,
                    task.attempt_token,
                    task.owner_role,
                    task.owner_agent,
                    task.resource_lease_id,
                    task.evidence_ref,
                    task.team,
                ),
            )
            self._conn.commit()

    def _delete_task(self, task_id: str) -> int:
        # 审计 P2-12: 返回删除行数, 供 delete() 判定磁盘上是否真存在 (lazy 模式).
        if not self._conn:
            return 0
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            self._conn.commit()
            return cur.rowcount or 0

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
            self._conn.executemany("DELETE FROM tasks WHERE task_id = ?", [(tid,) for tid in ids])
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
            # #238: 幂等去重. 同 idempotency_key 已存在 -> 返回旧 task, 不新建行.
            if task.idempotency_key:
                existing = self._find_by_idempotency_key(task.idempotency_key)
                if existing is not None:
                    logger.info(
                        "Task deduped by idempotency_key=%s -> existing %s",
                        task.idempotency_key,
                        existing.task_id,
                    )
                    self.last_submit_deduped = True
                    return existing
            self.last_submit_deduped = False
            if not task.task_id:
                self._id_seq += 1
                task.task_id = f"task_{int(time.time() * 1000)}_{self._id_seq}"
            self._tasks[task.task_id] = task
            self._save_task(task)
            # M1-1: 幂等索引独立表 (跨状态查重, 不耦合主表).
            if task.idempotency_key and self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO idempotency_index (idempotency_key, task_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (task.idempotency_key, task.task_id, task.created_at),
                )
                self._conn.commit()
        logger.info(
            "Task submitted: %s trigger=%s graph=%s status=%s",
            task.task_id,
            task.trigger,
            task.graph_id,
            task.status,
        )
        return task

    def get(self, task_id: str) -> Task | None:
        # 审计 P2-12: 复用 _get_task 的 lazy-load 磁盘回填, 统一盲区修复入口.
        return self._get_task(task_id)

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
            "SELECT "
            + ", ".join(_TASK_COLUMNS)
            + " FROM tasks"
            + where
            + " ORDER BY priority DESC, created_at DESC"
        )
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self._write_lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Task.from_row(row).to_dict() for row in rows]

    def _find_by_idempotency_key(self, key: str) -> Task | None:
        # #238: 幂等查找. 走 _TASK_COLUMNS 显式 SELECT (ALTER 追列位置稳定).
        # caller 必须持有 _write_lock (submit 内调用).
        if not self._conn or not key:
            return None
        sql = "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks WHERE idempotency_key = ? LIMIT 1"
        row = self._conn.execute(sql, (key,)).fetchone()
        if row is None:
            return None
        task = Task.from_row(row)
        self._tasks[task.task_id] = task
        return task

    def count_by_status(self, status: str) -> int:
        # #239: 按状态计数, task.health 队列深度上报. 无 db 时走内存 dict.
        if self._lazy_load and self._conn:
            with self._write_lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)
                ).fetchone()
            return int(row[0]) if row else 0
        return sum(1 for t in self._tasks.values() if t.status == status)

    def total_count(self) -> int:
        # #239: 全量计数, task.health.total_tasks.
        if self._lazy_load and self._conn:
            with self._write_lock:
                row = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            return int(row[0]) if row else 0
        return len(self._tasks)

    def update_status(
        self,
        task_id: str,
        status: str,
        last_result: dict | None = None,
        last_error: str = "",
    ) -> bool:
        task = self._get_task(task_id)
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
            task_id,
            status,
            len(task.last_result),
            len(task.last_error),
        )
        return True

    def cancel(self, task_id: str) -> bool:
        # 仅 pending/running 可取消; completed/failed/canceled 幂等返 False.
        task = self._get_task(task_id)
        if not task:
            return False
        if task.status in (TASK_STATUS_COMPLETED, TASK_STATUS_CANCELED):
            return False
        return self.update_status(task_id, TASK_STATUS_CANCELED)

    def rerun(self, task_id: str) -> Task | None:
        # 重置为 pending, retry_count+1, 供调度/前端再次拉起.
        task = self._get_task(task_id)
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
        task = self._get_task(task_id)
        if not task:
            return False
        for aid in artifact_ids:
            if aid and aid not in task.artifact_ids:
                task.artifact_ids.append(aid)
        task.updated_at = time.time()
        self._save_task(task)
        return True

    def delete(self, task_id: str) -> bool:
        # 审计 P2-12: lazy 模式 task 仅磁盘, pop miss -> 返 False 但磁盘残留.
        # 先弹缓存, 再无条件删磁盘行 (存在则删, 不存在无副作用), 按磁盘实际删除数判定.
        self._tasks.pop(task_id, None)
        deleted_rows = self._delete_task(task_id)
        if deleted_rows:
            logger.info("Task deleted: %s", task_id)
            return True
        return False

    def projects(self) -> list[dict]:
        # 聚合 distinct project_id 及其任务数/状态分布 (#141 priority-2 多 Task 看板).
        # 审计 P2-12: lazy 模式 dict 仅部分 task, 从 DB 聚合保证全量.
        buckets: dict[str, dict[str, Any]] = {}
        if self._lazy_load and self._conn:
            with self._write_lock:
                rows = self._conn.execute(
                    "SELECT project_id, status, COUNT(*) FROM tasks "
                    "WHERE project_id != '' GROUP BY project_id, status"
                ).fetchall()
            for pid, status, cnt in rows:
                b = buckets.setdefault(
                    pid,
                    {
                        "project_id": pid,
                        "total": 0,
                        "pending": 0,
                        "running": 0,
                        "completed": 0,
                        "failed": 0,
                        "canceled": 0,
                    },
                )
                b["total"] += cnt
                if status in b:
                    b[status] += cnt
        else:
            for t in self._tasks.values():
                pid = t.project_id or ""
                if not pid:
                    continue
                b = buckets.setdefault(
                    pid,
                    {
                        "project_id": pid,
                        "total": 0,
                        "pending": 0,
                        "running": 0,
                        "completed": 0,
                        "failed": 0,
                        "canceled": 0,
                    },
                )
                b["total"] += 1
                if t.status in b:
                    b[t.status] += 1
        result = sorted(buckets.values(), key=lambda b: b["total"], reverse=True)
        logger.info("Aggregated %d projects", len(result))
        return result

    def _append_history(
        self,
        task_id: str,
        from_status: str,
        to_status: str,
        from_review: str,
        to_review: str,
        actor: str,
        reason: str,
    ) -> None:
        # M1-1: 迁移审计行 (谁何时为何迁移). caller 持有 _write_lock.
        if not self._conn:
            return
        self._conn.execute(
            "INSERT INTO task_history (task_id, from_status, to_status, from_review, to_review, actor, reason, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, from_status, to_status, from_review, to_review, actor, reason, time.time()),
        )

    def dequeue(self, team: str, owner_agent: str, task_type: str = "") -> Task | None:
        # M1-1/M1-7: 认领 todo 列首个任务 (worker pull 模型). 原子单语句
        # UPDATE...RETURNING — subquery 选首个 pending 无 owner 任务 + WHERE
        # status=pending guard + RETURNING task_id. SQLite 写锁保护整个语句,
        # 无 RMW 窗口, 跨进程安全 (RLock 仅进程内串行, DB 写锁跨进程串行).
        # RETURNING 仅在行实际被更新 (status 仍 pending) 时返回 task_id,
        # 否则 None → 跨进程竞争第二个进程正确得到 None.
        if not self._conn or not owner_agent:
            return None
        import uuid

        token = uuid.uuid4().hex
        team = team or "default"
        now = time.time()
        with self._write_lock:
            sql = (
                "UPDATE tasks SET status = ?, owner_agent = ?, attempt_token = ?, "
                "updated_at = ?, last_run_at = ? "
                "WHERE task_id = ("
                "  SELECT task_id FROM tasks "
                "  WHERE team = ? AND status = ? AND owner_agent = '' "
                "  ORDER BY priority DESC, created_at ASC LIMIT 1"
                ") AND status = ? "
                "RETURNING task_id"
            )
            row = self._conn.execute(
                sql,
                (
                    TASK_STATUS_RUNNING, owner_agent, token, now, now,
                    team, TASK_STATUS_PENDING, TASK_STATUS_PENDING,
                ),
            ).fetchone()
            if row is None:
                return None
            task_id = row[0]
            self._append_history(
                task_id,
                TASK_STATUS_PENDING,
                TASK_STATUS_RUNNING,
                REVIEW_STATE_NONE,
                REVIEW_STATE_NONE,
                owner_agent,
                "dequeue claim",
            )
            self._conn.commit()
        # 强制从 DB 重载缓存 (UPDATE 不回填 _tasks dict, 旧对象 status 过期).
        self._tasks.pop(task_id, None)
        task = self._get_task(task_id)
        if task:
            logger.info(
                "Task %s dequeued by %s team=%s token=%s",
                task_id,
                owner_agent,
                team,
                token[:8],
            )
        return task

    def move_task(
        self,
        task_id: str,
        to_status: str,
        to_review: str = "",
        actor: str = "",
        reason: str = "",
    ) -> Task | None:
        # M1-1: 状态迁移 + 写 task_history 行. review_state 空串=不变.
        task = self._get_task(task_id)
        if not task:
            return None
        if to_status and to_status not in _VALID_STATUSES:
            logger.warning("move_task invalid status=%s, ignore", to_status)
            return None
        if to_review and to_review not in _VALID_REVIEW_STATES:
            logger.warning("move_task invalid review=%s, ignore", to_review)
            return None
        from_status = task.status
        from_review = task.review_state
        with self._write_lock:
            if to_status:
                task.status = to_status
            if to_review:
                task.review_state = to_review
            task.updated_at = time.time()
            if to_status == TASK_STATUS_RUNNING:
                task.last_run_at = task.updated_at
            self._save_task(task)
            self._append_history(
                task_id,
                from_status,
                task.status,
                from_review,
                task.review_state,
                actor or "system",
                reason,
            )
            self._conn.commit()
        logger.info(
            "Task %s moved %s->%s review %s->%s by %s (%s)",
            task_id,
            from_status,
            task.status,
            from_review,
            task.review_state,
            actor,
            reason,
        )
        return task

    def set_evidence(self, task_id: str, evidence_ref: str) -> bool:
        # M1-1: 写证据 jsonl 路径 (execution 证据链, 崩溃恢复靠证据 reconcile).
        task = self._get_task(task_id)
        if not task:
            return False
        task.evidence_ref = evidence_ref
        task.updated_at = time.time()
        self._save_task(task)
        logger.info("Task %s evidence_ref=%s", task_id, evidence_ref)
        return True

    def set_review_state(self, task_id: str, state: str) -> bool:
        # M1-7: 写评审态 (reconcile 标 needs_fix). none/review/needs_fix/approved.
        if state not in _VALID_REVIEW_STATES:
            logger.warning("invalid review_state=%s, ignore", state)
            return False
        task = self._get_task(task_id)
        if not task:
            return False
        task.review_state = state
        task.updated_at = time.time()
        self._save_task(task)
        logger.info("Task %s review_state=%s", task_id, state)
        return True

    def claim_lease(self, task_id: str, lease_id: str) -> bool:
        # M1-1: 关联资源租约 (M1-4 ResourceLease 写 resource_lease_id).
        task = self._get_task(task_id)
        if not task:
            return False
        task.resource_lease_id = lease_id
        task.updated_at = time.time()
        self._save_task(task)
        logger.info("Task %s lease=%s", task_id, lease_id)
        return True

    def list_by_column(self, team: str = "") -> dict[str, list[dict]]:
        # M1-1: 按 derive_column 分组 (GUI 看板用). 推导式列避免双写不一致.
        from .task_board import derive_column

        team = team or "default"
        columns: dict[str, list[dict]] = {
            "todo": [],
            "in_progress": [],
            "review": [],
            "approved": [],
        }
        if self._lazy_load and self._conn:
            with self._write_lock:
                rows = self._conn.execute(
                    "SELECT " + ", ".join(_TASK_COLUMNS) + " FROM tasks WHERE team = ?",
                    (team,),
                ).fetchall()
            for row in rows:
                task = Task.from_row(row)
                col = derive_column(task)
                if col == "archived":
                    continue
                columns.setdefault(col, []).append(task.to_dict())
        else:
            for task in self._tasks.values():
                if task.team != team:
                    continue
                col = derive_column(task)
                if col == "archived":
                    continue
                columns.setdefault(col, []).append(task.to_dict())
        for col in columns:
            columns[col].sort(
                key=lambda t: (t.get("priority", 0), t.get("created_at", 0)), reverse=True
            )
        return columns

    def find_by_idempotency(self, key: str) -> Task | None:
        # M1-1: 跨状态幂等查重 (查 idempotency_index 独立表). 公开方法 (非 submit 内部).
        if not self._conn or not key:
            return None
        with self._write_lock:
            row = self._conn.execute(
                "SELECT task_id FROM idempotency_index WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._get_task(row[0])

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
