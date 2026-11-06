import json
import urllib.request

import pytest

import agent_runtime.daemon_server as ds
from agent_runtime.daemon_server import (
    DaemonServer,
    _normalized_model_tail,
    _parse_model_size_b,
)


def test_size_parse_quant_suffix_not_inflated():
    # #626: "4bit" quant token must not read as 4B params.
    assert _parse_model_size_b("mlx-community--Qwen3-0.6B-4bit") == 0.6
    assert _parse_model_size_b("Qwen3.8-27B-4bit") == 27.0
    assert _parse_model_size_b("qwen38-27b") == 27.0
    assert _parse_model_size_b("mlx-community--Qwen3.5-4B-MLX-4bit") == 4.0
    assert _parse_model_size_b("Qwen3.8-27B-8bit") == 27.0


def test_size_parse_unknown_is_zero():
    assert _parse_model_size_b("qwen3.7-max") == 0.0
    assert _parse_model_size_b("") == 0.0


def test_tail_normalizes_separator_styles():
    assert _normalized_model_tail("mlx-community--Qwen3.5-4B-MLX-4bit") == (
        _normalized_model_tail("mlx-community/Qwen3.5-4B-MLX-4bit")
    )


def test_read_mlx_own_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ds._read_mlx_own_default() == ""
    cfg = tmp_path / ".fusion-mlx"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"default_model": "mlx-community/Qwen3.5-4B-MLX-4bit"}))
    assert ds._read_mlx_own_default() == "mlx-community/Qwen3.5-4B-MLX-4bit"


CATALOG = [
    {"id": "mlx-community--Qwen3-0.6B-4bit"},
    {"id": "Qwen3.8-27B-4bit"},
    {"id": "qwen38-27b"},
    {"id": "mlx-community--Qwen3.5-4B-MLX-4bit"},
    {"id": "qwen3.7-max"},
    {"id": "Qwen-Image"},
    {"id": "Qwen2.5-VL"},
]


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"data": CATALOG}).encode()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ds, "_resolve_mlx_base_url", lambda: "http://fake-mlx/v1")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResp())
    return DaemonServer(socket_path=str(tmp_path / "d.sock"))


def test_fallback_picks_largest_qwen_not_catalog_order(daemon):
    # #626: old fallback returned the first qwen in catalog order (0.6B).
    assert daemon._discover_mlx_model_id("") == "Qwen3.8-27B-4bit"


def test_settings_default_wins_when_in_catalog(daemon, monkeypatch):
    monkeypatch.setattr(
        ds, "_read_mlx_own_default", lambda: "mlx-community/Qwen3.5-4B-MLX-4bit"
    )
    assert daemon._discover_mlx_model_id("") == "mlx-community--Qwen3.5-4B-MLX-4bit"


def test_image_and_vl_excluded(daemon):
    chosen = daemon._discover_mlx_model_id("")
    assert chosen not in ("Qwen-Image", "Qwen2.5-VL")


def test_warning_below_capability_floor(daemon, caplog):
    small = [{"id": "mlx-community--Qwen3-0.6B-4bit"}]

    class _SmallResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": small}).encode()

    daemon2 = daemon
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=5.0: _SmallResp()
    try:
        with caplog.at_level("WARNING"):
            chosen = daemon2._discover_mlx_model_id("")
    finally:
        urllib.request.urlopen = orig
    assert chosen == "mlx-community--Qwen3-0.6B-4bit"
    assert any("capability floor" in r.message for r in caplog.records)
