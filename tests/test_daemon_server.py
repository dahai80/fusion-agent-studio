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


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def daemon(socket_path):
    d = DaemonServer(socket_path=socket_path)
    await d.start()
    yield d
    await d.stop()


async def _rpc_call(socket_path: str, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path)
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
            daemon.socket_path, "graph.create",
            {"name": "Test Agent", "nodes": [{"id": "start", "type": "start", "label": "Start"}, {"id": "end", "type": "end", "label": "End"}], "edges": [{"source_id": "start", "target_id": "end"}]},
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
            daemon.socket_path, "graph.create",
            {"name": "Get Test", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}], "edges": [{"source_id": "start", "target_id": "end"}]},
        )
        graph_id = create_resp["result"]["graph_id"]

        get_resp = await _rpc_call(daemon.socket_path, "graph.get", {"graph_id": graph_id})
        assert get_resp["result"]["graph_id"] == graph_id
        assert get_resp["result"]["name"] == "Get Test"

    @pytest.mark.asyncio
    async def test_graph_delete(self, daemon):
        create_resp = await _rpc_call(
            daemon.socket_path, "graph.create",
            {"name": "Delete Me", "nodes": [{"id": "start", "type": "start"}], "edges": []},
        )
        graph_id = create_resp["result"]["graph_id"]

        del_resp = await _rpc_call(daemon.socket_path, "graph.delete", {"graph_id": graph_id})
        assert del_resp["result"]["deleted"] is True

        get_resp = await _rpc_call(daemon.socket_path, "graph.get", {"graph_id": graph_id})
        assert "error" in get_resp

    @pytest.mark.asyncio
    async def test_graph_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "graph.get", {"graph_id": "nonexistent"})
        assert "error" in resp


class TestDaemonHardware:
    @pytest.mark.asyncio
    async def test_hardware_metrics(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "hardware.metrics")
        result = resp["result"]
        assert result["platform"] == "Darwin"
        assert "python_version" in result


class TestDaemonMLX:
    @pytest.mark.asyncio
    async def test_mlx_status_not_running(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "mlx.status")
        result = resp["result"]
        assert result["running"] is False
        assert result["port"] == 11434

    @pytest.mark.asyncio
    async def test_mlx_health_not_running(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "mlx.health")
        result = resp["result"]
        assert result["healthy"] is False


class TestDaemonEnvCheck:
    @pytest.mark.asyncio
    async def test_env_health_check(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "env.health_check")
        result = resp["result"]
        assert "checks" in result
        assert result["checks"]["python"]["ok"] is True


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
        writer.write(b'not json\n')
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
            daemon.socket_path, "graph.create",
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
    async def test_infer_mlx_not_running(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "mlx.infer",
            {"messages": [{"role": "user", "content": "hello"}], "model": "test-model"},
        )
        assert resp["result"]["status"] == "error"
        assert "not running" in resp["result"]["message"] or "unreachable" in resp["result"]["message"]


