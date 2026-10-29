"""Tests for issue #128 — 经 fusion-gateway 部署时 daemon 用 gateway client key 鉴权.

Runners: pytest tests/test_issue_128_gateway_auth.py
API: DaemonServer._is_gateway_path / _read_gateway_api_key / _attach_mlx_api_key
Data schemas: fusion_core.config gateway_admin_key 单点解析
(FUSION_GATEWAY_API_KEY > ~/.fusion-mlx/settings.json shell.gateway_admin_key),
env vars.

User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer


def _make_daemon() -> DaemonServer:
    return DaemonServer(socket_path="/tmp/_nonexistent_gw.sock")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in (
        "FUSION_CLIENT_KEY",
        "FUSION_MLX_API_KEY",
        "FUSION_GATEWAY_API_KEY",
        "FUSION_GATEWAY_URL",
        "FUSION_MLX_PORT",
        "FUSION_PROFILE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


class TestIsGatewayPath:
    def test_direct_11434_is_not_gateway(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = _make_daemon()
        assert d._is_gateway_path() is False

    def test_default_11432_is_gateway(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = _make_daemon()
        assert d._is_gateway_path() is True

    def test_explicit_gateway_url_is_gateway(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_GATEWAY_URL", "http://127.0.0.1:11432/v1")
        d = _make_daemon()
        assert d._is_gateway_path() is True


class TestReadGatewayApiKey:
    def test_env_var_first(self, monkeypatch, tmp_path, clean_env):
        settings = tmp_path / ".fusion-mlx" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"shell": {"gateway_admin_key": "settings-key"}}')
        monkeypatch.setenv("FUSION_GATEWAY_API_KEY", "env-key")
        d = _make_daemon()
        assert d._read_gateway_api_key() == "env-key"

    def test_settings_shell_gateway_admin_key_fallback(self, monkeypatch, tmp_path, clean_env):
        settings = tmp_path / ".fusion-mlx" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"shell": {"gateway_admin_key": "fg-admin-key"}}')
        d = _make_daemon()
        assert d._read_gateway_api_key() == "fg-admin-key"

    def test_missing_settings_returns_empty_in_dev(self, monkeypatch, clean_env):
        d = _make_daemon()
        assert d._read_gateway_api_key() == ""

    def test_missing_settings_raises_in_prod(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_PROFILE", "prod")
        d = _make_daemon()
        with pytest.raises(RuntimeError):
            d._read_gateway_api_key()

    def test_null_shell_key_returns_empty(self, monkeypatch, tmp_path, clean_env):
        settings = tmp_path / ".fusion-mlx" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"shell": {"gateway_admin_key": null}}')
        d = _make_daemon()
        assert d._read_gateway_api_key() == ""


class TestResolveMlxApiKeyForAttach:
    def test_gateway_path_uses_gateway_key(self, monkeypatch, tmp_path, clean_env):
        settings = tmp_path / ".fusion-mlx" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"shell": {"gateway_admin_key": "gw-master"}}')
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = _make_daemon()
        assert d._attach_mlx_api_key() == "gw-master"

    def test_direct_path_uses_mlx_key(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_MLX_API_KEY", "mlx-upstream-168")
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = _make_daemon()
        assert d._attach_mlx_api_key() == "mlx-upstream-168"

    def test_gateway_path_no_gateway_key_falls_back_mlx(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_MLX_API_KEY", "mlx-upstream-168")
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = _make_daemon()
        assert d._attach_mlx_api_key() == "mlx-upstream-168"
