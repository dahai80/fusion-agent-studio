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
    async def test_task_delete(self, daemon):
        sub = await _rpc_call(daemon.socket_path, "task.submit", {"title": "del me"})
        task_id = sub["result"]["task"]["task_id"]
        dele = await _rpc_call(daemon.socket_path, "task.delete", {"task_id": task_id})
        assert dele["result"]["status"] == "ok"
        assert dele["result"]["deleted"] is True
        # 删除后 get 应找不到.
        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        assert "error" in get["result"] or get["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_task_delete_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "task.delete", {"task_id": "nope"})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_task_add_artifacts(self, daemon):
        sub = await _rpc_call(daemon.socket_path, "task.submit", {"title": "art me"})
        task_id = sub["result"]["task"]["task_id"]
        resp = await _rpc_call(
            daemon.socket_path,
            "task.add_artifacts",
            {"task_id": task_id, "artifact_ids": ["art-1", "art-2"]},
        )
        assert resp["result"]["status"] == "ok"
        assert resp["result"]["added"] is True
        assert resp["result"]["artifact_ids"] == ["art-1", "art-2"]
        # 重复 add 不应产生重复.
        resp2 = await _rpc_call(
            daemon.socket_path,
            "task.add_artifacts",
            {"task_id": task_id, "artifact_ids": ["art-1", "art-3"]},
        )
        assert resp2["result"]["artifact_ids"] == ["art-1", "art-2", "art-3"]

    @pytest.mark.asyncio
    async def test_task_add_artifacts_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "task.add_artifacts",
            {"task_id": "nope", "artifact_ids": ["art-1"]},
        )
        assert resp["result"]["status"] == "error"

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


# ── #141 priority-4: task↔artifact 回写 (graph.execute 透传 task_id) ──


class _FakeArtifactCreateTool:
    # 假 artifact_create 工具: 直接返回带 artifact_id 的 JSON, 不依赖 ArtifactManager/LLM.
    name = "artifact_create"
    description = "fake artifact create"
    parameters = {"name": {"type": "string"}}

    async def execute(self, **kwargs):
        aid = "art-" + str(kwargs.get("name", "x"))
        return json.dumps({"status": "ok", "artifact_id": aid, "name": kwargs.get("name", "")})

    def openai_schema(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description}}


class TestTaskArtifactWriteback:
    @pytest.mark.asyncio
    async def test_graph_execute_writes_back_artifacts(self, daemon, tmp_path):
        from agent_runtime.graph import AgentGraph, NodeConfig
        from agent_runtime.runtime import AgentRuntime
        from tools.registry import ToolRegistry

        # 注入带假 artifact_create 工具的 runtime.
        reg = ToolRegistry()
        reg.register(_FakeArtifactCreateTool())
        daemon._runtime = AgentRuntime(tool_registry=reg)

        graph = AgentGraph(id="g-art", name="Art Graph")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "art",
            NodeConfig(
                type="tool",
                label="CreateArtifact",
                tool_name="artifact_create",
                tool_params={"name": "report"},
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "art")
        graph.add_edge("art", "end")
        daemon.store.save_graph(graph)

        # 提交一个关联该 graph 的 task.
        sub = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "art task", "graph_id": "g-art"},
        )
        task_id = sub["result"]["task"]["task_id"]

        # graph.execute 透传 task_id, 执行后应回写 artifact_ids + completed.
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute",
            {"graph_id": "g-art", "task_id": task_id},
        )
        assert resp["result"]["status"] == "completed"
        assert "art-report" in resp["result"]["artifact_ids"]

        # 查 task: artifact_ids 已回写, 状态 completed.
        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        task = get["result"]["task"]
        assert task["status"] == TASK_STATUS_COMPLETED
        assert "art-report" in task["artifact_ids"]
        assert task["last_result"]["artifact_ids"] == ["art-report"]

    @pytest.mark.asyncio
    async def test_graph_execute_without_task_id_no_writeback(self, daemon, tmp_path):
        # 不传 task_id 时, 行为不变, 不触发 task 回写.
        from agent_runtime.graph import AgentGraph, NodeConfig
        from agent_runtime.runtime import AgentRuntime
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(_FakeArtifactCreateTool())
        daemon._runtime = AgentRuntime(tool_registry=reg)

        graph = AgentGraph(id="g-noart", name="No Task Graph")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "art",
            NodeConfig(
                type="tool",
                label="CreateArtifact",
                tool_name="artifact_create",
                tool_params={"name": "doc"},
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "art")
        graph.add_edge("art", "end")
        daemon.store.save_graph(graph)

        resp = await _rpc_call(
            daemon.socket_path, "graph.execute", {"graph_id": "g-noart"}
        )
        assert resp["result"]["status"] == "completed"
        assert resp["result"]["artifact_ids"] == ["art-doc"]
        assert resp["result"]["task_id"] == ""


