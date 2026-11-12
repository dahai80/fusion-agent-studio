"""E9: single-machine worker pool — daemon-derived workers, one queue and
one isolated workspace directory per worker, parked-session auto-resume
(#646 semantics). Local-first by construction: workers are in-process
asyncio loops over the same daemon, no cloud hop.

Concurrency contract:
- Session affinity: a session always resumes on the worker that owns it.
- Zero cross-talk: each worker gets its own workspace dir; executors must
  only write there (proven by tests/proof/prove_worker_pool.py).
- Parked: an executor may return a parked marker; the pool re-queues the
  session after reset_at with the same session_id (idempotent resume).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    session_id: str
    worker_id: str
    ok: bool
    output: str = ""
    error: str = ""
    parked: bool = False
    parked_until: float = 0.0
    attempts: int = 1
    workspace: str = ""


@dataclass
class _Job:
    session_id: str
    payload: dict = field(default_factory=dict)
    future: asyncio.Future | None = None
    attempts: int = 0
    enqueued_at: float = 0.0


class WorkerPool:
    def __init__(
        self,
        size: int = 4,
        executor=None,
        workspace_root: str = "",
        max_attempts: int = 5,
    ):
        if size < 1:
            raise ValueError("size must be >= 1")
        self.size = size
        self.executor = executor
        self.max_attempts = max_attempts
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.workers: list[dict] = []
        self._affinity: dict[str, str] = {}
        self._queues: list[asyncio.Queue] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False
        logger.info("worker pool created size=%d root=%s", size, self.workspace_root or "default")

    def _workspace(self, worker_id: str) -> Path:
        root = self.workspace_root or (Path.home() / ".fusion-agent-studio" / "workers")
        ws = root / worker_id
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self.size):
            worker_id = f"worker-{i}"
            q: asyncio.Queue = asyncio.Queue()
            self._queues.append(q)
            self.workers.append({"worker_id": worker_id, "workspace": str(self._workspace(worker_id))})
            self._tasks.append(asyncio.create_task(self._loop(i)))
        logger.info("worker pool started workers=%d", self.size)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.warning("worker %s stopped with error: %s", i, r)
        self._tasks.clear()
        self._queues.clear()
        self.workers.clear()
        logger.info("worker pool stopped")

    async def submit(self, session_id: str, payload: dict | None = None) -> WorkerResult:
        if not self._running:
            await self.start()
        job = _Job(session_id=session_id, payload=payload or {}, enqueued_at=time.time())
        job.future = asyncio.get_running_loop().create_future()
        self._enqueue(job)
        return await job.future

    def _enqueue(self, job: _Job) -> None:
        worker_idx = self._pick_worker(job.session_id)
        job.attempts += 1
        if job.attempts > self.max_attempts:
            if job.future and not job.future.done():
                job.future.set_result(
                    WorkerResult(
                        session_id=job.session_id,
                        worker_id=self.workers[worker_idx]["worker_id"],
                        ok=False,
                        error=f"max attempts exhausted ({self.max_attempts})",
                        attempts=job.attempts,
                    )
                )
            return
        self._queues[worker_idx].put_nowait(job)

    def _pick_worker(self, session_id: str) -> int:
        # Affinity: parked/resumed sessions stay on their owning worker.
        if session_id in self._affinity:
            return self._affinity[session_id]
        idx = min(range(len(self._queues)), key=lambda i: self._queues[i].qsize())
        self._affinity[session_id] = idx
        return idx

    async def _loop(self, idx: int) -> None:
        q = self._queues[idx]
        worker_id = f"worker-{idx}"
        workspace = self._workspace(worker_id)
        logger.info("worker %s loop started workspace=%s", worker_id, workspace)
        while True:
            job = await q.get()
            try:
                if job.future is None or job.future.done():
                    continue
                if self.executor is None:
                    job.future.set_result(
                        WorkerResult(
                            session_id=job.session_id,
                            worker_id=worker_id,
                            ok=False,
                            error="no executor configured",
                            workspace=str(workspace),
                        )
                    )
                    continue
                try:
                    out = await self.executor(job.payload, session_id=job.session_id, workspace=str(workspace))
                except Exception as e:
                    logger.warning("worker %s session %s failed: %s", worker_id, job.session_id, e)
                    job.future.set_result(
                        WorkerResult(
                            session_id=job.session_id,
                            worker_id=worker_id,
                            ok=False,
                            error=str(e),
                            attempts=job.attempts,
                            workspace=str(workspace),
                        )
                    )
                    continue
                if isinstance(out, dict) and out.get("parked"):
                    until = float(out.get("parked_until", 0.0) or 0.0)
                    delay = max(0.0, until - time.time())
                    logger.info(
                        "worker %s session %s parked until %.0f (%.1fs), will resume",
                        worker_id,
                        job.session_id,
                        until,
                        delay,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._enqueue(job)
                    continue
                output = out.get("output", "") if isinstance(out, dict) else str(out)
                job.future.set_result(
                    WorkerResult(
                        session_id=job.session_id,
                        worker_id=worker_id,
                        ok=True,
                        output=output,
                        attempts=job.attempts,
                        workspace=str(workspace),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("worker %s loop error: %s", worker_id, e)
            finally:
                q.task_done()

    def stats(self) -> dict:
        return {
            "size": self.size,
            "running": self._running,
            "workers": [
                {"worker_id": w["worker_id"], "workspace": w["workspace"], "queued": self._queues[i].qsize()}
                for i, w in enumerate(self.workers)
            ],
            "sessions_tracked": len(self._affinity),
        }
