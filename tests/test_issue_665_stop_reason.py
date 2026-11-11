"""Tests for issue #665 - daemon graceful exits must be attributable.

28 unattributed AutoRestart events in 94 min: every graceful exit logged a
bare "Daemon stopped" with no signal name or stop reason, so supervisor
restart churn was undiagnosable. stop() now logs stop_reason (signal:NAME /
requested_stop / internal_error / unknown) and the signal handler names the
signal it received.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon(tmp_path):
    d = DaemonServer(socket_path=str(tmp_path / "test.sock"))
    d._agents_index_loaded = True
    return d


class TestStopReasonLogging:
    async def test_stop_logs_reason_default_unknown(self, daemon, caplog):
        with caplog.at_level(logging.INFO, logger="agent_runtime.daemon_server"):
            await daemon.stop()
        assert any(
            "Daemon stopped (stop_reason=unknown)" in r.getMessage() for r in caplog.records
        )

    async def test_stop_logs_signal_reason(self, daemon, caplog):
        daemon.stop_reason = "signal:SIGTERM"
        with caplog.at_level(logging.INFO, logger="agent_runtime.daemon_server"):
            await daemon.stop()
        assert any("stop_reason=signal:SIGTERM" in r.getMessage() for r in caplog.records)

    async def test_shutdown_rpc_records_requested_stop(self, daemon):
        await daemon._handle_daemon_shutdown({})
        assert daemon.stop_reason == "requested_stop"


class TestSignalPath:
    async def test_sigterm_run_forever_records_signal_reason(self, daemon, monkeypatch, caplog):
        # Full wiring: run_forever installs per-signal handlers; a real
        # SIGTERM must name the signal, set stop_reason, and exit cleanly.
        started = asyncio.Event()

        async def fake_start():
            started.set()

        async def fake_stop():
            return None

        monkeypatch.setattr(daemon, "_validate_config", lambda: None)
        monkeypatch.setattr(daemon, "start", fake_start)
        monkeypatch.setattr(daemon, "stop", fake_stop)

        with caplog.at_level(logging.INFO, logger="agent_runtime.daemon_server"):
            task = asyncio.create_task(daemon.run_forever())
            await asyncio.wait_for(started.wait(), timeout=5)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, timeout=5)

        assert daemon.stop_reason == "signal:SIGTERM"
        assert any("received SIGTERM" in r.getMessage() for r in caplog.records)

    async def test_sighup_also_handled(self, daemon, monkeypatch):
        async def fake_start():
            await asyncio.sleep(0)

        async def fake_stop():
            return None

        monkeypatch.setattr(daemon, "_validate_config", lambda: None)
        monkeypatch.setattr(daemon, "start", fake_start)
        monkeypatch.setattr(daemon, "stop", fake_stop)
        task = asyncio.create_task(daemon.run_forever())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(task, timeout=5)
        assert daemon.stop_reason == "signal:SIGHUP"
