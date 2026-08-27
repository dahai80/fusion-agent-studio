"""Tests for #240 task.submit input payload schema + parse_trigger_input helper.

Runner: pytest tests/test_issue_240_trigger_input.py
Schema: frozen JSON-encoded `input` string (trigger_id/event/context/rule_name/node_id).
Helper: parse_trigger_input -> TriggerInput | None (None = backward-compat fallback).

User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.trigger_input import (
    TriggerInput,
    parse_trigger_input,
)

_VALID_INPUT = {
    "trigger_id": "trig-uuid-1",
    "event": {
        "event_id": "evt-uuid-1",
        "type": "fileModified",
        "target_path": "/Users/dahai/src/a.swift",
        "timestamp": 1693027200000,
        "payload": {"key": "value"},
        "node_id": "macbook",
    },
    "context": "recent swift edits",
    "rule_name": "swift-watch",
    "node_id": "macbook",
}


# --- helper: parse valid schema ---


def test_parse_valid_schema():
    tri = parse_trigger_input(json.dumps(_VALID_INPUT))
    assert tri is not None
    assert tri.trigger_id == "trig-uuid-1"
    assert tri.rule_name == "swift-watch"
    assert tri.node_id == "macbook"
    assert tri.context == "recent swift edits"
    assert tri.event.event_id == "evt-uuid-1"
    assert tri.event.type == "fileModified"
    assert tri.event.target_path == "/Users/dahai/src/a.swift"
    assert tri.event.timestamp == 1693027200000
    assert tri.event.payload == {"key": "value"}
    assert tri.event.node_id == "macbook"


# --- helper: backward compat (None on parse failure) ---


def test_parse_empty_returns_none():
    assert parse_trigger_input("") is None


def test_parse_non_json_returns_none():
    assert parse_trigger_input("just a plain string") is None


def test_parse_json_array_returns_none():
    assert parse_trigger_input("[1, 2, 3]") is None


def test_parse_partial_schema_fills_defaults():
    tri = parse_trigger_input(json.dumps({"trigger_id": "only-id"}))
    assert tri is not None
    assert tri.trigger_id == "only-id"
    assert tri.event.type == ""
    assert tri.context == ""
    assert tri.rule_name == ""


# --- roundtrip ---


def test_trigger_input_roundtrip():
    tri = TriggerInput.from_dict(_VALID_INPUT)
    d = tri.to_dict()
    assert d["trigger_id"] == "trig-uuid-1"
    assert d["event"]["type"] == "fileModified"
    assert d["event"]["payload"] == {"key": "value"}
    # re-parse the serialized form
    tri2 = parse_trigger_input(json.dumps(d))
    assert tri2 is not None
    assert tri2.trigger_id == tri.trigger_id
    assert tri2.event.target_path == tri.event.target_path


def test_trigger_event_payload_non_dict_safe():
    tri = parse_trigger_input(
        json.dumps({"trigger_id": "x", "event": {"payload": "not-a-dict"}})
    )
    assert tri is not None
    assert tri.event.payload == {}


def test_trigger_event_timestamp_coerced():
    tri = parse_trigger_input(
        json.dumps({"trigger_id": "x", "event": {"timestamp": "9999"}})
    )
    assert tri is not None
    assert tri.event.timestamp == 9999


# --- DAG consumer wiring: cron handler uses helper (daemon-level) ---


@pytest.mark.asyncio
async def test_cron_handler_parses_trigger_input(tmp_path, monkeypatch):
    # minimal fake job carrying a schema-shaped input_data
    from agent_runtime.daemon_server import DaemonServer

    captured = {}

    class _FakeJob:
        id = "job_1"
        graph_id = "g1"
        input_data = json.dumps(_VALID_INPUT)

    d = DaemonServer(
        socket_path="/tmp/none.sock",
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "s.db"),
    )

    async def fake_execute(params):
        captured["variables"] = params.get("variables", {})
        return {"status": "ok", "events": []}

    monkeypatch.setattr(d, "_handle_graph_execute", fake_execute)

    result = await d._cron_default_handler(_FakeJob())
    assert result["status"] == "ok"
    vars_ = captured["variables"]
    assert vars_["trigger_id"] == "trig-uuid-1"
    assert vars_["context"] == "recent swift edits"
    assert "input" in vars_


@pytest.mark.asyncio
async def test_cron_handler_falls_back_on_freeform(tmp_path, monkeypatch):
    from agent_runtime.daemon_server import DaemonServer

    captured = {}

    class _FakeJob:
        id = "job_2"
        graph_id = "g1"
        input_data = json.dumps({"task_id": "t_legacy", "foo": "bar"})

    d = DaemonServer(
        socket_path="/tmp/none.sock",
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "s.db"),
    )

    async def fake_execute(params):
        captured["variables"] = params.get("variables", {})
        return {"status": "ok", "events": []}

    monkeypatch.setattr(d, "_handle_graph_execute", fake_execute)

    await d._cron_default_handler(_FakeJob())
    # freeform dict (no trigger_id) -> legacy path, task_id threaded through
    assert captured["variables"].get("task_id") == "t_legacy"
