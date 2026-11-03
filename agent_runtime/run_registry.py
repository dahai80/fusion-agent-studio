"""#553: managed SSE runs — background executor + replay buffer + cancel.

The graph execution runs in a background asyncio.Task decoupled from the
HTTP connection, so a dropped SSE client can reconnect with
resume_run_id + last_seq and replay exactly the missed frames (no
duplicates), and a run can be canceled while streaming.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

logger = logging.getLogger(__name__)

RUN_TTL_SECONDS = 300
MAX_RUNS = 50
MAX_BUFFER = 5000


class ManagedRun:
    def __init__(self, graph_id: str):
        self.run_id = "run-" + uuid.uuid4().hex[:12]
        self.graph_id = graph_id
        self.buffer: list[dict] = []
        self._next_seq = 0
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.cancelled = False
        self.done = False
        self.created_at = time.time()

    def publish(self, frame: dict):
        # monotonic seq — survives buffer trimming, keeps resume semantics sane
        frame["seq"] = self._next_seq
        self._next_seq += 1
        self.buffer.append(frame)
        if len(self.buffer) > MAX_BUFFER:
            logger.warning("run %s buffer exceeded %d, dropping oldest", self.run_id, MAX_BUFFER)
            self.buffer.pop(0)
        self.queue.put_nowait(frame)


class RunRegistry:
    def __init__(self):
        self._runs: dict[str, ManagedRun] = {}

    def create(self, graph_id: str) -> ManagedRun:
        self.reap()
        run = ManagedRun(graph_id)
        self._runs[run.run_id] = run
        logger.info("managed run created %s graph=%s", run.run_id, graph_id)
        return run

    def get(self, run_id: str) -> ManagedRun | None:
        return self._runs.get(run_id)

    def reap(self):
        now = time.time()
        expired = [
            rid
            for rid, r in self._runs.items()
            if r.done and now - r.created_at > RUN_TTL_SECONDS
        ]
        for rid in expired:
            del self._runs[rid]
        # hard cap: drop oldest finished runs
        if len(self._runs) > MAX_RUNS:
            finished = sorted(
                (r for r in self._runs.values() if r.done),
                key=lambda r: r.created_at,
            )
            for r in finished[: len(self._runs) - MAX_RUNS]:
                del self._runs[r.run_id]

    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if run is None:
            return False
        run.cancelled = True
        if run.task is not None and not run.task.done():
            run.task.cancel()
            logger.info("managed run canceled %s", run_id)
        return True
