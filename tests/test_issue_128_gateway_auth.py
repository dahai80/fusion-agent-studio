"""Tests for issue #128 — 经 fusion-gateway 部署时 daemon 用 gateway client key 鉴权.

Runners: pytest tests/test_issue_128_gateway_auth.py
API: DaemonServer._is_gateway_path / _read_gateway_api_key / _resolve_mlx_api_key_for_attach
Data schemas: gateway config.yaml (auth.master_key / auth.api_keys[].key), env vars.

User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer


def _make_daemon() -> DaemonServer:
    return DaemonServer(socket_path="/tmp/_nonexistent_gw.sock")


@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "FUSION_MLX_API_KEY",
        "FUSION_GATEWAY_API_KEY",
        "FUSION_GATEWAY_CONFIG",
        "FUSION_GATEWAY_URL",
        "FUSION_MLX_PORT",
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
        cfg = tmp_path / "gw.yaml"
        cfg.write_text("auth:\n  master_key: cfg-key\n")
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(cfg))
        monkeypatch.setenv("FUSION_GATEWAY_API_KEY", "env-key")
        d = _make_daemon()
        assert d._read_gateway_api_key() == "env-key"

    def test_master_key_from_config(self, monkeypatch, tmp_path, clean_env):
        cfg = tmp_path / "gw.yaml"
        cfg.write_text(
            "auth:\n"
            "  enabled: true\n"
            "  master_key: fg-master-key-change-me\n"
            "  api_keys:\n"
            "    - key: fg-demo-key-change-me\n"
            "      name: demo\n"
        )
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(cfg))
        d = _make_daemon()
        assert d._read_gateway_api_key() == "fg-master-key-change-me"

    def test_fallback_api_keys_first_when_no_master(self, monkeypatch, tmp_path, clean_env):
        cfg = tmp_path / "gw.yaml"
        cfg.write_text(
            "auth:\n"
            "  api_keys:\n"
            "    - key: fg-demo-key-change-me\n"
            "      name: demo\n"
            "    - key: fg-admin-key\n"
            "      name: admin\n"
        )
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(cfg))
        d = _make_daemon()
        assert d._read_gateway_api_key() == "fg-demo-key-change-me"

    def test_missing_config_returns_empty(self, monkeypatch, tmp_path, clean_env):
        monkeypatch.setenv(
            "FUSION_GATEWAY_CONFIG", str(tmp_path / "nope.yaml")
        )
        d = _make_daemon()
        assert d._read_gateway_api_key() == ""

    def test_empty_auth_returns_empty(self, monkeypatch, tmp_path, clean_env):
        cfg = tmp_path / "gw.yaml"
        cfg.write_text("auth:\n  enabled: false\n")
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(cfg))
        d = _make_daemon()
        assert d._read_gateway_api_key() == ""


class TestResolveMlxApiKeyForAttach:
    def test_gateway_path_uses_gateway_key(self, monkeypatch, tmp_path, clean_env):
        cfg = tmp_path / "gw.yaml"
        cfg.write_text("auth:\n  master_key: gw-master\n")
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(cfg))
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = _make_daemon()
        assert d._resolve_mlx_api_key_for_attach() == "gw-master"

    def test_direct_path_uses_mlx_key(self, monkeypatch, clean_env):
        monkeypatch.setenv("FUSION_MLX_API_KEY", "mlx-upstream-168")
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        d = _make_daemon()
        assert d._resolve_mlx_api_key_for_attach() == "mlx-upstream-168"

    def test_gateway_path_no_gateway_key_falls_back_mlx(self, monkeypatch, tmp_path, clean_env):
        monkeypatch.setenv("FUSION_GATEWAY_CONFIG", str(tmp_path / "nope.yaml"))
        monkeypatch.setenv("FUSION_MLX_API_KEY", "mlx-upstream-168")
        monkeypatch.setenv("FUSION_MLX_PORT", "11432")
        d = _make_daemon()
        assert d._resolve_mlx_api_key_for_attach() == "mlx-upstream-168"
