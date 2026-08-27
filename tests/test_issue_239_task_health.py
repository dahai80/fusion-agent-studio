"""Tests for #239 task.health RPC queue-depth aggregation.

Runner: pytest tests/test_issue_239_task_health.py
API: task.health over UDS JSON-RPC -> {ok, pending_tasks, running_tasks, total_tasks, max_concurrency}.
TaskStore: count_by_status/total_count helpers; FUSION_TASK_CONCURRENCY env.

User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.task_store import (
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TaskStore,
)


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


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def daemon(socket_path, tmp_path):
    d = DaemonServer(
        socket_path=socket_path,
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "test_store.db"),
    )
    await d.start()
    d._task_store = TaskStore(db_path=str(tmp_path / "test_tasks.db"))
    yield d
    await d.stop()


@pytest.fixture
def store(tmp_path):
    s = TaskStore(db_path=str(tmp_path / "unit_tasks.db"))
    yield s
    s.close()


# --- unit: store count helpers ---


def test_count_by_status(store):
    from agent_runtime.task_store import Task

    store.submit(Task(title="p1", graph_id="g"))
    store.submit(Task(title="p2", graph_id="g"))
    assert store.count_by_status(TASK_STATUS_PENDING) == 2
    assert store.count_by_status(TASK_STATUS_RUNNING) == 0


def test_total_count(store):
    from agent_runtime.task_store import Task

    assert store.total_count() == 0
    store.submit(Task(title="a", graph_id="g"))
    store.submit(Task(title="b", graph_id="g"))
    assert store.total_count() == 2


def test_count_status_after_update(store):
    from agent_runtime.task_store import Task

    t = store.submit(Task(title="r", graph_id="g"))
    store.update_status(t.task_id, TASK_STATUS_RUNNING)
    assert store.count_by_status(TASK_STATUS_PENDING) == 0
    assert store.count_by_status(TASK_STATUS_RUNNING) == 1


# --- unit: concurrency env ---


def test_max_concurrency_default(monkeypatch):
    from agent_runtime.task_store import _task_max_concurrency

    monkeypatch.delenv("FUSION_TASK_CONCURRENCY", raising=False)
    assert _task_max_concurrency() == 5


def test_max_concurrency_env_override(monkeypatch):
    from agent_runtime.task_store import _task_max_concurrency

    monkeypatch.setenv("FUSION_TASK_CONCURRENCY", "12")
    assert _task_max_concurrency() == 12


def test_max_concurrency_invalid_falls_back(monkeypatch):
    from agent_runtime.task_store import _task_max_concurrency

    monkeypatch.setenv("FUSION_TASK_CONCURRENCY", "not-a-number")
    assert _task_max_concurrency() == 5


# --- integration: RPC task.health ---


@pytest.mark.asyncio
async def test_rpc_task_health_empty(daemon, socket_path):
    r = await _rpc_call(socket_path, "task.health", {})
    result = r["result"]
    assert result["ok"] is True
    assert result["pending_tasks"] == 0
    assert result["running_tasks"] == 0
    assert result["total_tasks"] == 0
    assert result["max_concurrency"] == 5


@pytest.mark.asyncio
async def test_rpc_task_health_after_submit(daemon, socket_path):
    await _rpc_call(socket_path, "task.submit", {"title": "p1", "graph_id": "g"})
    await _rpc_call(socket_path, "task.submit", {"title": "p2", "graph_id": "g"})
    r = await _rpc_call(socket_path, "task.health", {})
    result = r["result"]
    assert result["ok"] is True
    assert result["pending_tasks"] == 2
    assert result["total_tasks"] == 2
    assert result["running_tasks"] == 0
