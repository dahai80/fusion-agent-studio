"""Tests for fusion-mlx process manager."""

from __future__ import annotations


from server.process_manager import FusionMLXProcessManager


class TestFusionMLXProcessManager:
    def test_init_defaults(self):
        mgr = FusionMLXProcessManager()
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
        mgr = FusionMLXProcessManager(port=9999)
        assert mgr._health_check() is False

    def test_start_fails_gracefully(self):
        mgr = FusionMLXProcessManager(port=9998)
        result = mgr.start(wait_timeout=1.0)
        assert result is False

    def test_double_start_handles_gracefully(self):
        mgr = FusionMLXProcessManager(port=9997)
        assert mgr.start(wait_timeout=1.0) is False
        assert mgr.start(wait_timeout=1.0) is False

    def test_stop_when_not_running(self):
        mgr = FusionMLXProcessManager()
        mgr.stop()  # Should not raise

    def test_restart_handles_gracefully(self):
        mgr = FusionMLXProcessManager(port=9996)
        result = mgr.restart(wait_timeout=1.0)
        assert result is False

    def test_context_manager(self):
        with FusionMLXProcessManager(port=9995) as mgr:
            assert mgr is not None
        # Should have stopped after exit
