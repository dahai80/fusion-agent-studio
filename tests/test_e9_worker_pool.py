"""E9: single-machine worker pool — affinity, parked resume, isolation."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_runtime.worker_pool import WorkerPool


class TestWorkerPool:
    async def test_all_workers_used_round_robin_load(self):
        seen = set()

        async def ex(payload, session_id="", workspace=""):
            seen.add(payload.get("worker_hint", ""))
            return {"output": "ok"}

        pool = WorkerPool(size=3, executor=ex)
        await pool.start()
        results = await asyncio.gather(*(pool.submit(f"s-{i}") for i in range(9)))
        await pool.stop()
        assert all(r.ok for r in results)
        assert len({r.worker_id for r in results}) == 3

    async def test_parked_session_resumes_same_worker(self):
        workspaces = {}

        async def ex(payload, session_id="", workspace=""):
            workspaces[session_id] = workspace
            if payload.get("attempt", 0) == 0:
                payload["attempt"] = 1
                return {"parked": True, "parked_until": time.time() + 0.05}
            return {"output": "resumed"}

        pool = WorkerPool(size=2, executor=ex)
        await pool.start()
        r = await pool.submit("p1")
        await pool.stop()
        assert r.ok and r.output == "resumed"
        assert r.attempts == 2
        # affinity: both attempts on the same workspace
        assert workspaces["p1"] == r.workspace

    async def test_no_executor_errors_cleanly(self):
        pool = WorkerPool(size=1)
        await pool.start()
        r = await pool.submit("s")
        await pool.stop()
        assert r.ok is False
        assert "no executor" in r.error

    async def test_executor_exception_is_result_not_raise(self):
        async def ex(payload, session_id="", workspace=""):
            raise RuntimeError("boom")

        pool = WorkerPool(size=1, executor=ex)
        await pool.start()
        r = await pool.submit("s")
        await pool.stop()
        assert r.ok is False and "boom" in r.error

    async def test_max_attempts_exhausted(self):
        async def ex(payload, session_id="", workspace=""):
            return {"parked": True, "parked_until": time.time()}

        pool = WorkerPool(size=1, executor=ex, max_attempts=3)
        await pool.start()
        r = await pool.submit("s")
        await pool.stop()
        assert r.ok is False
        assert "max attempts" in r.error
        assert r.attempts == 4

    def test_size_validation(self):
        with pytest.raises(ValueError):
            WorkerPool(size=0)

    async def test_stats_shape(self, tmp_path):
        async def ex(payload, session_id="", workspace=""):
            return {"output": "x"}

        pool = WorkerPool(size=2, executor=ex, workspace_root=str(tmp_path))
        await pool.start()
        s = pool.stats()
        await pool.stop()
        assert s["size"] == 2 and s["running"] is True
        assert len(s["workers"]) == 2
        assert all(w["workspace"].startswith(str(tmp_path)) for w in s["workers"])

    async def test_workspace_isolation(self, tmp_path):
        async def ex(payload, session_id="", workspace=""):
            (asyncio.get_event_loop(),)
            from pathlib import Path

            Path(workspace, f"{session_id}.txt").write_text(session_id)
            return {"output": workspace}

        pool = WorkerPool(size=2, executor=ex, workspace_root=str(tmp_path))
        await pool.start()
        results = await asyncio.gather(*(pool.submit(f"s{i}") for i in range(6)))
        await pool.stop()
        for r in results:
            assert r.workspace.startswith(str(tmp_path))
            assert r.workspace.split("/")[-1] == r.worker_id
