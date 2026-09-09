"""M1-2 contract tests: graph.execute_async + graph.status + graph.cancel + evidence jsonl.

Issue #302: F2 root cause fix — long execution must be async (return execution_id <=5s),
events stream via WS, status/cancel RPCs, evidence jsonl for crash recovery.

Runner: pytest tests/test_m1_2_graph_async.py -v
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


class _FakeArtifactCreateTool:
    name = "artifact_create"
    description = "Create an artifact"

    @property
    def parameters(self):
        return {"name": {"type": "string"}}

    async def execute(self, **kwargs):
        aid = "art-" + str(kwargs.get("name", "x"))
        return json.dumps({"status": "ok", "artifact_id": aid, "name": kwargs.get("name", "")})

    def openai_schema(self):
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description},
        }


class _SlowTool:
    name = "slow_task"
    description = "Slow tool for cancel testing"

    @property
    def parameters(self):
        return {"seconds": {"type": "number"}}

    async def execute(self, **kwargs):
        seconds = float(kwargs.get("seconds", 5))
        await asyncio.sleep(seconds)
        return json.dumps({"status": "ok", "slept": seconds})

    def openai_schema(self):
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description},
        }


def _make_artifact_graph(daemon, graph_id="g-async-art"):
    from agent_runtime.graph import AgentGraph, NodeConfig
    from agent_runtime.runtime import AgentRuntime
    from tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_FakeArtifactCreateTool())
    daemon._runtime = AgentRuntime(tool_registry=reg)

    graph = AgentGraph(id=graph_id, name="Async Art Graph")
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
    return graph


def _make_slow_graph(daemon, graph_id="g-async-slow", seconds=10):
    from agent_runtime.graph import AgentGraph, NodeConfig
    from agent_runtime.runtime import AgentRuntime
    from tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(_SlowTool())
    daemon._runtime = AgentRuntime(tool_registry=reg)

    graph = AgentGraph(id=graph_id, name="Slow Graph")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "slow",
        NodeConfig(
            type="tool",
            label="Slow",
            tool_name="slow_task",
            tool_params={"seconds": seconds},
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "slow")
    graph.add_edge("slow", "end")
    daemon.store.save_graph(graph)
    return graph


async def _wait_for_status(socket_path, execution_id, target, timeout=15, team="default"):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await _rpc_call(
            socket_path,
            "graph.status",
            {"execution_id": execution_id, "team": team},
        )
        status = resp.get("result", {}).get("status", "unknown")
        if status in (target, "failed", "cancelled"):
            return resp["result"]
        await asyncio.sleep(0.3)
    return {"status": "timeout", "last": resp.get("result") if "resp" in dir() else {}}


class TestExecuteAsync:
    @pytest.mark.asyncio
    async def test_execute_async_returns_execution_id_running(self, daemon, tmp_path):
        _make_artifact_graph(daemon)
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-async-art"},
        )
        result = resp["result"]
        assert result["status"] == "running"
        assert result["execution_id"].startswith("exec_")
        assert result["session_id"]

    @pytest.mark.asyncio
    async def test_execute_async_completes_and_writes_evidence(self, daemon, tmp_path):
        _make_artifact_graph(daemon)
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-async-art", "team": "default"},
        )
        execution_id = resp["result"]["execution_id"]

        final = await _wait_for_status(daemon.socket_path, execution_id, "completed", timeout=15)
        assert final["status"] == "completed", f"got {final}"

        ev_path = daemon._evidence_path(execution_id, "default")
        assert ev_path.exists(), f"evidence jsonl missing: {ev_path}"
        lines = ev_path.read_text(encoding="utf-8").strip().split("\n")
        records = [json.loads(l) for l in lines if l.strip()]
        types = [r["type"] for r in records]
        assert "execution.started" in types
        assert "execution.completed" in types
        assert records[-1]["artifact_ids"] == ["art-report"]

    @pytest.mark.asyncio
    async def test_execute_async_task_writeback(self, daemon, tmp_path):
        _make_artifact_graph(daemon, "g-async-twb")
        sub = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "async task", "graph_id": "g-async-twb"},
        )
        task_id = sub["result"]["task"]["task_id"]

        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-async-twb", "task_id": task_id},
        )
        execution_id = resp["result"]["execution_id"]

        final = await _wait_for_status(daemon.socket_path, execution_id, "completed", timeout=15)
        assert final["status"] == "completed"

        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        task = get["result"]["task"]
        assert task["status"] == TASK_STATUS_COMPLETED
        assert "art-report" in task["artifact_ids"]
        assert task["last_result"]["execution_id"] == execution_id
        assert task["evidence_ref"]
        assert os.path.exists(task["evidence_ref"])


class TestGraphStatus:
    @pytest.mark.asyncio
    async def test_status_unknown_execution(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.status",
            {"execution_id": "exec_nonexistent"},
        )
        result = resp["result"]
        assert result["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_status_missing_execution_id(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.status",
            {},
        )
        assert "error" in resp["result"]

    @pytest.mark.asyncio
    async def test_status_returns_progress_while_running(self, daemon):
        _make_slow_graph(daemon, "g-status-run", seconds=8)
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-status-run"},
        )
        execution_id = resp["result"]["execution_id"]
        try:
            status_resp = await _rpc_call(
                daemon.socket_path,
                "graph.status",
                {"execution_id": execution_id},
            )
            status = status_resp["result"]["status"]
            assert status in ("running", "completed")
        finally:
            await _rpc_call(
                daemon.socket_path,
                "graph.cancel",
                {"execution_id": execution_id},
            )


class TestGraphCancel:
    @pytest.mark.asyncio
    async def test_cancel_active_execution(self, daemon):
        _make_slow_graph(daemon, "g-cancel", seconds=20)
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-cancel"},
        )
        execution_id = resp["result"]["execution_id"]

        cancel_resp = await _rpc_call(
            daemon.socket_path,
            "graph.cancel",
            {"execution_id": execution_id, "reason": "test"},
        )
        assert cancel_resp["result"]["status"] == "cancelled"

        final = await _wait_for_status(daemon.socket_path, execution_id, "cancelled", timeout=5)
        assert final["status"] == "cancelled", f"expected cancelled, got {final}"

        ev_path = daemon._evidence_path(execution_id, "default")
        assert ev_path.exists()
        records = [
            json.loads(l)
            for l in ev_path.read_text(encoding="utf-8").strip().split("\n")
            if l.strip()
        ]
        types = [r["type"] for r in records]
        assert "execution.cancelled" in types

    @pytest.mark.asyncio
    async def test_cancel_task_writeback_canceled(self, daemon, tmp_path):
        _make_slow_graph(daemon, "g-cancel-tw", seconds=20)
        sub = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "cancel task", "graph_id": "g-cancel-tw"},
        )
        task_id = sub["result"]["task"]["task_id"]

        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute_async",
            {"graph_id": "g-cancel-tw", "task_id": task_id},
        )
        execution_id = resp["result"]["execution_id"]

        await _rpc_call(
            daemon.socket_path,
            "graph.cancel",
            {"execution_id": execution_id},
        )
        final = await _wait_for_status(daemon.socket_path, execution_id, "cancelled", timeout=5)
        assert final["status"] == "cancelled"

        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        task = get["result"]["task"]
        assert task["status"] == TASK_STATUS_CANCELED

    @pytest.mark.asyncio
    async def test_cancel_unknown_execution(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.cancel",
            {"execution_id": "exec_notthere"},
        )
        result = resp["result"]
        assert result["status"] == "not_found"


class TestSyncCompat:
    @pytest.mark.asyncio
    async def test_sync_graph_execute_still_works(self, daemon, tmp_path):
        _make_artifact_graph(daemon, "g-sync-still")
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.execute",
            {"graph_id": "g-sync-still"},
        )
        assert resp["result"]["status"] == "completed"
        assert resp["result"]["artifact_ids"] == ["art-report"]
