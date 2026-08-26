"""审计 0825 Bundle4 回归 — P1 资源管控 + 调度.

R-10 chat_stream usage 透传 (client StreamChunk + gateway yield + runtime capture + token_budget).
R-9  LLM 并发信号量 (FUSION_LLM_CONCURRENCY 限实际 HTTP 调用并发).
R-2  cron 并行扇出 + 单作业超时 + retry 达上限 disabled (不 reset).
3M-5 cron UTC + zoneinfo (DST 间隙由 tz-aware datetime 处理).
R-3  task_store TTL reaper + opt-in 懒载.
3M-4 工作流并发背压 + 真取消 (_active_tasks 注册 current_task, cancel_run task.cancel()).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from agent_runtime.context import AgentContext
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.task_store import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
    TaskStore,
)
from agent_runtime.triggers import CronJob, CronManager, _cron_tz
from agent_runtime.workflow_engine import WorkflowEngine, WorkflowStatus
from server.fusion_mlx_client import StreamChunk


# R-10 占位
class TestR10StreamUsage:
    def test_stream_chunk_carries_usage(self):
        chunk = StreamChunk(usage={"prompt_tokens": 10, "completion_tokens": 5})
        assert chunk.usage == {"prompt_tokens": 10, "completion_tokens": 5}
        assert StreamChunk().usage is None

    @pytest.mark.asyncio
    async def test_gateway_chat_stream_forwards_usage(self):
        async def fake_stream(**kwargs):
            yield StreamChunk(delta_content="hello")
            yield StreamChunk(
                finish_reason="stop",
                usage={"prompt_tokens": 12, "completion_tokens": 7},
            )

        client = MagicMock()
        client.chat_stream = fake_stream
        gw = LLMGateway()
        gw.set_default_client(client)
        chunks = []
        async for c in gw.chat_stream(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(c)
        usage_chunks = [c for c in chunks if c.get("usage")]
        assert len(usage_chunks) == 1
        assert usage_chunks[0]["usage"]["completion_tokens"] == 7

    def test_ctx_add_message_usage_aggregates(self):
        ctx = AgentContext()
        ctx.add_message("assistant", "a", usage={"prompt_tokens": 3, "completion_tokens": 2})
        ctx.add_message("assistant", "b", usage={"prompt_tokens": 5, "completion_tokens": 4})
        agg = ctx.token_usage()
        assert agg["prompt_tokens"] == 8
        assert agg["completion_tokens"] == 6
        assert agg["total"] == 14


# R-9 占位
class TestR9LlmConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrent_calls(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_CONCURRENCY", "4")
        in_flight = 0
        peak = 0

        async def slow_chat(**kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            resp = MagicMock()
            resp.content = "ok"
            resp.tool_calls = []
            resp.finish_reason = "stop"
            resp.usage = {}
            return resp

        client = MagicMock()
        client.chat = slow_chat
        gw = LLMGateway()
        gw._llm_concurrency_limit = 4
        gw._llm_semaphore = None
        gw.set_default_client(client)
        # 用 _call_default_client 直接验证限流 (绕过 model 路由).
        tasks = [
            gw._call_default_client(messages=[{"role": "user", "content": "x"}]) for _ in range(12)
        ]
        await asyncio.gather(*tasks)
        assert peak <= 4, f"peak in-flight {peak} exceeded limit 4"

    @pytest.mark.asyncio
    async def test_zero_means_unlimited(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_CONCURRENCY", "0")
        gw = LLMGateway()
        gw._llm_concurrency_limit = None
        assert gw._get_llm_semaphore() is None


# R-2 cron 并行扇出 + 单作业超时 + retry 达上限 disabled
class TestR2CronParallel:
    @pytest.mark.asyncio
    async def test_due_jobs_run_concurrently(self, tmp_path):
        cm = CronManager(db_path=str(tmp_path / "cron.db"))
        start_times = []

        async def slow_handler(job):
            start_times.append(time.time())
            await asyncio.sleep(0.3)
            return "ok"

        for i in range(3):
            job = CronJob(
                id=f"j{i}",
                name=f"job{i}",
                expression="*/1 * * * *",
                next_run=time.time() - 1,
                enabled=True,
            )
            cm._jobs[job.id] = job
            cm._handlers[job.id] = slow_handler
        await asyncio.gather(*[cm._run_single_job(j) for j in list(cm._jobs.values())])
        assert len(start_times) == 3
        spread = max(start_times) - min(start_times)
        # 并行: 三个应近同时开始, 间隔远小于 0.3s 作业耗时.
        assert spread < 0.15, f"jobs not parallel, spread={spread:.3f}s"
        cm.close()

    @pytest.mark.asyncio
    async def test_per_job_timeout_records_failed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FUSION_CRON_JOB_TIMEOUT", "0.1")
        cm = CronManager(db_path=str(tmp_path / "cron.db"))

        async def hang_handler(job):
            await asyncio.sleep(1)
            return "should-not-reach"

        job = CronJob(
            id="hang",
            name="hang",
            expression="*/1 * * * *",
            next_run=time.time() - 1,
            enabled=True,
            max_retries=0,
        )
        cm._jobs[job.id] = job
        cm._handlers[job.id] = hang_handler
        await cm._run_single_job(job)
        # 超时后 retry 达上限 -> disabled, 执行记录 saved.
        assert job.enabled is False, "retry-exhausted job should be disabled, not reset"
        assert job.retry_count == 0, "max_retries=0 means no increment, immediate disable"
        cm.close()

    @pytest.mark.asyncio
    async def test_retry_exhaustion_disables_not_reset(self, tmp_path):
        cm = CronManager(db_path=str(tmp_path / "cron.db"))

        async def fail_handler(job):
            raise RuntimeError("boom")

        job = CronJob(
            id="fail",
            name="fail",
            expression="*/1 * * * *",
            next_run=time.time() - 1,
            enabled=True,
            max_retries=2,
            retry_count=2,
        )
        cm._jobs[job.id] = job
        cm._handlers[job.id] = fail_handler
        await cm._run_single_job(job)
        # 原bug: retry_count 达上限后 reset=0 -> 无限重试. 修: disabled 不 reset.
        assert job.enabled is False, "job must be disabled after exhausting retries"
        assert job.retry_count == 2, "retry_count must NOT reset to 0 (infinite-retry bug)"
        cm.close()


# 3M-5 cron UTC tz-aware
class Test3M5CronUtc:
    def test_cron_tz_default_utc(self):
        tz = _cron_tz()
        assert tz == timezone.utc

    def test_compute_next_run_tz_aware(self, tmp_path):
        cm = CronManager(db_path=str(tmp_path / "cron.db"))
        nxt = cm._compute_next_run("0 12 * * *")
        assert nxt > time.time()
        # 每日12:00 UTC -> next_run 落在 12:00 整点.
        dt = datetime.fromtimestamp(nxt, tz=timezone.utc)
        assert dt.minute == 0
        assert dt.hour == 12
        cm.close()

    def test_compute_next_run_invalid_returns_future(self, tmp_path):
        cm = CronManager(db_path=str(tmp_path / "cron.db"))
        nxt = cm._compute_next_run("not-a-cron")
        assert nxt > time.time()
        cm.close()


# R-3 task_store TTL reaper
class TestR3TaskTtl:
    def test_reap_deletes_old_done_keeps_inflight(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_TASK_TTL", "100")
        ts = TaskStore(db_path=str(tmp_path / "tasks.db"))
        now = time.time()

        old_done = Task(
            task_id="old_done",
            title="old",
            status=TASK_STATUS_COMPLETED,
            created_at=now - 1000,
            updated_at=now - 1000,
        )
        old_failed = Task(
            task_id="old_fail",
            title="fail",
            status=TASK_STATUS_FAILED,
            created_at=now - 1000,
            updated_at=now - 1000,
        )
        fresh_done = Task(
            task_id="fresh_done",
            title="fresh",
            status=TASK_STATUS_COMPLETED,
            created_at=now,
            updated_at=now,
        )
        running = Task(
            task_id="running",
            title="run",
            status=TASK_STATUS_RUNNING,
            created_at=now - 1000,
            updated_at=now - 1000,
        )
        pending = Task(
            task_id="pending",
            title="pend",
            status=TASK_STATUS_PENDING,
            created_at=now - 1000,
            updated_at=now - 1000,
        )
        for t in (old_done, old_failed, fresh_done, running, pending):
            ts._tasks[t.task_id] = t
            ts._save_task(t)

        reaped = ts.reap_expired()
        assert reaped == 2, f"should reap 2 old done/failed, got {reaped}"
        assert "old_done" not in ts._tasks
        assert "old_fail" not in ts._tasks
        # fresh done + 在途 (running/pending) 不删.
        assert "fresh_done" in ts._tasks
        assert "running" in ts._tasks
        assert "pending" in ts._tasks
        ts.close()

    def test_reap_ttl_zero_disables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FUSION_TASK_TTL", "0")
        ts = TaskStore(db_path=str(tmp_path / "tasks.db"))
        now = time.time()
        old = Task(
            task_id="old",
            title="old",
            status=TASK_STATUS_COMPLETED,
            created_at=now - 99999,
            updated_at=now - 99999,
        )
        ts._tasks[old.task_id] = old
        ts._save_task(old)
        assert ts.reap_expired() == 0, "ttl=0 means no reaping"
        ts.close()

    def test_lazy_load_no_preload(self, tmp_path, monkeypatch):
        # 先用 eager 模式写入一个任务.
        eager = TaskStore(db_path=str(tmp_path / "tasks.db"))
        eager.submit(Task(title="seed", status=TASK_STATUS_COMPLETED))
        eager.close()
        # 懒载模式启动不预载, get 按需查 DB.
        monkeypatch.setenv("FUSION_TASK_LAZY_LOAD", "1")
        lazy = TaskStore(db_path=str(tmp_path / "tasks.db"))
        assert len(lazy._tasks) == 0, "lazy-load must not preload into _tasks"
        results = lazy.list(limit=10)
        assert len(results) >= 1, "lazy list should query DB"
        lazy.close()


# 3M-4 工作流并发背压 + 真取消
class Test3M4WorkflowCancel:
    @pytest.mark.asyncio
    async def test_active_tasks_populated_and_cleared(self, tmp_path):
        we = WorkflowEngine()
        wf = we.create_workflow(
            "w",
            [
                {"name": "p1", "pattern": "pipeline", "agent_configs": []},
            ],
        )
        assert len(we._active_tasks) == 0
        run = await we.execute_workflow(wf.id, "hello")
        # 执行完后应清理.
        assert run.id not in we._active_tasks
        assert run.status == WorkflowStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_cancel_run_cancels_inflight_task(self, monkeypatch):
        # 限并发=1 + phase 阻塞在事件上, cancel_run 中途取消.
        monkeypatch.setenv("FUSION_WORKFLOW_CONCURRENCY", "1")
        we = WorkflowEngine()
        we._workflow_concurrency_limit = 1
        we._workflow_semaphore = None

        block = asyncio.Event()

        async def slow_phase(phase, current_input):
            await block.wait()
            return {"output": current_input}

        we._exec_pipeline = slow_phase
        wf = we.create_workflow(
            "w",
            [
                {"name": "p1", "pattern": "pipeline", "agent_configs": [{"name": "a"}]},
            ],
        )

        run_task = asyncio.create_task(we.execute_workflow(wf.id, "x"))
        # 等 run 注册进 _runs (active task 已写入).
        for _ in range(100):
            if we._runs:
                break
            await asyncio.sleep(0.01)
        run_id = next(iter(we._runs))
        # 确认活跃 task 已注册.
        assert run_id in we._active_tasks, "active task must be registered for cancel"
        we.cancel_run(run_id)
        run = await run_task
        assert run.status == WorkflowStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrent_workflows(self, monkeypatch):
        monkeypatch.setenv("FUSION_WORKFLOW_CONCURRENCY", "2")
        we = WorkflowEngine()
        we._workflow_concurrency_limit = 2
        we._workflow_semaphore = None

        in_flight = 0
        peak = 0

        async def counting_phase(phase, current_input):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return {"output": current_input}

        we._exec_pipeline = counting_phase
        wf = we.create_workflow(
            "w",
            [
                {"name": "p1", "pattern": "pipeline", "agent_configs": [{"name": "a"}]},
            ],
        )
        tasks = [we.execute_workflow(wf.id, "x") for _ in range(6)]
        await asyncio.gather(*tasks)
        assert peak <= 2, f"peak concurrent workflows {peak} exceeded limit 2"
