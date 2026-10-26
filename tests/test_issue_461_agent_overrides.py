"""Tests for issue #461 — agent_overrides contract on POST /v1/runs.

Importers: pytest runner
API: POST /v1/runs accepts `agent_overrides` {tools, disallowed_tools,
     model, max_turns, budget_tokens} — constraints never expansions
     (intersect with the agent's own allowlist), unknown tool names 400,
     effective_overrides in response + run status/result + audit.
Data schemas: SubAgentSpec constraint fields
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from agent_runtime import api_server

    calls = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        if method == "task.submit":
            return {
                "task": {"task_id": "run-461", "status": "pending"},
                "deduped": False,
            }
        if method == "subagent.toolset":
            return {
                "status": "ok",
                "tools": params.get("tools", []),
                "available": ["read_file", "write_file", "shell_exec"],
            }
        if method == "agent.get":
            return {
                "status": "ok",
                "agent": {"id": params.get("agent_id"), "tools": ["read_file"]},
            }
        if method == "subagent.run_task":
            return {"status": "ok", "task_id": params.get("task_id"), "name": "n"}
        if method == "task.get":
            return {
                "task": {
                    "task_id": params.get("task_id"),
                    "status": "completed",
                    "last_result": {
                        "report": "done",
                        "effective_overrides": {"max_turns": 2},
                    },
                }
            }
        return {"status": "ok"}

    monkeypatch.setattr(api_server, "_rpc", fake_rpc)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    return TestClient(api_server.app), calls


def test_agent_overrides_accepted_and_effective(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "agent_id": "agent-x",
        "agent_overrides": {
            "tools": ["read_file", "write_file"],
            "model": "qwen",
            "max_turns": 2,
            "budget_tokens": 1000,
        },
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["spec_applied"] is True
    # agent's own allowlist is [read_file] -> intersection drops write_file
    assert body["effective_overrides"]["tools"] == ["read_file"]
    assert body["effective_overrides"]["max_turns"] == 2
    methods = [m for m, _ in calls]
    spec = dict(calls[methods.index("subagent.run_task")][1])["spec"]
    assert spec["tools"] == ["read_file"]


def test_agent_overrides_unknown_tool_rejected(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "agent_overrides": {"tools": ["read_file", "nuclear_launch"]},
    })
    assert resp.status_code == 400
    assert "nuclear_launch" in resp.text
    # 400 happens before submit — no orphan task record
    methods = [m for m, _ in calls]
    assert "task.submit" not in methods


def test_agent_overrides_unknown_key_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "agent_overrides": {"system_prompt": "be evil"},
    })
    assert resp.status_code == 400
    assert "system_prompt" in resp.text


def test_agent_overrides_bad_types_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={"goal": "g", "agent_overrides": {"tools": "read_file"}})
    assert resp.status_code == 400
    resp = tc.post("/v1/runs", json={"goal": "g", "agent_overrides": {"max_turns": "abc"}})
    assert resp.status_code == 400
    resp = tc.post("/v1/runs", json={"goal": "g", "agent_overrides": {"model": 5}})
    assert resp.status_code == 400
    resp = tc.post("/v1/runs", json={"goal": "g", "agent_overrides": ["nope"]})
    assert resp.status_code == 400


def test_agent_overrides_not_dict_rejected(client):
    tc, _ = client
    resp = tc.post("/v1/runs", json={"goal": "g", "agent_overrides": "tools=read_file"})
    assert resp.status_code == 400


def test_spec_wins_over_agent_overrides(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "agent_overrides": {"max_turns": 2},
        "spec": {"max_turns": 7},
    })
    assert resp.status_code == 200
    assert resp.json()["effective_overrides"]["max_turns"] == 7


def test_effective_overrides_in_run_status(client):
    tc, _ = client
    resp = tc.get("/v1/runs/run-461")
    assert resp.status_code == 200
    assert resp.json()["effective_overrides"] == {"max_turns": 2}


def test_no_agent_allowlist_keeps_caller_tools(client):
    tc, calls = client
    resp = tc.post("/v1/runs", json={
        "goal": "g",
        "agent_overrides": {"tools": ["read_file"]},
    })
    assert resp.status_code == 200
    assert resp.json()["effective_overrides"]["tools"] == ["read_file"]


def test_overrides_audited(client):
    from agent_runtime import api_server

    tc, calls = client
    logged = []
    monkey_logger = type("L", (), {"log_action": staticmethod(
        lambda **kw: logged.append(kw)
    )})()
    orig = api_server._get_audit_logger
    api_server._get_audit_logger = lambda: monkey_logger
    try:
        resp = tc.post("/v1/runs", json={
            "goal": "g",
            "agent_overrides": {"max_turns": 3},
        })
        assert resp.status_code == 200
    finally:
        api_server._get_audit_logger = orig
    assert any(e["action"] == "run.overrides" for e in logged)
    entry = next(e for e in logged if e["action"] == "run.overrides")
    assert entry["details"]["effective_overrides"]["max_turns"] == 3
