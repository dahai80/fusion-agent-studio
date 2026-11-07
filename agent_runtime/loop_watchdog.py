"""#633: event-loop stall watchdog.

The daemon serves UDS RPC, cluster API (11457) and HTTP (11442) from ONE
asyncio loop. A sync call that blocks that loop (file IO, subprocess, lock
wait) wedges every surface at once — ports stay LISTENING but nothing is
serviced, and historically there was no trace left (issue #633 incident
2026-11-07: /health hung 18s+, registry heartbeat gaps, SIGTERM delivered
~50s late, zero diagnostics).

This watchdog runs in a plain thread and pokes the loop every
``check_interval`` via ``call_soon_threadsafe``. If the poke is not serviced
within ``stall_threshold`` the loop is stalled; the thread then dumps to
stderr (1) every thread's current stack — which shows the exact sync call
the loop is blocked in — and (2) every asyncio task's stack, without
needing root (py-spy/sample were unavailable on the incident host).

Env: FUSION_LOOP_WATCHDOG=0 disables; FUSION_LOOP_WATCHDOG_THRESHOLD (float
seconds, default 30) tunes detection latency. Dumps repeat every threshold
while the stall persists, and a recovery line is logged when beats resume.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

DEFAULT_STALL_THRESHOLD = 30.0
DEFAULT_CHECK_INTERVAL = 5.0


def watchdog_enabled() -> bool:
    return os.environ.get("FUSION_LOOP_WATCHDOG", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def stall_threshold() -> float:
    raw = os.environ.get("FUSION_LOOP_WATCHDOG_THRESHOLD", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_STALL_THRESHOLD
    if val <= 0:
        return DEFAULT_STALL_THRESHOLD
    return val


class LoopWatchdog:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        threshold: float | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ):
        self.loop = loop
        self.threshold = threshold if threshold is not None else stall_threshold()
        self.check_interval = check_interval
        self._beat = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stalled = False
        self._last_beat_ts = time.monotonic()
        # watchdog is constructed on the loop thread (run_forever) — record it
        # so dumps can label which blocked stack is the loop's
        self.loop_thread_id = threading.get_ident()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="fusion-loop-watchdog", daemon=True)
        self._thread.start()
        logger.info(
            "loop watchdog started (threshold=%.1fs check=%.1fs)",
            self.threshold,
            self.check_interval,
        )

    def stop(self) -> None:
        self._stop.set()
        self._beat.set()
        if self._thread is not None:
            self._thread.join(timeout=self.threshold + self.check_interval + 5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._beat.clear()
            try:
                self.loop.call_soon_threadsafe(self._on_beat)
            except RuntimeError:
                # loop closed during shutdown — normal exit path
                break
            if self._beat.wait(self.threshold):
                if self._stalled:
                    stalled_for = time.monotonic() - self._last_beat_ts
                    logger.warning("event loop resumed after stall of %.1fs", stalled_for)
                    self._stalled = False
                self._last_beat_ts = time.monotonic()
            else:
                self._stalled = True
                self._dump()
            # pace the poke loop; repeat dumps fall out of wait(timeout) timing
            self._stop.wait(self.check_interval)

    def _on_beat(self) -> None:
        self._beat.set()

    def _dump(self) -> None:
        try:
            text = self._render_dump()
        except Exception as e:
            logger.error("loop watchdog dump failed: %s", type(e).__name__)
            return
        logger.error("%s", text)

    def _render_dump(self) -> str:
        buf = io.StringIO()
        buf.write(
            "#633 event loop STALLED: no beat serviced for >%.1fs; dumping "
            "thread + task stacks for diagnosis\n" % self.threshold
        )
        loop_tid = self.loop_thread_id
        try:
            frames = sys._current_frames()
        except Exception:
            frames = {}
        for tid, frame in frames.items():
            label = "LOOP THREAD" if tid == loop_tid else f"thread tid={tid}"
            buf.write(f"--- {label} current stack ---\n")
            for line in traceback.format_stack(frame):
                buf.write(line)
        try:
            tasks = asyncio.all_tasks(self.loop)
        except RuntimeError:
            tasks = []
        for task in tasks:
            buf.write(f"--- task {task.get_name()} ---\n")
            try:
                task.print_stack(file=buf)
            except Exception as e:
                buf.write(f"(print_stack failed: {type(e).__name__})\n")
        return buf.getvalue()
