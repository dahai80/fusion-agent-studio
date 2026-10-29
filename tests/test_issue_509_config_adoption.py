"""Tests for issue #509 — fusion_core.config single-point key resolution.

Runners: pytest tests/test_issue_509_config_adoption.py
API: FusionMLXClient.__init__ / ModelConfig.api_key / DaemonServer._attach_mlx_api_key
     / shell_registry._shell_admin_key / run_artifacts.snapshot_environment
Spec: #509 (config Phase 2.2 adoption — accept criterion b/c)
"""

from __future__ import annotations

import stat

import pytest

_KEY_ENV_VARS = (
    "FUSION_CLIENT_KEY",
    "FUSION_MLX_API_KEY",
    "MLX_API_KEY",
    "FUSION_ADMIN_KEY",
    "FUSION_PROFILE",
    "FUSION_GATEWAY_URL",
    "FUSION_GATEWAY_API_KEY",
    "FUSION_MLX_PORT",
)


@pytest.fixture
def clean_keys(monkeypatch, tmp_path):
    # HOME 隔离: gateway_admin_key/resolve_api_key 会读 ~/.fusion-mlx/settings.json,
    # 且 gateway.reconfigure RPC 会持久写 FUSION_GATEWAY_URL (进程级污染)。
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k in _KEY_ENV_VARS:
        monkeypatch.delenv(k, raising=False)


class TestFusionMLXClientResolution:
    def test_client_key_env_wins(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_CLIENT_KEY", "ck-1")
        from server.fusion_mlx_client import FusionMLXClient

        assert FusionMLXClient().api_key == "ck-1"

    def test_dev_anon_falls_back_local(self, clean_keys):
        from server.fusion_mlx_client import FusionMLXClient

        assert FusionMLXClient().api_key == "local"

    def test_explicit_empty_stays_anonymous(self, clean_keys):
        from server.fusion_mlx_client import FusionMLXClient

        c = FusionMLXClient(api_key="")
        assert c.api_key == ""

    def test_explicit_key_passthrough(self, clean_keys):
        from server.fusion_mlx_client import FusionMLXClient

        c = FusionMLXClient(api_key="explicit-key")
        assert c.api_key == "explicit-key"

    def test_prod_missing_key_raises(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        from server.fusion_mlx_client import FusionMLXClient

        with pytest.raises(RuntimeError):
            FusionMLXClient()

    def test_model_config_default_no_import_time_read(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_CLIENT_KEY", "ck-env")
        from agent_runtime.llm_gateway import ModelConfig

        cfg = ModelConfig()
        assert cfg.api_key == ""


class TestDaemonKeyResolution:
    def _make_daemon(self):
        from agent_runtime.daemon_server import DaemonServer

        return DaemonServer(socket_path="/tmp/_nonexistent_509.sock")

    def test_attach_old_name_gone(self):
        d = self._make_daemon()
        assert hasattr(d, "_attach_mlx_api_key")
        assert not hasattr(d, "_resolve_mlx_api_key_for_attach")

    def test_client_key_wins_direct_path(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_CLIENT_KEY", "ck-daemon")
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = self._make_daemon()
        assert d._attach_mlx_api_key() == "ck-daemon"

    def test_alias_still_honored(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_MLX_API_KEY", "alias-168")
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = self._make_daemon()
        assert d._attach_mlx_api_key() == "alias-168"

    def test_settings_json_fallback(self, monkeypatch, tmp_path, clean_keys):
        home = tmp_path / "home"
        mlx = home / ".fusion-mlx"
        mlx.mkdir(parents=True)
        (mlx / "settings.json").write_text('{"auth": {"api_key": "settings-key"}}')
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = self._make_daemon()
        assert d._attach_mlx_api_key() == "settings-key"

    def test_settings_json_chmod_hardened(self, monkeypatch, tmp_path, clean_keys):
        home = tmp_path
        mlx = home / ".fusion-mlx"
        mlx.mkdir(parents=True)
        p = mlx / "settings.json"
        p.write_text('{"auth": {"api_key": "settings-key"}}')
        p.chmod(0o644)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = self._make_daemon()
        assert d._attach_mlx_api_key() == "settings-key"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_gateway_path_prefers_gateway_key(self, monkeypatch, tmp_path, clean_keys):
        settings = tmp_path / ".fusion-mlx" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"shell": {"gateway_admin_key": "gw-master"}}')
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = self._make_daemon()
        assert d._attach_mlx_api_key() == "gw-master"


class TestShellRegistryAdminKey:
    def test_enabled_with_admin_key(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        monkeypatch.setenv("FUSION_ADMIN_KEY", "ak-1")
        from agent_runtime import shell_registry

        assert shell_registry.shell_registration_enabled() is True

    def test_dev_unset_disabled(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        from agent_runtime import shell_registry

        assert shell_registry.shell_registration_enabled() is False

    def test_prod_unset_disabled_no_raise(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        monkeypatch.setenv("FUSION_SHELL_GATEWAY_URL", "http://127.0.0.1:11432")
        from agent_runtime import shell_registry

        assert shell_registry.shell_registration_enabled() is False


class TestRunArtifactsEnv:
    def test_snapshot_records_client_key_presence(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_CLIENT_KEY", "ck-snap")
        from agent_runtime.run_artifacts import snapshot_environment

        snap = snapshot_environment()
        assert snap["env"]["FUSION_CLIENT_KEY"] == "set"

    def test_snapshot_legacy_key_not_recorded(self, monkeypatch, clean_keys):
        monkeypatch.setenv("FUSION_MLX_API_KEY", "legacy")
        from agent_runtime.run_artifacts import snapshot_environment

        snap = snapshot_environment()
        assert snap["env"].get("FUSION_MLX_API_KEY") is None
        assert snap["env"].get("FUSION_CLIENT_KEY") == ""
