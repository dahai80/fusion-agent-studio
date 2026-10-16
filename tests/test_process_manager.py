"""Tests for fusion-mlx process manager.

Hermetic: fusion_mlx IS installed in the shared venv, so an un-stubbed
`start()` really launches a fusion-mlx server. The old tests assumed
start-on-an-unused-port always fails — with a warm import cache the server
can become healthy within wait_timeout, flipping the assertion AND leaving
orphan `fusion-mlx-server` processes on ports 9995-9998 that poison every
later run. All start/stop paths are now driven by a stub Popen so no real
subprocess is spawned.
"""

from __future__ import annotations

import server.process_manager as pm
from server.process_manager import FusionMLXProcessManager


class _StubPopen:
    def __init__(self, *args, **kwargs):
        self.pid = 4242
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _stub_start_path(monkeypatch):
    monkeypatch.setattr(pm.subprocess, "Popen", _StubPopen)
    monkeypatch.setattr(FusionMLXProcessManager, "_health_check", lambda self: False)


class TestFusionMLXProcessManager:
    def test_init_defaults(self, monkeypatch):
        monkeypatch.delenv("FUSION_MLX_PORT", raising=False)
        import importlib

        importlib.reload(pm)
        mgr = pm.FusionMLXProcessManager()
        assert mgr.port == 11432
        assert mgr.model == ""
        assert mgr.host == "127.0.0.1"
        assert mgr.base_url == "http://127.0.0.1:11432/v1"

    def test_init_custom(self):
        mgr = FusionMLXProcessManager(
            port=8080,
            model="qwen3.5-9b",
            model_dir="/tmp/models",
            host="0.0.0.0",
            log_level="DEBUG",
            extra_args=["--cors-origins", "*"],
        )
        assert mgr.port == 8080
        assert mgr.model == "qwen3.5-9b"
        assert mgr.model_dir == "/tmp/models"
        assert mgr.host == "0.0.0.0"
        assert mgr.log_level == "DEBUG"
        assert mgr.extra_args == ["--cors-origins", "*"]

    def test_not_running_initially(self):
        mgr = FusionMLXProcessManager()
        assert mgr.is_running() is False

    def test_health_check_no_server(self):
        mgr = FusionMLXProcessManager(port=40499)
        assert mgr._health_check() is False

    def test_start_binary_missing_returns_false(self, monkeypatch):
        def _missing(*args, **kwargs):
            raise FileNotFoundError("fusion-mlx not installed")

        monkeypatch.setattr(pm.subprocess, "Popen", _missing)
        mgr = FusionMLXProcessManager(port=40498)
        assert mgr.start(wait_timeout=0.5) is False
        assert mgr.process is None

    def test_start_unhealthy_returns_false(self, monkeypatch):
        _stub_start_path(monkeypatch)
        mgr = FusionMLXProcessManager(port=40497)
        assert mgr.start(wait_timeout=0.5) is False
        assert mgr.process is None

    def test_double_start_handles_gracefully(self, monkeypatch):
        _stub_start_path(monkeypatch)
        mgr = FusionMLXProcessManager(port=40496)
        assert mgr.start(wait_timeout=0.5) is False
        assert mgr.start(wait_timeout=0.5) is False

    def test_stop_when_not_running(self):
        mgr = FusionMLXProcessManager()
        mgr.stop()  # Should not raise

    def test_stop_terminates_stub_process(self, monkeypatch):
        _stub_start_path(monkeypatch)
        mgr = FusionMLXProcessManager(port=40495)
        mgr.process = _StubPopen()
        mgr.stop()
        assert mgr.process is None

    def test_restart_handles_gracefully(self, monkeypatch):
        _stub_start_path(monkeypatch)
        mgr = FusionMLXProcessManager(port=40494)
        assert mgr.restart(wait_timeout=0.5) is False

    def test_context_manager(self, monkeypatch):
        monkeypatch.setattr(
            FusionMLXProcessManager, "start", lambda self, wait_timeout=30.0: False
        )
        with FusionMLXProcessManager(port=40493) as mgr:
            assert mgr is not None
        # Should have stopped after exit without raising
