"""#633 regression tests: loop stall watchdog + embedded uvicorn signal hygiene."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

import pytest

from agent_runtime.loop_watchdog import LoopWatchdog, stall_threshold, watchdog_enabled


def _run_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    ready.set()
    loop.run_forever()


def _start_loop_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    t = threading.Thread(target=_run_loop, args=(loop, ready), daemon=True)
    t.start()
    assert ready.wait(5.0)
    return loop, t


def _stop_loop_thread(loop: asyncio.AbstractEventLoop, t: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5.0)
    loop.close()


def test_watchdog_detects_stall_and_dumps_stacks(caplog):
    loop, t = _start_loop_thread()
    blocker = threading.Event()
    watchdog = None
    try:
        # block the loop thread on a sync call — the wedge pattern of #633
        loop.call_soon_threadsafe(blocker.wait, 10.0)

        watchdog = LoopWatchdog(loop, threshold=0.2, check_interval=0.05)
        with caplog.at_level(logging.ERROR, logger="agent_runtime.loop_watchdog"):
            watchdog.start()
            deadline = time.monotonic() + 10.0
            dumped = False
            while time.monotonic() < deadline:
                if "event loop STALLED" in caplog.text:
                    dumped = True
                    break
                time.sleep(0.05)
            assert dumped, "watchdog did not report a stall within 10s"
            # dump must contain the loop thread's blocking stack and a task section
            assert "LOOP THREAD" in caplog.text
        blocker.set()
        # recovery must be logged once beats resume
        with caplog.at_level(logging.WARNING, logger="agent_runtime.loop_watchdog"):
            deadline = time.monotonic() + 10.0
            recovered = False
            while time.monotonic() < deadline:
                if "resumed after stall" in caplog.text:
                    recovered = True
                    break
                time.sleep(0.05)
            assert recovered, "watchdog did not log recovery after unblock"
    finally:
        blocker.set()
        if watchdog:
            watchdog.stop()
        _stop_loop_thread(loop, t)


def test_watchdog_beats_when_loop_healthy(caplog):
    loop, t = _start_loop_thread()
    watchdog = None
    try:
        watchdog = LoopWatchdog(loop, threshold=0.5, check_interval=0.05)
        watchdog.start()
        # healthy loop services beats: no stall within 1.5s (> 2 thresholds)
        time.sleep(1.5)
        assert not watchdog._stalled
        assert "event loop STALLED" not in caplog.text
    finally:
        if watchdog:
            watchdog.stop()
        _stop_loop_thread(loop, t)


def test_watchdog_env_toggles():
    import os

    old = os.environ.get("FUSION_LOOP_WATCHDOG")
    old_thr = os.environ.get("FUSION_LOOP_WATCHDOG_THRESHOLD")
    try:
        os.environ["FUSION_LOOP_WATCHDOG"] = "0"
        assert not watchdog_enabled()
        os.environ["FUSION_LOOP_WATCHDOG"] = "1"
        assert watchdog_enabled()
        os.environ["FUSION_LOOP_WATCHDOG_THRESHOLD"] = "1.5"
        assert stall_threshold() == pytest.approx(1.5)
        os.environ["FUSION_LOOP_WATCHDOG_THRESHOLD"] = "bogus"
        assert stall_threshold() > 0
        os.environ["FUSION_LOOP_WATCHDOG_THRESHOLD"] = "-3"
        assert stall_threshold() > 0
    finally:
        if old is None:
            os.environ.pop("FUSION_LOOP_WATCHDOG", None)
        else:
            os.environ["FUSION_LOOP_WATCHDOG"] = old
        if old_thr is None:
            os.environ.pop("FUSION_LOOP_WATCHDOG_THRESHOLD", None)
        else:
            os.environ["FUSION_LOOP_WATCHDOG_THRESHOLD"] = old_thr


def test_embedded_uvicorn_server_neutralizes_signal_capture():
    # #633: uvicorn.Server.capture_signals() hijacks SIGINT/SIGTERM while
    # serving; the embedded wrapper must swap it out for a no-op during
    # serve() and restore the original afterwards.
    from agent_runtime.daemon_server import _EmbeddedUvicornServer

    real_entered = []

    @contextlib.contextmanager
    def real_capture():
        real_entered.append("real")
        yield

    class _FakeInner:
        def __init__(self):
            self.capture_signals = real_capture
            self.during_serve = None

        async def serve(self):
            with self.capture_signals():
                self.during_serve = "served"

    wrapper = _EmbeddedUvicornServer.__new__(_EmbeddedUvicornServer)
    fake = _FakeInner()
    wrapper._server = fake
    asyncio.run(wrapper.serve())

    assert fake.during_serve == "served"
    assert real_entered == [], "real uvicorn capture_signals must not run"
    assert fake.capture_signals is real_capture, "original capture_signals must be restored"


def test_shell_registry_loop_error_logs_exception_type(caplog, monkeypatch):
    # #633: empty str(exc) made heartbeat failures undiagnosable — the loop
    # error log must carry the exception class name
    from agent_runtime import shell_registry

    class _FailingClient:
        async def post(self, *a, **k):
            raise RuntimeError("boom")

        async def get(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        shell_registry,
        "build_registration_payload",
        lambda manifest: {"upstream": "http://127.0.0.1:1", "api_key": None},
    )

    async def _scenario():
        task = asyncio.create_task(
            shell_registry._registry_loop_body(
                _FailingClient(), {"id": "fusion-agent-studio"}, interval=0.01
            )
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any("loop error" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.WARNING, logger="agent_runtime.shell_registry"):
        asyncio.run(_scenario())

    assert any(
        "loop error" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records
    )
