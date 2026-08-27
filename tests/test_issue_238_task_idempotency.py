"""Tests for #238 task.submit idempotency_key dedup.

Runner: pytest tests/test_issue_238_task_idempotency.py
API: task.submit with idempotency_key -> dedup returns existing task_id + deduped=True.
TaskStore: partial UNIQUE INDEX WHERE idempotency_key != '' (empty keys don't conflict).

User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.task_store import TaskStore


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


# --- unit: store-level dedup ---


def test_submit_dedup_returns_existing_task(store):
    from agent_runtime.task_store import Task

    t1 = store.submit(Task(title="a", graph_id="g1", idempotency_key="key-1"))
    assert store.last_submit_deduped is False
    t2 = store.submit(Task(title="b", graph_id="g2", idempotency_key="key-1"))
    assert store.last_submit_deduped is True
    # same task_id, no new row
    assert t2.task_id == t1.task_id
    assert t2.title == "a"
    assert t2.graph_id == "g1"


def test_submit_different_keys_create_separate_tasks(store):
    from agent_runtime.task_store import Task

    t1 = store.submit(Task(title="a", idempotency_key="key-1"))
    t2 = store.submit(Task(title="b", idempotency_key="key-2"))
    assert store.last_submit_deduped is False
    assert t1.task_id != t2.task_id


def test_submit_empty_key_no_dedup(store):
    from agent_runtime.task_store import Task

    t1 = store.submit(Task(title="a"))
    t2 = store.submit(Task(title="b"))
    assert store.last_submit_deduped is False
    assert t1.task_id != t2.task_id


def test_submit_dedup_survives_reopen(store, tmp_path):
    from agent_runtime.task_store import Task

    store.submit(Task(title="persisted", graph_id="g1", idempotency_key="key-p"))
    store.close()
    reopened = TaskStore(db_path=str(tmp_path / "unit_tasks.db"))
    t2 = reopened.submit(Task(title="new", graph_id="g2", idempotency_key="key-p"))
    assert reopened.last_submit_deduped is True
    assert t2.title == "persisted"
    reopened.close()


def test_idempotency_key_roundtrips_in_to_dict(store):
    from agent_runtime.task_store import Task

    t = store.submit(Task(title="x", idempotency_key="round-trip"))
    d = t.to_dict()
    assert d["idempotency_key"] == "round-trip"


# --- integration: RPC task.submit deduped flag ---


@pytest.mark.asyncio
async def test_rpc_submit_deduped_flag(daemon, socket_path):
    params1 = {
        "title": "first",
        "graph_id": "g1",
        "idempotency_key": "rpc-key-1",
    }
    r1 = await _rpc_call(socket_path, "task.submit", params1)
    assert r1["result"]["status"] == "ok"
    task1 = r1["result"]["task"]
    assert task1["deduped"] is False
    assert task1["idempotency_key"] == "rpc-key-1"
    first_id = task1["task_id"]

    params2 = {
        "title": "second",
        "graph_id": "g2",
        "idempotency_key": "rpc-key-1",
    }
    r2 = await _rpc_call(socket_path, "task.submit", params2)
    task2 = r2["result"]["task"]
    assert task2["deduped"] is True
    assert task2["task_id"] == first_id
    # dedup returns the original task, not the second submission
    assert task2["title"] == "first"
    assert task2["graph_id"] == "g1"


@pytest.mark.asyncio
async def test_rpc_submit_no_key_no_dedup_flag(daemon, socket_path):
    r = await _rpc_call(socket_path, "task.submit", {"title": "no-key", "graph_id": "g1"})
    assert r["result"]["task"]["deduped"] is False
