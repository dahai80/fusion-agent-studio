"""Issue #225 — MLX inference-pool state (leases/LRU/TTL) surfaced via mlx.status / mlx.pool.

The daemon's _handle_mlx_status previously returned only {running, port, models, pid}.
Issue #225 asks the daemon to surface the fusion-mlx engine-pool lifecycle (per-model
LRU last_used, loaded/loading state, pinned, sizes, idle/TTL-remaining, memory pressure)
so Fusion Studio can render real-time pool visibility.

This test pins:
  1. mlx.status now carries a `pool` key (None when MLX not running).
  2. mlx.pool RPC is registered and returns {running, pool}.
  3. _fetch_mlx_pool_state merges /admin/api/models + /admin/api/stats into the
     pool.loaded[] shape the client consumes, including TTL-remaining + idle_seconds
     from stats, and the pending_upstream marker for the still-unexposed lease refcount.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    # Replaces httpx.AsyncClient: records request URLs and returns preset responses.
    # Closes issue #225 pool-fetch logic, no real MLX runtime needed.
    responses: dict[str, _FakeResp] = {}
    requested: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kwargs):
        type(self).requested.append(url)
        resp = type(self).responses.get(url)
        if resp is None:
            return _FakeResp(404, {})
        return resp


def _seed_fake_httpx(monkeypatch, models_payload, stats_payload):
    _FakeAsyncClient.responses = {
        "http://127.0.0.1:11434/admin/api/models": _FakeResp(200, models_payload),
        "http://127.0.0.1:11434/admin/api/stats": _FakeResp(200, stats_payload),
    }
    _FakeAsyncClient.requested = []
    import httpx as _real_httpx

    # Inside _fetch_mlx_pool_state `import httpx` yields the module object; patch the
    # AsyncClient attribute.
    monkeypatch.setattr(_real_httpx, "AsyncClient", _FakeAsyncClient)


def _sample_models_payload() -> dict:
    return {
        "models": [
            {
                "id": "qwen-2.5",
                "loaded": True,
                "is_loading": False,
                "loading_started_at": None,
                "last_access": 1740000000.0,
                "pinned": False,
                "estimated_size": 4000000000,
                "actual_size": 4200000000,
                "engine_type": "batched",
                "model_type": "llm",
            },
            {
                "id": "bge-m3",
                "loaded": False,
                "is_loading": False,
                "loading_started_at": None,
                "last_access": None,
                "pinned": True,
                "estimated_size": 2300000000,
                "actual_size": 0,
                "engine_type": "embedding",
                "model_type": "embedding",
            },
        ]
    }


def _sample_stats_payload() -> dict:
    return {
        "active_models": {
            "model_memory_used": 4200000000,
            "model_memory_max": 113000000000,
            "memory_pressure": {
                "pressure_level": "ok",
                "current_bytes": 4200000000,
                "soft_bytes": 80000000000,
                "hard_bytes": 110000000000,
            },
            "models": [
                {
                    "id": "qwen-2.5",
                    "active_requests": 1,
                    "waiting_requests": 2,
                    "idle_seconds": 3.0,
                    "ttl_remaining_seconds": 297,
                    "is_loading": False,
                    "loading_elapsed_seconds": None,
                    "loading_estimated_seconds": None,
                    "loading_remaining_seconds_estimate": None,
                    "prefilling": False,
                    "generating": True,
                },
            ],
        }
    }


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store_db(tmp_path):
    yield str(tmp_path / "test_store.db")


@pytest.fixture
async def daemon(socket_path, store_db, monkeypatch):
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )

    # #419: mlx.status now probes engine health when the daemon owns no
    # process handle — a locally running engine would flip running=true and
    # fetch a real pool. Force the "engine unreachable" path for this suite.
    async def _no_engine():
        return False

    d._check_mlx_health = _no_engine
    await d.start()
    yield d
    await d.stop()


async def _rpc_call(socket_path, method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


class TestIssue225MlxPoolState:
    @pytest.mark.asyncio
    async def test_mlx_status_has_pool_key_not_running(self, daemon):
        # MLX not running (daemon fixture has no _mlx_process) -> pool must be None.
        resp = await _rpc_call(daemon.socket_path, "mlx.status")
        result = resp["result"]
        assert "pool" in result
        assert result["pool"] is None
        assert result["running"] is False

    @pytest.mark.asyncio
    async def test_mlx_pool_rpc_registered_and_shape(self, daemon):
        # mlx.pool RPC exists and returns a {running, pool} structure.
        resp = await _rpc_call(daemon.socket_path, "mlx.pool")
        assert resp["jsonrpc"] == "2.0"
        result = resp["result"]
        assert "running" in result
        assert "pool" in result
        # When not running pool=None, no exception (RPC route reachable).
        assert result["pool"] is None

    @pytest.mark.asyncio
    async def test_fetch_pool_state_merges_models_and_stats(self, monkeypatch):
        # Direct unit test of _fetch_mlx_pool_state: merges /admin/api/models + /admin/api/stats.
        _seed_fake_httpx(monkeypatch, _sample_models_payload(), _sample_stats_payload())
        d = DaemonServer(
            socket_path="/tmp/_nonexistent_pool.sock",
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=":memory:",
        )
        pool = await d._fetch_mlx_pool_state()
        assert pool is not None
        assert pool["loaded_count"] == 1  # only qwen-2.5 is loaded=True
        assert len(pool["loaded"]) == 2  # all registered models are listed

        by_id = {e["id"]: e for e in pool["loaded"]}
        qwen = by_id["qwen-2.5"]
        # Fields sourced from /admin/api/models.
        assert qwen["loaded"] is True
        assert qwen["last_used"] == 1740000000.0  # LRU timestamp
        assert qwen["pinned"] is False
        assert qwen["estimated_size"] == 4000000000
        assert qwen["actual_size"] == 4200000000
        assert qwen["engine_type"] == "batched"
        # Fields sourced from /admin/api/stats (merged).
        assert qwen["active_requests"] == 1
        assert qwen["waiting_requests"] == 2
        assert qwen["idle_seconds"] == 3.0
        assert qwen["ttl_remaining_seconds"] == 297
        assert qwen["generating"] is True
        assert qwen["prefilling"] is False

        bge = by_id["bge-m3"]
        assert bge["loaded"] is False
        assert bge["pinned"] is True
        # bge-m3 is not in stats active_models.models (not loaded) -> merged fields absent.
        assert "active_requests" not in bge

        # Memory water level.
        mem = pool["memory"]
        assert mem["model_memory_used"] == 4200000000
        assert mem["model_memory_max"] == 113000000000
        assert mem["pressure_level"] == "ok"
        assert mem["current_bytes"] == 4200000000

        # lease refcount not exposed upstream -> flagged pending fusion-mlx#647.
        assert pool["pending_upstream"] == ["lease_refcount"]

    @pytest.mark.asyncio
    async def test_fetch_pool_state_resilient_to_admin_401(self, monkeypatch):
        # Admin auth failure (401) -> pool does not raise, returns the retrievable parts + warning.
        import httpx as _real_httpx

        _FakeAsyncClient.responses = {
            "http://127.0.0.1:11434/admin/api/models": _FakeResp(401, {}),
            "http://127.0.0.1:11434/admin/api/stats": _FakeResp(401, {}),
        }
        _FakeAsyncClient.requested = []
        monkeypatch.setattr(_real_httpx, "AsyncClient", _FakeAsyncClient)
        d = DaemonServer(
            socket_path="/tmp/_nonexistent_pool2.sock",
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=":memory:",
        )
        pool = await d._fetch_mlx_pool_state()
        # Both endpoints fail but a structure is still returned (loaded empty, memory
        # empty), not None (None means connection failure).
        assert pool is not None
        assert pool["loaded"] == []
        assert pool["loaded_count"] == 0
        assert pool["memory"] == {}

    @pytest.mark.asyncio
    async def test_fetch_pool_state_partial_stats_failure(self, monkeypatch):
        # models succeeds + stats 404 -> loaded still has models, memory empty, no crash.
        import httpx as _real_httpx

        _FakeAsyncClient.responses = {
            "http://127.0.0.1:11434/admin/api/models": _FakeResp(200, _sample_models_payload()),
            "http://127.0.0.1:11434/admin/api/stats": _FakeResp(404, {}),
        }
        _FakeAsyncClient.requested = []
        monkeypatch.setattr(_real_httpx, "AsyncClient", _FakeAsyncClient)
        d = DaemonServer(
            socket_path="/tmp/_nonexistent_pool3.sock",
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=":memory:",
        )
        pool = await d._fetch_mlx_pool_state()
        assert pool is not None
        assert len(pool["loaded"]) == 2
        # stats fails -> merged fields missing, but /admin/api/models base fields are present.
        by_id = {e["id"]: e for e in pool["loaded"]}
        assert by_id["qwen-2.5"]["loaded"] is True
        assert "active_requests" not in by_id["qwen-2.5"]
        assert pool["memory"] == {}

    @pytest.mark.asyncio
    async def test_resolve_mlx_server_root_direct_port(self, monkeypatch):
        # Defaults to direct 11434; FUSION_MLX_DIRECT_PORT can override.
        d = DaemonServer(
            socket_path="/tmp/_nonexistent_root.sock",
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=":memory:",
        )
        monkeypatch.delenv("FUSION_MLX_DIRECT_PORT", raising=False)
        assert d._resolve_mlx_server_root() == "http://127.0.0.1:11434"
        monkeypatch.setenv("FUSION_MLX_DIRECT_PORT", "11499")
        assert d._resolve_mlx_server_root() == "http://127.0.0.1:11499"
