"""Tests for #141 task.* RPC + TaskStore persistence.

Runner: pytest tests/test_issue_141_task_rpc.py
API: task.submit/list/get/status/cancel/rerun RPC over UDS; TaskStore sqlite.
Data schemas: Task records (task_id/title/agent_id/graph_id/trigger/status/...).

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
    TASK_STATUS_CANCELED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TaskStore,
)


def _mlx_reachable() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.5):
            return True
    except OSError:
        return False


_MLX_UP = _mlx_reachable()


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
    # 注入临时 tasks.db, 不污染用户真实库.
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


class TestTaskStoreUnit:
    def test_submit_assigns_id_and_timestamps(self, store):
        from agent_runtime.task_store import Task

        task = store.submit(Task(title="hello", graph_id="g1"))
        assert task.task_id.startswith("task_")
        assert task.created_at > 0
        assert task.updated_at > 0
        assert task.status == TASK_STATUS_PENDING

    def test_submit_invalid_trigger_falls_back(self, store):
        from agent_runtime.task_store import Task

        task = store.submit(Task(title="x", trigger="bogus"))
        assert task.trigger == "immediate"

    def test_persistence_roundtrip(self, tmp_path, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="persist", agent_id="a1", priority=5))
        store.close()
        reopened = TaskStore(db_path=str(tmp_path / "unit_tasks.db"))
        loaded = reopened.get("t1")
        assert loaded is not None
        assert loaded.title == "persist"
        assert loaded.agent_id == "a1"
        assert loaded.priority == 5
        reopened.close()

    def test_list_filters_and_sorts(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t_a", title="a", agent_id="agent1", priority=1))
        store.submit(Task(task_id="t_b", title="b", agent_id="agent2", priority=9))
        store.submit(Task(task_id="t_c", title="c", agent_id="agent1", priority=3))
        all_tasks = store.list()
        assert [t["task_id"] for t in all_tasks] == ["t_b", "t_c", "t_a"]
        agent1 = store.list(agent_id="agent1")
        assert [t["task_id"] for t in agent1] == ["t_c", "t_a"]

    def test_update_status_running_sets_last_run(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="x"))
        ok = store.update_status("t1", TASK_STATUS_RUNNING, last_result={"k": "v"})
        assert ok is True
        task = store.get("t1")
        assert task.status == TASK_STATUS_RUNNING
        assert task.last_run_at > 0
        assert task.last_result == {"k": "v"}

    def test_cancel_pending_succeeds_completed_fails(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="x"))
        assert store.cancel("t1") is True
        assert store.get("t1").status == TASK_STATUS_CANCELED
        # 已 canceled 幂等返 False.
        assert store.cancel("t1") is False
        store.submit(Task(task_id="t2", title="y", status=TASK_STATUS_COMPLETED))
        assert store.cancel("t2") is False

    def test_rerun_increments_retry_and_resets(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="x", status=TASK_STATUS_FAILED, last_error="boom"))
        task = store.rerun("t1")
        assert task.status == TASK_STATUS_PENDING
        assert task.retry_count == 1
        assert task.last_error == ""
        assert store.rerun("nope") is None

    def test_add_artifacts_dedups(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="x"))
        assert store.add_artifacts("t1", ["art1", "art2"]) is True
        store.add_artifacts("t1", ["art2", "art3"])
        assert store.get("t1").artifact_ids == ["art1", "art2", "art3"]

    def test_artifact_result_json_roundtrip(self, tmp_path):
        from agent_runtime.task_store import Task

        s = TaskStore(db_path=str(tmp_path / "json_tasks.db"))
        s.submit(Task(task_id="t1", title="x"))
        s.update_status("t1", TASK_STATUS_COMPLETED, last_result={"events": 3, "ok": True})
        s.add_artifacts("t1", ["a", "b"])
        s.close()
        s2 = TaskStore(db_path=str(tmp_path / "json_tasks.db"))
        t = s2.get("t1")
        assert t.last_result == {"events": 3, "ok": True}
        assert t.artifact_ids == ["a", "b"]
        s2.close()


class TestTaskRpc:
    @pytest.mark.asyncio
    async def test_task_submit_and_get(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "My Task", "graph_id": "g1", "agent_id": "a1", "priority": 3},
        )
        assert "result" in resp, resp
        task = resp["result"]["task"]
        assert task["title"] == "My Task"
        assert task["status"] == TASK_STATUS_PENDING
        task_id = task["task_id"]

        get_resp = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        assert get_resp["result"]["task"]["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_task_list_and_status(self, daemon):
        s1 = await _rpc_call(daemon.socket_path, "task.submit", {"title": "one"})
        s2 = await _rpc_call(daemon.socket_path, "task.submit", {"title": "two"})
        # 两次提交必须拿到不同 task_id (修复同毫秒并发撞 id 后的回归断言).
        assert s1["result"]["task"]["task_id"] != s2["result"]["task"]["task_id"]
        list_resp = await _rpc_call(daemon.socket_path, "task.list", {"limit": 10})
        assert list_resp["result"]["total"] == 2

        task_id = s1["result"]["task"]["task_id"]
        st = await _rpc_call(
            daemon.socket_path,
            "task.status",
            {"task_id": task_id, "status": "running"},
        )
        assert st["result"]["task"]["status"] == TASK_STATUS_RUNNING
        assert st["result"]["task"]["last_run_at"] > 0

    @pytest.mark.asyncio
    async def test_task_cancel(self, daemon):
        sub = await _rpc_call(daemon.socket_path, "task.submit", {"title": "cancel me"})
        task_id = sub["result"]["task"]["task_id"]
        cancel = await _rpc_call(daemon.socket_path, "task.cancel", {"task_id": task_id})
        assert cancel["result"]["task"]["status"] == TASK_STATUS_CANCELED

    @pytest.mark.asyncio
    async def test_task_rerun(self, daemon):
        sub = await _rpc_call(daemon.socket_path, "task.submit", {"title": "rerun me"})
        task_id = sub["result"]["task"]["task_id"]
        rerun = await _rpc_call(daemon.socket_path, "task.rerun", {"task_id": task_id})
        assert rerun["result"]["task"]["retry_count"] == 1
        assert rerun["result"]["task"]["status"] == TASK_STATUS_PENDING

    @pytest.mark.asyncio
    async def test_task_get_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "task.get", {"task_id": "nope"})
        assert "error" in resp["result"] or resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_task_list_filter_by_status(self, daemon):
        await _rpc_call(daemon.socket_path, "task.submit", {"title": "p1"})
        s2 = await _rpc_call(daemon.socket_path, "task.submit", {"title": "p2"})
        tid = s2["result"]["task"]["task_id"]
        await _rpc_call(daemon.socket_path, "task.status", {"task_id": tid, "status": "completed"})
        done = await _rpc_call(daemon.socket_path, "task.list", {"status": "completed"})
        assert done["result"]["total"] == 1
        assert done["result"]["tasks"][0]["task_id"] == tid