class TestDaemonPlanner:
    @pytest.mark.asyncio
    async def test_planner_create_plan(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
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
    async def test_planner_get_plan(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Test task"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(daemon.socket_path, "planner.get_plan", {"plan_id": plan_id})
        assert resp["result"]["plan"]["id"] == plan_id

    @pytest.mark.asyncio
    async def test_planner_get_plan_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "planner.get_plan", {"plan_id": "nonexistent"})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_planner_approve_plan(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Approve test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(daemon.socket_path, "planner.approve_plan", {"plan_id": plan_id})
        assert resp["result"]["approved"] is True

    @pytest.mark.asyncio
    async def test_planner_reject_plan(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Reject test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(daemon.socket_path, "planner.reject_plan", {"plan_id": plan_id, "reason": "bad"})
        assert resp["result"]["rejected"] is True

    @pytest.mark.asyncio
    async def test_planner_list_plans(self, daemon):
        await _rpc_call(daemon.socket_path, "planner.create_plan", {"task": "List test 1"})
        await _rpc_call(daemon.socket_path, "planner.create_plan", {"task": "List test 2"})
        resp = await _rpc_call(daemon.socket_path, "planner.list_plans", {})
        assert len(resp["result"]["plans"]) >= 2

    @pytest.mark.asyncio
    async def test_planner_cancel_plan(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Cancel test"},
        )
        plan_id = create["result"]["plan"]["id"]
        resp = await _rpc_call(daemon.socket_path, "planner.cancel_plan", {"plan_id": plan_id})
        assert resp["result"]["cancelled"] is True

    @pytest.mark.asyncio
    async def test_planner_execute_step(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Step exec test"},
        )
        plan_id = create["result"]["plan"]["id"]
        await _rpc_call(daemon.socket_path, "planner.approve_plan", {"plan_id": plan_id})
        step_id = create["result"]["plan"]["steps"][0]["id"]
        resp = await _rpc_call(daemon.socket_path, "planner.execute_step", {"plan_id": plan_id, "step_id": step_id})
        assert "step" in resp["result"]

    @pytest.mark.asyncio
    async def test_planner_execute_plan(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "planner.create_plan",
            {"task": "Plan exec test"},
        )
        plan_id = create["result"]["plan"]["id"]
        await _rpc_call(daemon.socket_path, "planner.approve_plan", {"plan_id": plan_id})
        resp = await _rpc_call(daemon.socket_path, "planner.execute_plan", {"plan_id": plan_id})
        assert "plan" in resp["result"]


class TestDaemonRAG:
    @pytest.mark.asyncio
    async def test_rag_query_no_query(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "rag.query", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_rag_query_returns_result(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "rag.query", {"query": "test query"})
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
            daemon.socket_path, "memory.store",
            {"content": "Test memory content", "scope": "test", "importance": 7},
        )
        assert "entry_id" in store_resp["result"]
        entry_id = store_resp["result"]["entry_id"]

        recall_resp = await _rpc_call(
            daemon.socket_path, "memory.recall",
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
            daemon.socket_path, "memory.store",
            {"content": "Recent test", "scope": "recent_test"},
        )
        resp = await _rpc_call(daemon.socket_path, "memory.list_recent", {"scope": "recent_test"})
        assert len(resp["result"]["entries"]) >= 1

    @pytest.mark.asyncio
    async def test_memory_get(self, daemon):
        store_resp = await _rpc_call(
            daemon.socket_path, "memory.store",
            {"content": "Get test", "scope": "get_test"},
        )
        entry_id = store_resp["result"]["entry_id"]
        resp = await _rpc_call(daemon.socket_path, "memory.get", {"entry_id": entry_id})
        assert resp["result"]["entry"]["id"] == entry_id

    @pytest.mark.asyncio
    async def test_memory_get_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "memory.get", {"entry_id": "nonexistent"})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_memory_delete(self, daemon):
        store_resp = await _rpc_call(
            daemon.socket_path, "memory.store",
            {"content": "Delete test", "scope": "del_test"},
        )
        entry_id = store_resp["result"]["entry_id"]
        resp = await _rpc_call(daemon.socket_path, "memory.delete", {"entry_id": entry_id})
        assert resp["result"]["deleted"] is True

    @pytest.mark.asyncio
    async def test_memory_delete_scope(self, daemon):
        await _rpc_call(
            daemon.socket_path, "memory.store",
            {"content": "Scope del 1", "scope": "scope_del_test"},
        )
        await _rpc_call(
            daemon.socket_path, "memory.store",
            {"content": "Scope del 2", "scope": "scope_del_test"},
        )
        resp = await _rpc_call(daemon.socket_path, "memory.delete_scope", {"scope": "scope_del_test"})
        assert resp["result"]["deleted_count"] >= 2

    @pytest.mark.asyncio
    async def test_memory_count(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "memory.count", {"scope": "nonexistent_scope_xyz"})
        assert resp["result"]["count"] == 0

    @pytest.mark.asyncio
    async def test_memory_delete_scope_no_scope(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "memory.delete_scope", {})
        assert resp["result"]["status"] == "error"


class TestDaemonSafety:
    @pytest.mark.asyncio
    async def test_safety_check_clean(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.check", {"content": "Hello world"})
        verdict = resp["result"]["verdict"]
        assert verdict["action"] == "allow"

    @pytest.mark.asyncio
    async def test_safety_check_no_content(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.check", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_safety_evaluate_action(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.evaluate_action", {"category": "code_modification"})
        assert "verdict" in resp["result"]

    @pytest.mark.asyncio
    async def test_safety_evaluate_no_category(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.evaluate_action", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_safety_approve_action_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.approve_action", {"action_id": "nonexistent"})
        assert resp["result"]["approved"] is False

    @pytest.mark.asyncio
    async def test_safety_reject_action_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.reject_action", {"action_id": "nonexistent"})
        assert resp["result"]["rejected"] is False

    @pytest.mark.asyncio
    async def test_safety_get_pending_actions(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "safety.get_pending_actions", {})
        assert "actions" in resp["result"]

    @pytest.mark.asyncio
    async def test_safety_add_policy(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path, "safety.add_policy",
            {"category": "custom_test", "description": "Test policy", "default_level": "L2"},
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
        resp = await _rpc_call(daemon.socket_path, "template.list", {"category": "conversation"})
        assert "templates" in resp["result"]

    @pytest.mark.asyncio
    async def test_template_get_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "template.get", {"template_id": "nonexistent"})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_template_instantiate_no_id(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "template.instantiate", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_template_instantiate_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "template.instantiate", {"template_id": "nonexistent"})
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
        resp = await _rpc_call(daemon.socket_path, "deploy.export", {"graph_id": "nonexistent", "format": "json"})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_deploy_export_json(self, daemon):
        create = await _rpc_call(
            daemon.socket_path, "graph.create",
            {"name": "Export Test", "nodes": [{"id": "start", "type": "start"}, {"id": "end", "type": "end"}], "edges": [{"source_id": "start", "target_id": "end"}]},
        )
        graph_id = create["result"]["graph_id"]
        resp = await _rpc_call(
            daemon.socket_path, "deploy.export",
            {"graph_id": graph_id, "format": "json", "filepath": tempfile.mktemp(suffix=".json")},
        )
        assert resp["result"]["status"] == "ok"
        assert "path" in resp["result"]

    @pytest.mark.asyncio
    async def test_deploy_import_no_filepath(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "deploy.import", {})
        assert resp["result"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_deploy_import_file_not_found(self, daemon):
        resp = await _rpc_call(daemon.socket_path, "deploy.import", {"filepath": "/tmp/nonexistent_abc123.json"})
        assert resp["result"]["status"] == "error"
