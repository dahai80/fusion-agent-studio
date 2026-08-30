"""Tests for daemon_server — UDS JSON-RPC 2.0 daemon.

Runners: pytest tests/test_daemon_server.py
API: DaemonServer start/stop + JSON-RPC dispatch over UDS.
Data schemas: JSON-RPC 2.0 request/response, temp socket paths.

User instruction: "坚各个产品的边界和原则，fusion-studio的GUI基本定稿了，现在把功能做起来，开始吧"
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer


def _mlx_reachable() -> bool:
    # 与 daemon_server._check_mlx_health 探测的 MLX_BASE_URL 对齐:
    # NetLayer 方案B 后默认经 gateway :11432, 不再直连 :11434.
    # 否则 gateway 起着但 11434 没起时, _MLX_UP=False 却 mlx.health=True, "not running" 用例误判失败.
    import socket
    from urllib.parse import urlparse

    gw = os.environ.get("FUSION_GATEWAY_URL")
    url = gw or f"http://127.0.0.1:{os.environ.get('FUSION_MLX_PORT', '11432')}/v1"
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_MLX_UP = _mlx_reachable()


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store_db(tmp_path):
    # #135: 用临时库隔离, 不污染用户真实 ~/.fusion-agent-studio/store.db.
    db = tmp_path / "test_store.db"
    yield str(db)


@pytest.fixture
async def daemon(socket_path, store_db):
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
async def daemon_stub(socket_path, store_db):
    # planner/RAG 测试在 fusion-mlx 运行时会走真实 LLM, 套件并发负载下
    # 偶发超时 flaky. 清掉 _default_client + _default_model → gateway.route
    # 返回 None → chat 立即返空 → planner/RAG 走 stub 路径, 确定性.
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    d._gateway._default_client = None
    d._gateway._default_model = ""
    # 清空 _models: _attach_mlx_client 启动探测到运行中 fusion-mlx 会
    # register_model, 使 route() 返真实 model 连外部服务 (本地 launchd-MLX
    # 在跑时偶发连真实 LLM). 清空 → route 返 None → stub 路径确定性.
    d._gateway._models.clear()
    d._planner = None
    d._rag = None
    yield d
    await d.stop()


async def _rpc_call(
    socket_path: str, method: str, params: dict | None = None, msg_id: int = 1
) -> dict:
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


class TestDaemonPing:
    @pytest.mark.asyncio
    async def test_ping(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "ping")
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"]["pong"] is True
        assert "timestamp" in resp["result"]


class TestDaemonGraphCRUD:
    @pytest.mark.asyncio
    async def test_graph_create_and_list(self, daemon):
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "Test Agent",
                "nodes": [
                    {"id": "start", "type": "start", "label": "Start"},
                    {"id": "end", "type": "end", "label": "End"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
            },
        )
        assert "result" in create_resp
        assert create_resp["result"]["name"] == "Test Agent"
        graph_id = create_resp["result"]["graph_id"]

        list_resp = await _rpc_call(daemon.socket_path, "graph.list")
        assert "result" in list_resp
        graphs = list_resp["result"]["graphs"]
        assert any(g["id"] == graph_id for g in graphs)

    @pytest.mark.asyncio
    async def test_graph_get(self, daemon):
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "Get Test",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
            },
        )
        graph_id = create_resp["result"]["graph_id"]

        get_resp = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": graph_id}
        )
        assert get_resp["result"]["graph_id"] == graph_id
        assert get_resp["result"]["name"] == "Get Test"

    @pytest.mark.asyncio
    async def test_graph_agent_id_persisted(self, daemon):
        # #131: graph 元数据内嵌 agent_id, create→get 往返不丢.
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "AgentGraph",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
                "agent_id": "c65efddbe8c5",
            },
        )
        graph_id = create_resp["result"]["graph_id"]
        get_resp = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": graph_id}
        )
        assert get_resp["result"]["agent_id"] == "c65efddbe8c5"

    @pytest.mark.asyncio
    async def test_graph_stop_on_tool_error_persisted(self, daemon):
        # #202: stop_on_tool_error 经 nodes/edges 分支 create→get→update 往返不丢.
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "StopOnErr",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
                "stop_on_tool_error": True,
            },
        )
        graph_id = create_resp["result"]["graph_id"]
        get_resp = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": graph_id}
        )
        assert get_resp["result"]["stop_on_tool_error"] is True
        # update 不传 flag 应回退保持原值 (load→save 保留), 返回字段含 flag.
        upd_resp = await _rpc_call(
            daemon.socket_path,
            "graph.update",
            {"graph_id": graph_id, "name": "StopOnErr2"},
        )
        assert upd_resp["result"]["stop_on_tool_error"] is True
        # 显式关 flag 后 get 应回 False.
        await _rpc_call(
            daemon.socket_path,
            "graph.update",
            {"graph_id": graph_id, "stop_on_tool_error": False},
        )
        get2 = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": graph_id}
        )
        assert get2["result"]["stop_on_tool_error"] is False

    @pytest.mark.asyncio
    async def test_graph_delete(self, daemon):
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "Delete Me",
                "nodes": [{"id": "start", "type": "start"}],
                "edges": [],
            },
        )
        graph_id = create_resp["result"]["graph_id"]

        del_resp = await _rpc_call(
            daemon.socket_path, "graph.delete", {"graph_id": graph_id}
        )
        assert del_resp["result"]["deleted"] is True

        get_resp = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": graph_id}
        )
        assert "error" in get_resp

    @pytest.mark.asyncio
    async def test_graph_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "graph.get", {"graph_id": "nonexistent"}
        )
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_graph_purge_test_by_names(self, daemon):
        # #135: graph.purge_test 按 names 精确清理 + dry_run 不实删.
        # 用唯一前缀隔离, 不依赖 store 是否已被既有残留污染.
        tag = "purgename-"
        for name in [tag + "a", tag + "a", tag + "keep"]:
            await _rpc_call(
                daemon.socket_path,
                "graph.create",
                {"name": name, "nodes": [{"id": "start", "type": "start"}], "edges": []},
            )

        dry = await _rpc_call(
            daemon.socket_path, "graph.purge_test", {"names": [tag + "a"], "dry_run": True}
        )
        assert dry["result"]["dry_run"] is True
        assert dry["result"]["would_delete"] == 2

        real = await _rpc_call(
            daemon.socket_path, "graph.purge_test", {"names": [tag + "a"]}
        )
        assert real["result"]["deleted"] == 2
        after = (await _rpc_call(daemon.socket_path, "graph.list"))["result"]["graphs"]
        ours = sorted(g["name"] for g in after if g["name"].startswith(tag))
        assert ours == [tag + "keep"]

    @pytest.mark.asyncio
    async def test_graph_purge_test_by_prefix(self, daemon):
        # #135: graph.purge_test 按前缀批量清理 e2e- 残留.
        tag = "purgepre-"
        for name in [tag + "flow", tag + "loop", tag + "keep"]:
            await _rpc_call(
                daemon.socket_path,
                "graph.create",
                {"name": name, "nodes": [{"id": "start", "type": "start"}], "edges": []},
            )
        resp = await _rpc_call(
            daemon.socket_path, "graph.purge_test", {"prefixes": [tag + "flow", tag + "loop"]}
        )
        assert resp["result"]["deleted"] == 2
        after = (await _rpc_call(daemon.socket_path, "graph.list"))["result"]["graphs"]
        ours = sorted(g["name"] for g in after if g["name"].startswith(tag))
        assert ours == [tag + "keep"]


class TestDaemonMLX:
    @pytest.mark.asyncio
    async def test_mlx_status_not_running(self, daemon):
        from agent_runtime.daemon_server import MLX_PORT
        resp = await _rpc_call(daemon.socket_path, "mlx.status")
        result = resp["result"]
        assert result["running"] is False
        assert result["port"] == MLX_PORT

    def test_default_ports_align_114xx(self):
        d = DaemonServer(socket_path="/tmp/_nonexistent.sock")
        assert d.http_port == 11455, "agent-studio HTTP must listen on 11455 (PORT_ALLOCATION)"
        assert d.cluster_port == 11457, "cluster port must avoid fusion-security 11454"
        assert d.ws_port == 11435

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        _MLX_UP, reason="fusion-mlx running; 'not running' path not testable"
    )
    async def test_mlx_health_not_running(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "mlx.health")
        result = resp["result"]
        assert result["healthy"] is False


class TestDaemonHardwareMetrics:
    @pytest.mark.asyncio
    async def test_hardware_metrics_schema(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "hardware.metrics")
        result = resp["result"]
        assert "memory" in result
        assert "cpu" in result
        assert "gpu" in result
        assert "mlx" in result
        for key in ("total_gb", "used_gb", "percent"):
            assert key in result["memory"]
        for key in ("percent", "count"):
            assert key in result["cpu"]
        assert "running" in result["mlx"]

    @pytest.mark.asyncio
    async def test_hardware_metrics_memory_total_positive(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "hardware.metrics")
        result = resp["result"]
        assert result["memory"]["total_gb"] > 0


class TestDaemonEnvCheck:
    @pytest.mark.asyncio
    async def test_env_health_check(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "env.health_check")
        result = resp["result"]
        assert "checks" in result
        assert result["checks"]["python"]["ok"] is True

    @pytest.mark.asyncio
    async def test_env_repair_returns_structured_not_implemented(self, daemon):
        # #217: env.repair must distinguish "not implemented" from "tried & failed".
        resp = await _rpc_call(
            daemon.socket_path, "env.repair", {"item_id": "mlx_server"}
        )
        result = resp["result"]
        assert result["status"] == "not_implemented"
        assert result["implemented"] is False
        assert result["repaired"] is False  # backward-compat field kept
        assert result["item_id"] == "mlx_server"

    @pytest.mark.asyncio
    async def test_env_repair_all_returns_structured_not_implemented(self, daemon):
        # #217: env.repair_all same structured not_implemented contract.
        resp = await _rpc_call(daemon.socket_path, "env.repair_all", {})
        result = resp["result"]
        assert result["status"] == "not_implemented"
        assert result["implemented"] is False
        assert result["repaired"] == []  # backward-compat field kept


class TestDaemonProtocol:
    @pytest.mark.asyncio
    async def test_invalid_jsonrpc(self, daemon):
        reader, writer = await asyncio.open_unix_connection(daemon.socket_path)
        writer.write(b'{"id": 1, "method": "ping"}\n')
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(data)
        assert "error" in resp
        assert resp["error"]["code"] == -32600

    @pytest.mark.asyncio
    async def test_method_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "nonexistent.method")
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_parse_error(self, daemon):
        reader, writer = await asyncio.open_unix_connection(daemon.socket_path)
        writer.write(b"not json\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(data)
        assert "error" in resp
        assert resp["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_multiple_requests(self, daemon):
        resp1 = await _rpc_call(daemon.socket_path, "ping", msg_id=10)
        resp2 = await _rpc_call(daemon.socket_path, "ping", msg_id=20)
        assert resp1["id"] == 10
        assert resp2["id"] == 20
        assert resp1["result"]["pong"] is True
        assert resp2["result"]["pong"] is True


class TestDaemonGraphCreateWithGraphData:
    @pytest.mark.asyncio
    async def test_create_with_graph_data(self, daemon):
        graph_data = {
            "id": "custom-id-123",
            "name": "From Data",
            "description": "Created from graph_data",
            "nodes": {
                "s1": {"type": "start", "label": "Start"},
                "e1": {"type": "end", "label": "End"},
            },
            "edges": [{"source_id": "s1", "target_id": "e1"}],
            "start_node_id": "s1",
        }
        resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {"name": "Override Name", "graph_data": graph_data},
        )
        assert resp["result"]["name"] == "Override Name"
        assert resp["result"]["description"] == "Created from graph_data"


class TestDaemonMLXInfer:
    @pytest.mark.asyncio
    async def test_infer_no_messages(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "mlx.infer", {})
        assert resp["result"]["status"] == "error"
        assert "messages" in resp["result"]["message"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        _MLX_UP, reason="fusion-mlx running; 'not running' path not testable"
    )
    async def test_infer_mlx_not_running(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "mlx.infer",
            {"messages": [{"role": "user", "content": "hello"}], "model": "test-model"},
        )
        assert resp["result"]["status"] == "error"
        assert (
            "not running" in resp["result"]["message"]
            or "unreachable" in resp["result"]["message"]
        )


class TestDaemonPlanner:
    @pytest.mark.asyncio
    async def test_planner_create_plan(self, daemon_stub):
        resp = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Refactor the auth module", "context": "Python project"},
        )
        result = resp["result"]
        assert "plan" in result
        plan = result["plan"]
        assert plan["task"] == "Refactor the auth module"
        assert len(plan["steps"]) >= 1
        assert plan["status"] == "pending_approval"

    @pytest.mark.asyncio
    async def test_planner_create_plan_no_task(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "planner.create_plan", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_planner_get_plan(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Test task"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(
            daemon_stub.socket_path, "planner.get_plan", {"plan_id": plan_id}
        )
        assert resp["result"]["plan"]["id"] == plan_id

    @pytest.mark.asyncio
    async def test_planner_get_plan_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "planner.get_plan", {"plan_id": "nonexistent"}
        )
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_planner_approve_plan(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Approve test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(
            daemon_stub.socket_path, "planner.approve_plan", {"plan_id": plan_id}
        )
        assert resp["result"]["approved"] is True

    @pytest.mark.asyncio
    async def test_planner_reject_plan(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Reject test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(
            daemon_stub.socket_path,
            "planner.reject_plan",
            {"plan_id": plan_id, "reason": "bad"},
        )
        assert resp["result"]["rejected"] is True

    @pytest.mark.asyncio
    async def test_planner_list_plans(self, daemon_stub):
        await _rpc_call(
            daemon_stub.socket_path, "planner.create_plan", {"task": "List test 1"}
        )
        await _rpc_call(
            daemon_stub.socket_path, "planner.create_plan", {"task": "List test 2"}
        )
        resp = await _rpc_call(daemon_stub.socket_path, "planner.list_plans", {})
        assert len(resp["result"]["plans"]) >= 2

    @pytest.mark.asyncio
    async def test_planner_cancel_plan(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Cancel test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(
            daemon_stub.socket_path, "planner.cancel_plan", {"plan_id": plan_id}
        )
        assert resp["result"]["cancelled"] is True

    @pytest.mark.asyncio
    async def test_planner_execute_step(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Step exec test"},
        )
        plan_id = create["result"]["plan"]["id"]
        await _rpc_call(
            daemon_stub.socket_path, "planner.approve_plan", {"plan_id": plan_id}
        )
        step_id = create["result"]["plan"]["steps"][0]["id"]
        resp = await _rpc_call(
            daemon_stub.socket_path,
            "planner.execute_step",
            {"plan_id": plan_id, "step_id": step_id},
        )
        assert "step" in resp["result"]

    @pytest.mark.asyncio
    async def test_planner_execute_plan(self, daemon_stub):
        create = await _rpc_call(
            daemon_stub.socket_path,
            "planner.create_plan",
            {"task": "Plan exec test"},
        )
        plan_id = create["result"]["plan"]["id"]
        await _rpc_call(
            daemon_stub.socket_path, "planner.approve_plan", {"plan_id": plan_id}
        )
        resp = await _rpc_call(
            daemon_stub.socket_path, "planner.execute_plan", {"plan_id": plan_id}
        )
        assert "plan" in resp["result"]


class TestDaemonRAG:
    @pytest.mark.asyncio
    async def test_rag_query_no_query(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "rag.query", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_rag_query_returns_result(self, daemon_stub):
        resp = await _rpc_call(
            daemon_stub.socket_path, "rag.query", {"query": "test query"}
        )
        result = resp["result"]
        assert "answer" in result
        assert "sources" in result

    @pytest.mark.asyncio
    async def test_rag_retrieve_no_query(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "rag.retrieve", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_rag_retrieve_returns_result(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "rag.retrieve", {"query": "test"})
        result = resp["result"]
        assert "query" in result
        assert "context_text" in result


class TestDaemonMemory:
    @pytest.mark.asyncio
    async def test_memory_store_and_recall(self, daemon):
        store_resp = await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Test memory content", "scope": "test", "importance": 7},
        )
        assert "entry_id" in store_resp["result"]
        _entry_id = store_resp["result"]["entry_id"]

        recall_resp = await _rpc_call(
            daemon.socket_path,
            "memory.recall",
            {"query": "Test memory", "scope": "test"},
        )
        entries = recall_resp["result"]["entries"]
        assert len(entries) >= 1

    @pytest.mark.asyncio
    async def test_memory_store_no_content(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "memory.store", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_memory_list_recent(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Recent test", "scope": "recent_test"},
        )
        resp = await _rpc_call(
            daemon.socket_path, "memory.list_recent", {"scope": "recent_test"}
        )
        assert len(resp["result"]["entries"]) >= 1

    @pytest.mark.asyncio
    async def test_memory_get(self, daemon):
        store_resp = await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Get test", "scope": "get_test"},
        )
        entry_id = store_resp["result"]["entry_id"]
        resp = await _rpc_call(daemon.socket_path, "memory.get", {"entry_id": entry_id})
        assert resp["result"]["entry"]["id"] == entry_id

    @pytest.mark.asyncio
    async def test_memory_get_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "memory.get", {"entry_id": "nonexistent"}
        )
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_memory_delete(self, daemon):
        store_resp = await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Delete test", "scope": "del_test"},
        )
        entry_id = store_resp["result"]["entry_id"]
        resp = await _rpc_call(
            daemon.socket_path, "memory.delete", {"entry_id": entry_id}
        )
        assert resp["result"]["deleted"] is True

    @pytest.mark.asyncio
    async def test_memory_delete_scope(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Scope del 1", "scope": "scope_del_test"},
        )
        await _rpc_call(
            daemon.socket_path,
            "memory.store",
            {"content": "Scope del 2", "scope": "scope_del_test"},
        )
        resp = await _rpc_call(
            daemon.socket_path, "memory.delete_scope", {"scope": "scope_del_test"}
        )
        assert resp["result"]["deleted_count"] >= 2

    @pytest.mark.asyncio
    async def test_memory_count(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "memory.count", {"scope": "nonexistent_scope_xyz"}
        )
        assert resp["result"]["count"] == 0

    @pytest.mark.asyncio
    async def test_memory_delete_scope_no_scope(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "memory.delete_scope", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_memory_backend_legacy_sqlite(self, daemon, monkeypatch):
        # FUSION_MEMORY_API_KEY 未配 -> _get_memory 返 MemoryEngine (sqlite)。
        monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
        resp = await _rpc_call(daemon.socket_path, "memory.backend", {})
        assert resp["result"]["backend"] == "sqlite"
        assert resp["result"]["api_key_configured"] is False

    @pytest.mark.asyncio
    async def test_memory_backend_fusion_memory(self, daemon, monkeypatch):
        # FUSION_MEMORY_API_KEY 配 -> _get_memory 返 FusionMemoryAdapter。
        # adapter __init__ 只建 httpx.Client, 不发网络, fm-server 不起也成功。
        monkeypatch.setenv("FUSION_MEMORY_API_KEY", "test-key")
        resp = await _rpc_call(daemon.socket_path, "memory.backend", {})
        assert resp["result"]["backend"] == "fusion_memory"
        assert resp["result"]["api_key_configured"] is True
        assert "11435" in resp["result"]["base_url"]


class TestDaemonSafety:
    @pytest.mark.asyncio
    async def test_safety_check_clean(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "safety.check", {"content": "Hello world"}
        )
        verdict = resp["result"]["verdict"]
        assert verdict["action"] == "allow"

    @pytest.mark.asyncio
    async def test_safety_check_no_content(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.check", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_safety_evaluate_action(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "safety.evaluate_action",
            {"category": "code_modification"},
        )
        assert "verdict" in resp["result"]

    @pytest.mark.asyncio
    async def test_safety_evaluate_no_category(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.evaluate_action", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_safety_approve_action_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "safety.approve_action", {"action_id": "nonexistent"}
        )
        assert resp["result"]["approved"] is False

    @pytest.mark.asyncio
    async def test_safety_reject_action_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "safety.reject_action", {"action_id": "nonexistent"}
        )
        assert resp["result"]["rejected"] is False

    @pytest.mark.asyncio
    async def test_safety_get_pending_actions(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.get_pending_actions", {})
        assert "actions" in resp["result"]

    @pytest.mark.asyncio
    async def test_safety_add_policy(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "safety.add_policy",
            {
                "category": "custom_test",
                "description": "Test policy",
                "default_level": "L2",
            },
        )
        assert resp["result"]["added"] is True
        assert resp["result"]["category"] == "custom_test"

    @pytest.mark.asyncio
    async def test_safety_add_policy_no_category(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.add_policy", {})
        assert resp["result"]["status"] == "error"


class TestDaemonTemplate:
    @pytest.mark.asyncio
    async def test_template_list(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "template.list", {})
        assert "templates" in resp["result"]
        assert isinstance(resp["result"]["templates"], list)

    @pytest.mark.asyncio
    async def test_template_list_with_category(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "template.list", {"category": "conversation"}
        )
        assert "templates" in resp["result"]

    @pytest.mark.asyncio
    async def test_template_get_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "template.get", {"template_id": "nonexistent"}
        )
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_template_instantiate_no_id(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "template.instantiate", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_template_instantiate_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "template.instantiate", {"template_id": "nonexistent"}
        )
        assert resp["result"]["status"] == "error"


class TestDaemonDeploy:
    @pytest.mark.asyncio
    async def test_deploy_list_formats(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "deploy.list_formats", {})
        assert "formats" in resp["result"]
        formats = resp["result"]["formats"]
        assert len(formats) >= 3

    @pytest.mark.asyncio
    async def test_deploy_export_no_graph(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "deploy.export",
            {"graph_id": "nonexistent", "format": "json"},
        )
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_deploy_export_json(self, daemon):
        create = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "Export Test",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
            },
        )
        graph_id = create["result"]["graph_id"]
        resp = await _rpc_call(
            daemon.socket_path,
            "deploy.export",
            {
                "graph_id": graph_id,
                "format": "json",
                "filepath": tempfile.mktemp(suffix=".json"),
            },
        )
        assert resp["result"]["status"] == "ok"
        assert "path" in resp["result"]

    @pytest.mark.asyncio
    async def test_deploy_import_no_filepath(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "deploy.import", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_deploy_import_file_not_found(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "deploy.import",
            {"filepath": "/tmp/nonexistent_abc123.json"},
        )
        assert resp["result"]["status"] == "error"


class TestTeamEndpoints:
    # team.* JSON-RPC endpoints wire SwarmRouter/Plaza/FMProtocol to fusion-studio GUI.

    @pytest.mark.asyncio
    async def test_swarm_register_and_list(self, daemon):
        r = await _rpc_call(
            daemon.socket_path,
            "team.swarm_register",
            {
                "id": "a1",
                "name": "coder",
                "capabilities": ["code"],
                "handoff_targets": ["a2"],
                "max_hops": 3,
            },
        )
        assert r["result"]["ok"] is True
        r = await _rpc_call(daemon.socket_path, "team.swarm_agents")
        assert any(a["id"] == "a1" for a in r["result"]["agents"])

    @pytest.mark.asyncio
    async def test_swarm_delegate_and_stats(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "team.swarm_register",
            {"id": "sup", "name": "supervisor", "capabilities": ["manage"]},
        )
        await _rpc_call(
            daemon.socket_path,
            "team.swarm_register",
            {"id": "cod", "name": "coder", "capabilities": ["code"]},
        )
        r = await _rpc_call(
            daemon.socket_path,
            "team.swarm_delegate",
            {"delegator_id": "sup", "task": "write", "capability": "code"},
        )
        assert r["result"]["delegation"] is not None
        r = await _rpc_call(daemon.socket_path, "team.swarm_stats")
        assert r["result"]["delegations"] >= 1
        assert r["result"]["fmp_sent"] >= 1

    @pytest.mark.asyncio
    async def test_swarm_handoff(self, daemon):
        await _rpc_call(
            daemon.socket_path, "team.swarm_register", {"id": "h1", "name": "a"}
        )
        await _rpc_call(
            daemon.socket_path, "team.swarm_register", {"id": "h2", "name": "b"}
        )
        r = await _rpc_call(
            daemon.socket_path,
            "team.swarm_handoff",
            {
                "from_id": "h1",
                "to_id": "h2",
                "conversation": [],
                "hop_count": 0,
                "task_id": "t1",
            },
        )
        assert r["result"]["context"] is not None

    @pytest.mark.asyncio
    async def test_plaza_create_broadcast_messages(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "team.plaza_create",
            {"name": "ch1", "participants": ["w1", "w2"]},
        )
        await _rpc_call(
            daemon.socket_path,
            "team.plaza_broadcast",
            {"channel": "ch1", "sender": "w1", "content": "hello"},
        )
        r = await _rpc_call(
            daemon.socket_path, "team.plaza_messages", {"channel": "ch1"}
        )
        assert len(r["result"]["messages"]) >= 1
        r = await _rpc_call(daemon.socket_path, "team.plaza_channels")
        assert "ch1" in r["result"]["channels"]

    @pytest.mark.asyncio
    async def test_plaza_circuit_initial(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "team.plaza_create",
            {"name": "ch2", "participants": ["w1"]},
        )
        r = await _rpc_call(
            daemon.socket_path, "team.plaza_circuit", {"channel": "ch2"}
        )
        assert r["result"]["tripped"] is False

    @pytest.mark.asyncio
    async def test_fmp_register_send_stats(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "team.fmp_register",
            {"id": "f1", "name": "agent1", "capabilities": ["code"]},
        )
        r = await _rpc_call(
            daemon.socket_path,
            "team.fmp_send",
            {"recipient": "f1", "message_type": "request", "payload": {"k": "v"}},
        )
        assert r["result"]["message"] is not None
        r = await _rpc_call(daemon.socket_path, "team.fmp_stats")
        assert r["result"]["stats"]["sent"] >= 1

    @pytest.mark.asyncio
    async def test_team_endpoints_mapped(self, daemon):
        methods = [
            "team.swarm_register",
            "team.swarm_agents",
            "team.swarm_delegate",
            "team.swarm_handoff",
            "team.swarm_evaluate",
            "team.swarm_escalate",
            "team.swarm_stats",
            "team.plaza_create",
            "team.plaza_broadcast",
            "team.plaza_messages",
            "team.plaza_channels",
            "team.plaza_break_in",
            "team.plaza_circuit",
            "team.fmp_register",
            "team.fmp_send",
            "team.fmp_stats",
            "team.orchestrate",
        ]
        for m in methods:
            assert daemon._get_handler(m) is not None, m


class TestHarnessEndpoints:
    # hooks.* / context.* JSON-RPC endpoints expose harness engines to the GUI.

    @pytest.mark.asyncio
    async def test_hooks_register_list(self, daemon):
        r = await _rpc_call(daemon.socket_path, "hooks.list", {})
        assert r["result"]["hooks"] == []
        r = await _rpc_call(
            daemon.socket_path,
            "hooks.register",
            {
                "event": "PRE_TOOL_USE",
                "matcher": ".*",
                "type": "command",
                "command": "echo hi",
            },
        )
        assert r["result"]["ok"] is True
        r = await _rpc_call(daemon.socket_path, "hooks.list", {})
        assert len(r["result"]["hooks"]) == 1
        assert r["result"]["hooks"][0]["event"] == "PRE_TOOL_USE"

    @pytest.mark.asyncio
    async def test_hooks_test_default_result(self, daemon):
        r = await _rpc_call(
            daemon.socket_path,
            "hooks.test",
            {
                "event": "SESSION_START",
                "payload": {"k": "v"},
            },
        )
        res = r["result"]["result"]
        assert res["continue_loop"] is True
        assert res["decision"] == "approve"

    @pytest.mark.asyncio
    async def test_context_usage(self, daemon):
        msgs = [{"role": "user", "content": "hello world"}]
        r = await _rpc_call(daemon.socket_path, "context.usage", {"messages": msgs})
        assert r["result"]["tokens"] > 0
        assert r["result"]["level"] == "none"
        assert r["result"]["context_window"] > 0

    @pytest.mark.asyncio
    async def test_context_compact_truncates_tool(self, daemon):
        big = "x" * 5000
        msgs = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "run", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": big},
        ]
        r = await _rpc_call(
            daemon.socket_path,
            "context.compact",
            {"messages": msgs, "level": "warning"},
        )
        assert r["result"]["before_tokens"] > r["result"]["after_tokens"]
        assert isinstance(r["result"]["messages"], list)

    @pytest.mark.asyncio
    async def test_harness_endpoints_mapped(self, daemon):
        for m in [
            "hooks.list",
            "hooks.register",
            "hooks.test",
            "context.compact",
            "context.usage",
        ]:
            assert daemon._get_handler(m) is not None, m


class TestDaemonCronRuntime:
    @pytest.mark.asyncio
    async def test_cron_loop_started_with_daemon(self, daemon):
        # 断裂点 1 修复: daemon start() 必须启动 cron loop
        cm = daemon._get_cron_manager()
        assert cm._running is True

    @pytest.mark.asyncio
    async def test_cron_default_handler_executes_graph(self, daemon):
        # 断裂点 2 修复: 默认 handler 包装 graph.execute
        create_resp = await _rpc_call(
            daemon.socket_path,
            "graph.create",
            {
                "name": "Cron Target",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [{"source_id": "start", "target_id": "end"}],
            },
        )
        graph_id = create_resp["result"]["graph_id"]
        reg_resp = await _rpc_call(
            daemon.socket_path,
            "cron.register",
            {
                "id": "cron_e2e_test",
                "name": "E2E trigger",
                "expression": "* * * * *",
                "graph_id": graph_id,
                "input_data": '{"hook_variant": "A"}',
            },
        )
        assert reg_resp["result"]["status"] == "ok"
        cm = daemon._get_cron_manager()
        job = cm.get("cron_e2e_test")
        assert job is not None and job.graph_id == graph_id
        result = await daemon._cron_default_handler(job)
        assert result["status"] == "completed"
        assert result["events"] >= 1
        await cm.aunregister("cron_e2e_test")

    @pytest.mark.asyncio
    async def test_cron_default_handler_no_graph_id_skips(self, daemon):
        from agent_runtime.triggers import CronJob

        job = CronJob(id="skip_test", name="No graph", expression="* * * * *")
        result = await daemon._cron_default_handler(job)
        assert result["status"] == "skipped"
        assert result["reason"] == "no graph_id"


class TestDaemonStatusFailedPlugins:
    # issue #164: daemon.status 必须显式暴露 plugin 加载失败, 避免静默缺工具.

    @pytest.mark.asyncio
    async def test_status_returns_failed_plugins_key(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "daemon.status")
        assert "result" in resp
        result = resp["result"]
        # 新增 failed_plugins 字段, 始终存在 (dict[str,str], 空表示无失败).
        assert "failed_plugins" in result
        assert isinstance(result["failed_plugins"], dict)

    @pytest.mark.asyncio
    async def test_status_failed_plugins_reflects_registry(self, daemon):
        # 注入一个模拟失败 plugin, 确认 daemon.status 透传 registry.failed_plugins.
        reg = daemon._get_tool_registry()
        reg.failed_plugins = {"fake_broken_plugin": "No module named 'cv2'"}
        resp = await _rpc_call(daemon.socket_path, "daemon.status")
        result = resp["result"]
        assert result["failed_plugins"] == {"fake_broken_plugin": "No module named 'cv2'"}


class TestDispatchStrParamsTolerance:
    # issue #172: 客户端传 str params (如 "{}" 或 json.dumps) 时 _dispatch 须规范化为 dict,
    # 否则 handler 内 params.get 崩溃 AttributeError.

    async def _rpc_call_str_params(self, socket_path, method, params_str, msg_id=1):
        # 绕过 _rpc_call (只接 dict), 直接发 str params.
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params_str}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        return json.loads(data)

    @pytest.mark.asyncio
    async def test_str_params_empty_dict_string(self, daemon):
        # params="{}" 应被解析为 {} -> graph.list 不崩, 正常返回.
        resp = await self._rpc_call_str_params(daemon.socket_path, "graph.list", "{}")
        assert "result" in resp, resp
        assert "graphs" in resp["result"]

    @pytest.mark.asyncio
    async def test_str_params_json_object(self, daemon):
        # params='{"limit":5}' 应被解析为 dict -> daemon.status 不崩.
        resp = await self._rpc_call_str_params(
            daemon.socket_path, "daemon.status", '{"limit": 5}'
        )
        assert "result" in resp, resp

    @pytest.mark.asyncio
    async def test_str_params_invalid_json(self, daemon):
        # params="not-json" 应兜底为 {} -> 不崩, 返回正常 result (非 error -32603).
        resp = await self._rpc_call_str_params(daemon.socket_path, "graph.list", "not-json")
        assert "result" in resp, resp
        assert "graphs" in resp["result"]

    @pytest.mark.asyncio
    async def test_str_params_empty_string(self, daemon):
        # params="" 应兜底为 {} -> graph.list 不崩.
        resp = await self._rpc_call_str_params(daemon.socket_path, "graph.list", "")
        assert "result" in resp, resp


class TestP0_12ExecKeyCollision:
    # 审计 P0-12: id(rt) 进程级单例 fallback, 并发同 graph 无 task_id 的多执行
    # 互覆同一 exec_key. register_execution 用 id(current_task) 每 task 唯一.

    @pytest.mark.asyncio
    async def test_register_execution_distinct_keys_per_task(self, daemon):
        # 两个并发 task 同 scope+空 key_id -> 旧版同键互覆, 新版各拿唯一键.
        keys = []

        async def _register():
            exec_key, _unreg = daemon.register_execution("graph", "")
            keys.append(exec_key)
            await asyncio.sleep(0.05)
            _unreg()

        await asyncio.gather(_register(), _register())
        assert len(keys) == 2
        assert keys[0] != keys[1], f"collision: both keys = {keys[0]}"

    @pytest.mark.asyncio
    async def test_register_execution_unregister_releases_slot(self, daemon):
        exec_key, _unreg = daemon.register_execution("graph", "task-1")
        assert exec_key in daemon._active_executions
        _unreg()
        assert exec_key not in daemon._active_executions

    @pytest.mark.asyncio
    async def test_register_execution_explicit_key_stable(self, daemon):
        # 有 task_id 时不走 fallback, 同 task_id 多次注册同键 (调用方职责去重).
        k1, u1 = daemon.register_execution("graph", "task-7")
        k2, u2 = daemon.register_execution("graph", "task-7")
        assert k1 == k2 == "graph:task-7"
        u1()
        u2()
