"""#646: parked-run recovery on quota/budget exhaustion (t3code pattern).

t3code UsageLimitRecoveryWorker semantics: a run that stops because its
provider quota / token budget is exhausted is PARKED, not failed — the
platform persists ``usage_limit_reset_at`` and a background worker
auto-resumes the run when the window resets. Parking is capped (24h):
past the cap the run fails honestly instead of zombie-parking forever.

Persistence: JSON file (default ~/.fusion-agent-studio/parked-runs.json,
override FUSION_PARKED_RUNS_PATH) so a daemon restart does not lose the
reset window. Content failures never park — only error_class usage_limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

PARK_CAP_SECONDS = 24 * 3600
DEFAULT_RESET_SECONDS = 3600


def parked_runs_path() -> str:
    return os.environ.get("FUSION_PARKED_RUNS_PATH") or os.path.expanduser(
        "~/.fusion-agent-studio/parked-runs.json"
    )


class ParkedRunStore:
    # Crash-safe-enough JSON persistence for parked run metadata: a lost
    # file only costs one auto-resume; state of truth stays in the run
    # registry / run history.
    def __init__(self, path: str | None = None):
        self.path = path or parked_runs_path()

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning("parked runs load failed path=%s err=%s", self.path, e)
            return {}

    def save(self, runs: dict) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(runs, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("parked runs save failed path=%s err=%s", self.path, e)

    def upsert(self, record: dict) -> None:
        runs = self.load()
        runs[record["run_id"]] = record
        self.save(runs)

    def remove(self, run_id: str) -> None:
        runs = self.load()
        if runs.pop(run_id, None) is not None:
            self.save(runs)


class ParkedRunRecovery:
    # Background worker: every interval, resume parked runs whose reset
    # window has passed; fail runs parked longer than the 24h cap honestly.
    # resume_fn(run) must re-launch execution (api layer rebuilds the graph
    # from run.resume_request); it runs as a fresh asyncio task.
    def __init__(
        self,
        registry,
        resume_fn,
        *,
        clock=time.time,
        interval: float = 30.0,
        cap_seconds: float = PARK_CAP_SECONDS,
        store: ParkedRunStore | None = None,
    ):
        self.registry = registry
        self.resume_fn = resume_fn
        self.clock = clock
        self.interval = interval
        self.cap_seconds = cap_seconds
        self.store = store or ParkedRunStore()
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = asyncio.Event()
            self._task = asyncio.create_task(self._loop())
            logger.info("parked-run recovery started interval=%.0fs", self.interval)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("parked-run recovery stopped")

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.warning("parked-run recovery tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def tick(self) -> dict:
        # One recovery pass: resume due runs, fail capped runs. Returns the
        # action summary; injectable clock makes it testable deterministically.
        now = self.clock()
        resumed: list[str] = []
        failed: list[str] = []
        for run in list(self.registry.all_runs()):
            if not getattr(run, "parked", False):
                continue
            reset_at = float(getattr(run, "parked_until", 0) or 0)
            parked_at = float(getattr(run, "parked_at", 0) or 0)
            if now - parked_at > self.cap_seconds:
                self._fail_honestly(run, now)
                failed.append(run.run_id)
                continue
            if reset_at and now >= reset_at:
                run.parked = False
                run.parked_until = 0.0
                self.store.remove(run.run_id)
                run.publish({"type": "resumed", "resumed_at": now})
                try:
                    run.task = asyncio.create_task(self.resume_fn(run))
                except Exception as e:
                    logger.warning("resume dispatch failed run=%s err=%s", run.run_id, e)
                resumed.append(run.run_id)
        if resumed or failed:
            logger.info("parked-run recovery tick resumed=%s failed=%s", resumed, failed)
        return {"resumed": resumed, "failed": failed}

    def _fail_honestly(self, run, now: float) -> None:
        # 24h overdue arm cap: quota windows longer than the cap are not
        # auto-resumable — the run ends failed with a clear reason, never
        # silently parked forever.
        run.parked = False
        run.parked_until = 0.0
        self.store.remove(run.run_id)
        run.publish(
            {
                "type": "error",
                "message": (
                    "parked over 24h cap on usage_limit — failing honestly; "
                    "raise the quota/budget or re-launch the run"
                ),
            }
        )
        run.done = True
        run.publish({"type": "done"})
        logger.warning("parked run failed at 24h cap run=%s", run.run_id)


def compute_reset_at(budget, now: float) -> float:
    # usage_limit_reset_at: day-cap exhaustion resets at the next budget day
    # roll; absolute-cap exhaustion has no natural window — default 1h retry
    # (the 24h park cap still bounds total parking).
    if budget is not None:
        day_reset = getattr(budget, "next_day_reset_at", None)
        if callable(day_reset):
            ts = day_reset()
            if ts:
                return float(ts)
    return now + DEFAULT_RESET_SECONDS