# ── #141 priority-2: project.* 多 Task 聚合容器 (TaskBoardView 看板) ──


class TestProjectStoreUnit:
    def test_projects_aggregates_counts(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="a", project_id="proj-A", status=TASK_STATUS_PENDING))
        store.submit(Task(task_id="t2", title="b", project_id="proj-A", status=TASK_STATUS_COMPLETED))
        store.submit(Task(task_id="t3", title="c", project_id="proj-B", status=TASK_STATUS_RUNNING))
        # project_id 空的不计入聚合.
        store.submit(Task(task_id="t4", title="d", project_id=""))
        projects = store.projects()
        pids = {p["project_id"] for p in projects}
        assert pids == {"proj-A", "proj-B"}
        a = next(p for p in projects if p["project_id"] == "proj-A")
        assert a["total"] == 2
        assert a["pending"] == 1
        assert a["completed"] == 1

    def test_list_filters_by_project(self, store):
        from agent_runtime.task_store import Task

        store.submit(Task(task_id="t1", title="a", project_id="proj-A"))
        store.submit(Task(task_id="t2", title="b", project_id="proj-B"))
        store.submit(Task(task_id="t3", title="c", project_id="proj-A"))
        a_tasks = store.list(project_id="proj-A")
        assert [t["task_id"] for t in a_tasks] == ["t3", "t1"]

    def test_project_id_persisted_roundtrip(self, tmp_path):
        from agent_runtime.task_store import Task

        s = TaskStore(db_path=str(tmp_path / "proj_tasks.db"))
        s.submit(Task(task_id="t1", title="x", project_id="proj-A"))
        s.close()
        s2 = TaskStore(db_path=str(tmp_path / "proj_tasks.db"))
        t = s2.get("t1")
        assert t is not None
        assert t.project_id == "proj-A"
        s2.close()

    def test_old_db_migrates_project_id_column(self, tmp_path):
        # 老库无 project_id 列(其余列齐全), 重新打开应自动 ALTER 迁移, 不报错.
        import sqlite3

        dbp = str(tmp_path / "old_tasks.db")
        conn = sqlite3.connect(dbp)
        conn.execute(
            """CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT DEFAULT '',
                description TEXT DEFAULT '', agent_id TEXT DEFAULT '',
                graph_id TEXT DEFAULT '', trigger TEXT DEFAULT 'immediate',
                cron_expression TEXT DEFAULT '', run_at REAL DEFAULT 0,
                cron_job_id TEXT DEFAULT '', input TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 0,
                artifact_ids TEXT DEFAULT '[]', last_result TEXT DEFAULT '{}',
                last_error TEXT DEFAULT '', retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 0, created_at REAL DEFAULT 0,
                updated_at REAL DEFAULT 0, last_run_at REAL DEFAULT 0
            )"""
        )
        conn.execute("INSERT INTO tasks (task_id, title) VALUES ('old1', 'legacy')")
        conn.commit()
        conn.close()
        s = TaskStore(db_path=dbp)
        t = s.get("old1")
        assert t is not None
        assert t.project_id == ""
        s.close()


class TestProjectRpc:
    @pytest.mark.asyncio
    async def test_project_list_aggregates(self, daemon):
        await _rpc_call(daemon.socket_path, "task.submit", {"title": "a", "project_id": "proj-A"})
        await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "b", "project_id": "proj-A", "status": "completed"},
        )
        await _rpc_call(daemon.socket_path, "task.submit", {"title": "c", "project_id": "proj-B"})
        resp = await _rpc_call(daemon.socket_path, "project.list", {})
        projects = resp["result"]["projects"]
        a = next(p for p in projects if p["project_id"] == "proj-A")
        assert a["total"] == 2
        assert a["completed"] == 1
        b = next(p for p in projects if p["project_id"] == "proj-B")
        assert b["total"] == 1

    @pytest.mark.asyncio
    async def test_project_tasks_filter(self, daemon):
        await _rpc_call(daemon.socket_path, "task.submit", {"title": "a", "project_id": "proj-X"})
        s2 = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "b", "project_id": "proj-X", "status": "completed"},
        )
        await _rpc_call(daemon.socket_path, "task.submit", {"title": "c", "project_id": "proj-Y"})
        resp = await _rpc_call(daemon.socket_path, "project.tasks", {"project_id": "proj-X"})
        assert resp["result"]["project_id"] == "proj-X"
        assert resp["result"]["total"] == 2
        done = await _rpc_call(
            daemon.socket_path,
            "project.tasks",
            {"project_id": "proj-X", "status": "completed"},
        )
        assert done["result"]["total"] == 1
        assert done["result"]["tasks"][0]["task_id"] == s2["result"]["task"]["task_id"]

    @pytest.mark.asyncio
    async def test_project_tasks_requires_project_id(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "project.tasks", {})
        assert resp["result"]["status"] == "error"
