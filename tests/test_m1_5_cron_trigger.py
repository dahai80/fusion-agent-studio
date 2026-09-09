"""M1-5 contract tests: cron trigger → task creation (no sync graph exec).

Issue #304: cron job triggers = create Task (idempotency_key=job_id+fire_time),
not sync execute graph. cron从执行器降为触发器.

Runner: pytest tests/test_m1_5_cron_trigger.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

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
    data = await asyncio.wait_for(reader.readline(), timeout=10.0)
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


def _make_graph(daemon, graph_id="g-cron-test"):
    from agent_runtime.graph import AgentGraph, NodeConfig
    from agent_runtime.runtime import AgentRuntime
    from tools.registry import ToolRegistry

    class _EchoTool:
        name = "echo"
        description = "Echo tool"

        @property
        def parameters(self):
            return {"msg": {"type": "string"}}

        async def execute(self, **kwargs):
            return json.dumps({"status": "ok", "msg": kwargs.get("msg", "")})

        def openai_schema(self):
            return {
                "type": "function",
                "function": {"name": self.name, "description": self.description},
            }

    reg = ToolRegistry()
    reg.register(_EchoTool())
    daemon._runtime = AgentRuntime(tool_registry=reg)

    graph = AgentGraph(id=graph_id, name="Cron Test Graph")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "echo",
        NodeConfig(type="tool", label="Echo", tool_name="echo", tool_params={"msg": "hello"}),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "echo")
    graph.add_edge("echo", "end")
    daemon.store.save_graph(graph)
    return graph


def _make_job(graph_id="g-cron-test", job_id="cron-test-1", fire_time=None):
    from agent_runtime.triggers import CronJob

    return CronJob(
        id=job_id,
        name="Test Cron Job",
        expression="*/5 * * * *",
        graph_id=graph_id,
        enabled=True,
        next_run=fire_time or time.time(),
        input_data="",
    )


class TestCronTriggerTaskCreation:
    @pytest.mark.asyncio
    async def test_cron_creates_task_not_sync_exec(self, daemon, tmp_path):
        _make_graph(daemon, "g-cron-1")
        job = _make_job(graph_id="g-cron-1", job_id="j1", fire_time=1000000)
        result = await daemon._cron_default_handler(job)

        assert result["status"] == "triggered"
        assert result["task_id"].startswith("task_")
        assert result["execution_id"].startswith("exec_")

        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": result["task_id"]})
        task = get["result"]["task"]
        assert task["trigger"] == "cron"
        assert task["graph_id"] == "g-cron-1"
        assert task["idempotency_key"] == "cron:j1:1000000"

    @pytest.mark.asyncio
    async def test_cron_idempotency_dedup(self, daemon, tmp_path):
        _make_graph(daemon, "g-cron-2")
        job = _make_job(graph_id="g-cron-2", job_id="j2", fire_time=2000000)
        r1 = await daemon._cron_default_handler(job)
        assert r1["status"] == "triggered"
        task1_id = r1["task_id"]

        r2 = await daemon._cron_default_handler(job)
        assert r2["status"] == "deduped"
        assert r2["task_id"] == task1_id

    @pytest.mark.asyncio
    async def test_cron_different_fire_times_not_deduped(self, daemon, tmp_path):
        _make_graph(daemon, "g-cron-3")
        job1 = _make_job(graph_id="g-cron-3", job_id="j3", fire_time=3000000)
        job2 = _make_job(graph_id="g-cron-3", job_id="j3", fire_time=3000015)
        r1 = await daemon._cron_default_handler(job1)
        r2 = await daemon._cron_default_handler(job2)
        assert r1["status"] == "triggered"
        assert r2["status"] == "triggered"
        assert r1["task_id"] != r2["task_id"]

    @pytest.mark.asyncio
    async def test_cron_no_graph_id_skipped(self, daemon):
        from agent_runtime.triggers import CronJob

        job = CronJob(id="j-skip", name="Skip", expression="*/5 * * * *", graph_id="")
        result = await daemon._cron_default_handler(job)
        assert result["status"] == "skipped"
        assert result["reason"] == "no graph_id"

    @pytest.mark.asyncio
    async def test_cron_task_completes_async(self, daemon, tmp_path):
        _make_graph(daemon, "g-cron-complete")
        job = _make_job(graph_id="g-cron-complete", job_id="j4", fire_time=4000000)
        result = await daemon._cron_default_handler(job)
        execution_id = result["execution_id"]

        deadline = asyncio.get_event_loop().time() + 15
        final = {"status": "unknown"}
        while asyncio.get_event_loop().time() < deadline:
            status_resp = await _rpc_call(
                daemon.socket_path,
                "graph.status",
                {"execution_id": execution_id},
            )
            final = status_resp["result"]
            if final["status"] in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.3)

        assert final["status"] == "completed", f"got {final}"

        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": result["task_id"]})
        task = get["result"]["task"]
        assert task["status"] == "completed"
        assert (
            task.get("execution_id") == execution_id
            or task.get("last_result", {}).get("execution_id") == execution_id
        )

    @pytest.mark.asyncio
    async def test_cron_with_input_data_variables(self, daemon, tmp_path):
        _make_graph(daemon, "g-cron-input")
        from agent_runtime.triggers import CronJob

        job = CronJob(
            id="j-input",
            name="Input Job",
            expression="*/5 * * * *",
            graph_id="g-cron-input",
            next_run=5000000,
            input_data=json.dumps({"team": "ops", "custom_var": "value123"}),
        )
        result = await daemon._cron_default_handler(job)
        assert result["status"] == "triggered"

        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": result["task_id"]})
        task = get["result"]["task"]
        assert task["team"] == "ops"
