"""Webhook and Cron trigger system for agent execution."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Webhook:
    """A webhook trigger configuration."""

    id: str
    name: str
    secret: str = ""
    graph_id: str = ""
    enabled: bool = True
    created_at: float = 0.0


@dataclass
class CronJob:
    """A scheduled cron job configuration."""

    id: str
    name: str
    expression: str  # cron expression: "*/5 * * * *"
    graph_id: str = ""
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    created_at: float = 0.0
    input_data: str = ""
    max_retries: int = 0
    retry_count: int = 0
    one_shot: bool = False  # #141 priority-3: 一次性定时任务 (run_at), 触发后自动注销

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "expression": self.expression,
            "graph_id": self.graph_id,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "created_at": self.created_at,
            "input_data": self.input_data,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "one_shot": self.one_shot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronJob:
        return cls(
            id=data["id"],
            name=data["name"],
            expression=data["expression"],
            graph_id=data.get("graph_id", ""),
            enabled=data.get("enabled", True),
            last_run=data.get("last_run", 0.0),
            next_run=data.get("next_run", 0.0),
            created_at=data.get("created_at", 0.0),
            input_data=data.get("input_data", ""),
            max_retries=data.get("max_retries", 0),
            retry_count=data.get("retry_count", 0),
            one_shot=data.get("one_shot", False),
        )


@dataclass
class CronExecution:
    """Record of a single cron job execution."""

    id: str = ""
    job_id: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = ""
    error: str = ""
    result_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error": self.error,
            "result_preview": self.result_preview,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronExecution:
        return cls(
            id=data.get("id", ""),
            job_id=data.get("job_id", ""),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at", 0.0),
            status=data.get("status", ""),
            error=data.get("error", ""),
            result_preview=data.get("result_preview", ""),
        )


class WebhookManager:
    """Manages webhook triggers for agent execution."""

    def __init__(self):
        self._webhooks: dict[str, Webhook] = {}
        self._handlers: dict[str, Callable] = {}

    def register(self, webhook: Webhook, handler: Callable | None = None) -> None:
        self._webhooks[webhook.id] = webhook
        if handler:
            self._handlers[webhook.id] = handler

    def unregister(self, webhook_id: str) -> None:
        self._webhooks.pop(webhook_id, None)
        self._handlers.pop(webhook_id, None)

    def get(self, webhook_id: str) -> Webhook | None:
        return self._webhooks.get(webhook_id)

    def list(self) -> list[dict]:
        return [
            {"id": w.id, "name": w.name, "graph_id": w.graph_id, "enabled": w.enabled}
            for w in self._webhooks.values()
        ]

    async def handle(
        self, webhook_id: str, payload: dict, headers: dict | None = None
    ) -> dict:
        webhook = self._webhooks.get(webhook_id)
        if not webhook or not webhook.enabled:
            return {"error": "Webhook not found or disabled"}
        # Verify signature if secret is set
        if webhook.secret and headers:
            signature = self._compute_signature(payload, webhook.secret)
            received = headers.get("x-webhook-signature", "")
            if signature != received:
                return {"error": "Invalid signature"}
        handler = self._handlers.get(webhook_id)
        if handler:
            return await handler(webhook, payload)
        logger.info("Webhook %s triggered (no handler)", webhook_id)
        return {"status": "received", "webhook_id": webhook_id}

    @staticmethod
    def _compute_signature(payload: dict, secret: str) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    @property
    def count(self) -> int:
        return len(self._webhooks)


class CronManager:
    """Manages scheduled cron jobs with SQLite persistence and full 5-field cron parsing."""

    def __init__(self, db_path: str = "", default_handler: Callable | None = None):
        self._jobs: dict[str, CronJob] = {}
        self._handlers: dict[str, Callable] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._db_path = db_path
        self._conn = None
        self._default_handler: Callable | None = default_handler
        if db_path:
            self._init_db(db_path)
            self._load_jobs()

    def _init_db(self, db_path: str) -> None:
        import sqlite3

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                expression TEXT NOT NULL,
                graph_id TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                last_run REAL DEFAULT 0,
                next_run REAL DEFAULT 0,
                created_at REAL DEFAULT 0,
                input_data TEXT DEFAULT '',
                max_retries INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                one_shot INTEGER DEFAULT 0
            )
        """)
        # #141 priority-3: 老库无 one_shot 列, ALTER 兜底迁移 (CREATE IF NOT EXISTS 不会加列).
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(cron_jobs)").fetchall()}
        if "one_shot" not in cols:
            self._conn.execute("ALTER TABLE cron_jobs ADD COLUMN one_shot INTEGER DEFAULT 0")
            logger.info("CronManager migrated cron_jobs: added one_shot column")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cron_executions (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                started_at REAL DEFAULT 0,
                finished_at REAL DEFAULT 0,
                status TEXT DEFAULT '',
                error TEXT DEFAULT '',
                result_preview TEXT DEFAULT ''
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_exec_job ON cron_executions(job_id)"
        )
        self._conn.commit()
        logger.info("CronManager DB initialized: %s", db_path)

    def _load_jobs(self) -> None:
        if not self._conn:
            return
        rows = self._conn.execute("SELECT * FROM cron_jobs").fetchall()
        for row in rows:
            job = CronJob(
                id=row[0],
                name=row[1],
                expression=row[2],
                graph_id=row[3],
                enabled=bool(row[4]),
                last_run=row[5],
                next_run=row[6],
                created_at=row[7],
                input_data=row[8],
                max_retries=row[9],
                retry_count=row[10],
                one_shot=bool(row[11]) if len(row) > 11 else False,
            )
            self._jobs[job.id] = job
        logger.info("Loaded %d cron jobs from DB", len(self._jobs))

    def _save_job(self, job: CronJob) -> None:
        if not self._conn:
            return
        self._conn.execute(
            """INSERT OR REPLACE INTO cron_jobs
               (id, name, expression, graph_id, enabled, last_run, next_run, created_at, input_data, max_retries, retry_count, one_shot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id,
                job.name,
                job.expression,
                job.graph_id,
                int(job.enabled),
                job.last_run,
                job.next_run,
                job.created_at,
                job.input_data,
                job.max_retries,
                job.retry_count,
                int(job.one_shot),
            ),
        )
        self._conn.commit()

    def _delete_job(self, job_id: str) -> None:
        if not self._conn:
            return
        self._conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        self._conn.commit()

    EXECUTION_TTL_SECONDS = 7 * 24 * 3600

    def _save_execution(self, exe: CronExecution) -> None:
        if not self._conn:
            return
        self._conn.execute(
            """INSERT OR REPLACE INTO cron_executions
               (id, job_id, started_at, finished_at, status, error, result_preview)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                exe.id,
                exe.job_id,
                exe.started_at,
                exe.finished_at,
                exe.status,
                exe.error,
                exe.result_preview,
            ),
        )
        cutoff = time.time() - self.EXECUTION_TTL_SECONDS
        result = self._conn.execute(
            "DELETE FROM cron_executions WHERE started_at < ?", (cutoff,)
        )
        if result.rowcount:
            logger.info("Cleaned up %d expired cron execution records", result.rowcount)
        self._conn.commit()

    def register(self, job: CronJob, handler: Callable | None = None) -> None:
        job.next_run = self._compute_next_run(job.expression)
        if not job.created_at:
            job.created_at = time.time()
        self._jobs[job.id] = job
        if handler:
            self._handlers[job.id] = handler
        self._save_job(job)
        logger.info("Cron job registered: %s (%s)", job.id, job.expression)

    async def aregister(self, job: CronJob, handler: Callable | None = None) -> None:
        job.next_run = self._compute_next_run(job.expression)
        if not job.created_at:
            job.created_at = time.time()
        self._jobs[job.id] = job
        if handler:
            self._handlers[job.id] = handler
        await asyncio.to_thread(self._save_job, job)
        logger.info("Cron job registered (async): %s (%s)", job.id, job.expression)

    def register_once(
        self,
        run_at: float,
        graph_id: str,
        input_data: str = "",
        job_id: str = "",
        name: str = "",
        handler: Callable | None = None,
    ) -> CronJob:
        # #141 priority-3: 一次性定时任务 (run_at). 不走 cron 表达式, next_run=run_at,
        # one_shot=True 触发后 _run_loop 自动注销. run_at 已过期则立即排队下一 tick 触发.
        if not job_id:
            job_id = f"once_{int(time.time() * 1000)}_{id(self) % 100000}"
        job = CronJob(
            id=job_id,
            name=name or job_id,
            expression="@once",
            graph_id=graph_id,
            input_data=input_data,
            next_run=float(run_at) if run_at > 0 else time.time(),
            created_at=time.time(),
            one_shot=True,
        )
        self._jobs[job_id] = job
        if handler:
            self._handlers[job_id] = handler
        self._save_job(job)
        logger.info("One-shot cron job registered: %s run_at=%.0f", job_id, job.next_run)
        return job

    async def aregister_once(
        self,
        run_at: float,
        graph_id: str,
        input_data: str = "",
        job_id: str = "",
        name: str = "",
        handler: Callable | None = None,
    ) -> CronJob:
        job = self.register_once(
            run_at, graph_id, input_data, job_id, name, handler
        )
        await asyncio.to_thread(self._save_job, job)
        return job

    def unregister(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._handlers.pop(job_id, None)
        self._delete_job(job_id)
        logger.info("Cron job unregistered: %s", job_id)

    async def aunregister(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._handlers.pop(job_id, None)
        await asyncio.to_thread(self._delete_job, job_id)
        logger.info("Cron job unregistered (async): %s", job_id)

    def get(self, job_id: str) -> CronJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.to_dict() for j in self._jobs.values()]

    def list_executions(self, job_id: str = "", limit: int = 20) -> list[dict]:
        if not self._conn:
            return []
        if job_id:
            rows = self._conn.execute(
                "SELECT * FROM cron_executions WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM cron_executions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            results.append(
                CronExecution(
                    id=row[0],
                    job_id=row[1],
                    started_at=row[2],
                    finished_at=row[3],
                    status=row[4],
                    error=row[5],
                    result_preview=row[6],
                ).to_dict()
            )
        return results

    async def alist_executions(self, job_id: str = "", limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(self.list_executions, job_id, limit)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("CronManager started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("CronManager stopped")

    async def _run_loop(self) -> None:
        while self._running:
            now = time.time()
            for job in list(self._jobs.values()):
                if not job.enabled:
                    continue
                if job.next_run > 0 and now >= job.next_run:
                    exe = CronExecution(
                        id=f"exe_{job.id}_{int(now)}",
                        job_id=job.id,
                        started_at=now,
                    )
                    handler = self._handlers.get(job.id) or self._default_handler
                    if handler:
                        try:
                            result = await handler(job)
                            exe.status = "success"
                            exe.result_preview = str(result)[:200] if result else ""
                            job.retry_count = 0
                        except Exception as e:
                            logger.error("Cron job %s failed: %s", job.id, e)
                            exe.status = "failed"
                            exe.error = str(e)[:500]
                            if job.retry_count < job.max_retries:
                                job.retry_count += 1
                                logger.info(
                                    "Cron job %s retry %d/%d",
                                    job.id,
                                    job.retry_count,
                                    job.max_retries,
                                )
                            else:
                                job.retry_count = 0
                    else:
                        exe.status = "no_handler"
                    exe.finished_at = time.time()
                    await asyncio.to_thread(self._save_execution, exe)
                    job.last_run = now
                    if job.one_shot:
                        # #141 priority-3: 一次性任务触发后自动注销, 不再排下次.
                        logger.info("One-shot cron job %s fired and auto-unregistered", job.id)
                        self._jobs.pop(job.id, None)
                        self._handlers.pop(job.id, None)
                        await asyncio.to_thread(self._delete_job, job.id)
                    else:
                        job.next_run = self._compute_next_run(job.expression)
                        await asyncio.to_thread(self._save_job, job)
            await asyncio.sleep(15)

    def _compute_next_run(self, expression: str) -> float:
        from datetime import datetime, timedelta

        parts = expression.strip().split()
        if len(parts) != 5:
            logger.warning("Invalid cron expression (need 5 fields): %r", expression)
            return time.time() + 3600

        try:
            minute_spec, hour_spec, dom_spec, month_spec, dow_spec = parts
            now = time.time()
            current = datetime.fromtimestamp(now)
            candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)

            max_iterations = 525600
            iterations = 0
            while iterations < max_iterations:
                iterations += 1

                if not self._matches_field(month_spec, candidate.month, 1, 12):
                    candidate = candidate.replace(day=1, hour=0, minute=0) + timedelta(
                        days=32
                    )
                    candidate = candidate.replace(day=1, hour=0, minute=0)
                    continue

                dom_match = self._matches_field(dom_spec, candidate.day, 1, 31)
                dow_match = self._matches_field(
                    dow_spec, candidate.isoweekday() % 7, 0, 6
                )
                if dom_spec != "*" and dow_spec != "*":
                    day_ok = dom_match or dow_match
                else:
                    day_ok = dom_match and dow_match

                if not day_ok:
                    candidate += timedelta(days=1)
                    candidate = candidate.replace(hour=0, minute=0)
                    continue
                if not self._matches_field(hour_spec, candidate.hour, 0, 23):
                    candidate += timedelta(hours=1)
                    candidate = candidate.replace(minute=0)
                    continue
                if not self._matches_field(minute_spec, candidate.minute, 0, 59):
                    candidate += timedelta(minutes=1)
                    continue
                return candidate.timestamp()

            logger.warning(
                "Cron expression %r: no match within %d iterations",
                expression,
                max_iterations,
            )
            return time.time() + 3600
        except Exception as e:
            logger.error("Cron parse error for %r: %s", expression, e)
            return time.time() + 3600

    @staticmethod
    def _matches_field(spec: str, value: int, min_val: int, max_val: int) -> bool:
        if spec == "*":
            return True
        try:
            for part in spec.split(","):
                if "/" in part:
                    range_part, step_str = part.split("/", 1)
                    step = int(step_str)
                    if range_part == "*":
                        start = min_val
                    else:
                        start = int(range_part)
                    if value >= start and (value - start) % step == 0:
                        return True
                elif "-" in part:
                    low, high = part.split("-", 1)
                    if int(low) <= value <= int(high):
                        return True
                else:
                    if int(part) == value:
                        return True
        except (ValueError, ZeroDivisionError):
            logger.warning("Invalid cron field spec: %r", spec)
            return False
        return False

    @property
    def count(self) -> int:
        return len(self._jobs)

    def close(self) -> None:
        self.stop()
        if self._conn:
            self._conn.close()
            self._conn = None
