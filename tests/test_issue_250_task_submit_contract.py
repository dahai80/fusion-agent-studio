"""Tests for #250 — fusion-event task.submit frozen-contract E2E verification.

Verifies fusion-agent-studio implements task.submit with the exact request/response
shape fusion-event (upstream caller) sends. Contract authority: fusion-event (D-10).
Freezes the wire shape so both sides reconcile without drift.

Runner: pytest tests/test_issue_250_task_submit_contract.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.task_store import TaskStore
from agent_runtime.trigger_input import parse_trigger_input


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
    d._task_store = TaskStore(db_path=str(tmp_path / "test_tasks_250.db"))
    yield d
    await d.stop()


def _frozen_input() -> str:
    # #250 冻结契约: input 是 JSON-encoded string (非嵌套对象),
    # fusion-event 序列化后发。schema 见 trigger_input.py。
    return json.dumps(
        {
            "trigger_id": "trig-uuid-1",
            "event": {
                "event_id": "evt-uuid-1",
                "type": "fileModified",
                "target_path": "/Users/x/proj/a.swift",
                "timestamp": 1693027200000,
                "payload": {"diff_lines": 12},
                "node_id": "macbook",
            },
            "context": "prior session memory",
            "rule_name": "swift-watch",
            "node_id": "macbook",
        }
    )


# ── E2E: 冻结请求形状往返 ────────────────────────────────────


@pytest.mark.asyncio
async def test_task_submit_accepts_frozen_contract(daemon, socket_path):
    # 逐字段对齐 #250 契约请求 params。
    params = {
        "title": "event:swift-watch",
        "description": "fileModified @ /Users/x/proj/a.swift",
        "agent_id": "fusion-code",
        "graph_id": "",
        "input": _frozen_input(),
        "trigger": "immediate",
        "priority": 0,
        "idempotency_key": "sha256-node-scoped-key-1",
    }
    r = await _rpc_call(socket_path, "task.submit", params)
    # #250 期望: result.task.task_id (string), error=null/absent。
    assert r["jsonrpc"] == "2.0"
    assert "error" not in r or r["error"] is None
    assert r["result"]["status"] == "ok"
    task = r["result"]["task"]
    assert isinstance(task["task_id"], str)
    assert task["task_id"]
    assert task["title"] == "event:swift-watch"
    assert task["agent_id"] == "fusion-code"
    assert task["trigger"] == "immediate"
    assert task["priority"] == 0
    assert task["idempotency_key"] == "sha256-node-scoped-key-1"


@pytest.mark.asyncio
async def test_task_submit_response_path_result_task_task_id(daemon, socket_path):
    # #250 Q3: 确认响应路径 result.task.task_id (非扁平 task_id)。
    r = await _rpc_call(
        socket_path,
        "task.submit",
        {
            "title": "probe",
            "agent_id": "fusion-code",
            "input": _frozen_input(),
            "idempotency_key": "k-path-1",
        },
    )
    assert "task" in r["result"]
    assert "task_id" in r["result"]["task"]
    assert isinstance(r["result"]["task"]["task_id"], str)


@pytest.mark.asyncio
async def test_task_submit_input_json_string_decodes(daemon, socket_path):
    # #250 Q2: input 是 JSON string, agent-studio 存原串, DAG 节点经
    # parse_trigger_input 解码。验证存原串 + 可解冻结构。
    raw = _frozen_input()
    r = await _rpc_call(
        socket_path,
        "task.submit",
        {
            "title": "decode-probe",
            "agent_id": "fusion-code",
            "input": raw,
            "idempotency_key": "k-decode-1",
        },
    )
    assert r["result"]["task"]["input"] == raw
    parsed = parse_trigger_input(r["result"]["task"]["input"])
    assert parsed is not None
    assert parsed.trigger_id == "trig-uuid-1"
    assert parsed.event.type == "fileModified"
    assert parsed.rule_name == "swift-watch"


@pytest.mark.asyncio
async def test_task_submit_idempotency_key_dedup(daemon, socket_path):
    # #250 Q4: 同 idempotency_key 去重, 返同一 task_id + deduped=True。
    params = {
        "title": "event:swift-watch",
        "agent_id": "fusion-code",
        "input": _frozen_input(),
        "trigger": "immediate",
        "idempotency_key": "sha256-dup-key",
    }
    r1 = await _rpc_call(socket_path, "task.submit", params)
    first_id = r1["result"]["task"]["task_id"]
    assert r1["result"]["task"]["deduped"] is False

    r2 = await _rpc_call(socket_path, "task.submit", params)
    assert r2["result"]["task"]["task_id"] == first_id
    assert r2["result"]["task"]["deduped"] is True


@pytest.mark.asyncio
async def test_task_submit_empty_graph_id_accepted(daemon, socket_path):
    # #250 契约: graph_id 可为空串 (fusion-event 发空)。
    r = await _rpc_call(
        socket_path,
        "task.submit",
        {
            "title": "no-graph",
            "agent_id": "fusion-code",
            "graph_id": "",
            "input": _frozen_input(),
            "idempotency_key": "k-empty-graph",
        },
    )
    assert r["result"]["status"] == "ok"
    assert r["result"]["task"]["graph_id"] == ""
