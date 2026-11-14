"""#694: MLX client lazy re-attach.

A daemon started while fusion-mlx is down used to keep the LLM gateway
without a default client forever ("No available model" on every call until
a manual restart). Fix: lazy attach before each RPC dispatch + a background
re-attach worker with exponential backoff (5s -> 60s cap).
"""

from __future__ import annotations

import asyncio

from agent_runtime.daemon_server import DaemonServer


def _daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AUTH_ENFORCE", "0")
    return DaemonServer(
        socket_path=str(tmp_path / "t.sock"),
        store_path=str(tmp_path / "store.db"),
    )


def _health(daemon, up: bool, calls: list):
    async def _check():
        calls.append(up)
        return up

    daemon._check_mlx_health = _check


def _discovery(daemon):
    daemon._discover_mlx_model_id = lambda api_key: "stub-model"


async def test_lazy_attach_after_mlx_comes_up(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    _discovery(daemon)
    calls: list = []
    _health(daemon, up=False, calls=calls)

    assert daemon._gateway_attached() is False
    ok = await daemon._ensure_mlx_attached()
    assert ok is False
    # failed lazy probe scheduled the retry worker — cancel it for the test
    worker = daemon._mlx_reattach_task
    assert worker is not None and not worker.done()
    worker.cancel()

    # mlx comes up — next ensure attaches without any daemon restart
    _health(daemon, up=True, calls=calls)
    ok = await daemon._ensure_mlx_attached()
    assert ok is True
    assert daemon._gateway_attached() is True
    assert daemon._gateway._default_client is not None
    assert daemon._gateway._default_model == "stub-model"


async def test_worker_retries_until_attach(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    _discovery(daemon)
    state = {"up": False}

    async def _check():
        return state["up"]

    daemon._check_mlx_health = _check
    daemon._schedule_mlx_reattach("test")
    worker = daemon._mlx_reattach_task
    assert worker is not None

    # first probe fails (worker sleeps 5s after it) — flip up and the next
    # round attaches; bound the wait so a broken worker fails fast
    await asyncio.sleep(0.05)
    assert not worker.done()
    state["up"] = True
    done, _ = await asyncio.wait({worker}, timeout=10.0)
    assert worker in done and not worker.cancelled()
    assert daemon._gateway_attached() is True


async def test_worker_spawn_is_idempotent(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)

    async def _check():
        return False

    daemon._check_mlx_health = _check
    daemon._schedule_mlx_reattach("a")
    first = daemon._mlx_reattach_task
    daemon._schedule_mlx_reattach("b")
    assert daemon._mlx_reattach_task is first
    first.cancel()


async def test_attached_gateway_skips_probe(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    _discovery(daemon)
    calls: list = []
    _health(daemon, up=True, calls=calls)
    assert await daemon._ensure_mlx_attached() is True
    # normal path: no extra probe once attached
    assert await daemon._ensure_mlx_attached() is True
    assert len(calls) == 1


async def test_dispatch_lazily_attaches_before_handler(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)
    _discovery(daemon)
    state = {"up": True}

    async def _check():
        return state["up"]

    daemon._check_mlx_health = _check
    resp = await daemon._dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert resp.get("result") is not None
    assert daemon._gateway_attached() is True
    # daemon started while mlx was down then mlx recovered: dispatch retries
    daemon._detach_mlx_client()
    state["up"] = True
    resp = await daemon._dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
    assert resp.get("result") is not None
    assert daemon._gateway_attached() is True


async def test_degraded_probe_failure_schedules_worker_not_crash(tmp_path, monkeypatch):
    daemon = _daemon(tmp_path, monkeypatch)

    async def _check():
        return False

    daemon._check_mlx_health = _check
    resp = await daemon._dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert resp.get("result") is not None
    worker = daemon._mlx_reattach_task
    assert worker is not None and not worker.done()
    worker.cancel()
