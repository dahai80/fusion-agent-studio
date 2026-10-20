"""Issue #402 — cowork_workflow_run tool: run cowork workflows as agent-studio tools."""

from __future__ import annotations

import functools
import json

import httpx
import pytest

import tools.cowork_tools as cw
from tools import create_default_registry
from tools.cowork_tools import CoworkWorkflowRunTool


def _tool() -> CoworkWorkflowRunTool:
    registry = create_default_registry()
    assert registry.has("cowork_workflow_run")
    return registry.get("cowork_workflow_run")


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cw.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


def _ok_run_record(status="running", steps=None, artifacts=None):
    return {
        "run_id": "run_abc123",
        "workflow_id": "wf_test",
        "workflow_name": "test wf",
        "status": status,
        "created_at": 0.0,
        "finished_at": None,
        "steps": steps or [],
        "result": None,
        "error": None,
        "review": None,
        "artifacts": artifacts or [],
    }


class FakeCowork:
    def __init__(self, run_records=None, approvals=None):
        self.run_records = list(run_records or [])
        self.approvals = list(approvals or [])
        self.poll_index = 0
        self.run_bodies = []
        self.generate_called = 0
        self.headers_seen = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers_seen = dict(request.headers)
        path = request.url.path
        if path == "/api/ai/generate-workflow":
            self.generate_called += 1
            body = json.loads(request.content)
            if not body.get("prompt"):
                return httpx.Response(400, json={"detail": {"code": "bad_request", "message": "prompt 必填"}})
            return httpx.Response(200, json={"workflow": {"name": "gen wf", "nodes": [{"id": "n1", "name": "llm"}], "edges": []}})
        if path == "/api/runs" and request.method == "POST":
            self.run_bodies.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": "run_abc123", "status": "running"})
        if path == "/api/runs/run_abc123" and request.method == "GET":
            record = self.run_records[min(self.poll_index, len(self.run_records) - 1)]
            self.poll_index += 1
            return httpx.Response(200, json=record)
        if path == "/api/permissions/pending":
            return httpx.Response(200, json={"approvals": self.approvals})
        return httpx.Response(404, json={"detail": {"code": "not_found", "message": path}})


def test_tool_registered_and_schema_valid():
    tool = _tool()
    assert tool.name == "cowork_workflow_run"
    schema = tool.openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "cowork_workflow_run"
    assert "workflow_id" in schema["function"]["parameters"]["properties"]
    assert "prompt" in schema["function"]["parameters"]["properties"]
    assert "inputs" in schema["function"]["parameters"]["properties"]
    assert "wait" in schema["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_run_by_workflow_id_wait_success(monkeypatch):
    steps = [
        {"node_id": "n1", "node_name": "llm", "status": "success", "output_data": {"markdown": "hello"}},
        {"node_id": "n2", "node_name": "end", "status": "success", "output_data": {"text": "done"}},
    ]
    record = _ok_run_record(status="success", steps=steps, artifacts=[{"node_id": "n1", "name": "a.md", "type": "markdown", "path": "/tmp/a.md", "truncated": False}])
    record["result"] = {"summary": "ok"}
    fake = FakeCowork(run_records=[record])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test"))
    assert out["run_id"] == "run_abc123"
    assert out["status"] == "success"
    assert out["summary"] == "ok"
    assert out["node_outputs"]["n1"] == {"markdown": "hello"}
    assert out["node_outputs"]["n2"] == {"text": "done"}
    assert out["artifacts"][0]["name"] == "a.md"
    assert fake.run_bodies[0]["workflow_id"] == "wf_test"


@pytest.mark.asyncio
async def test_run_fire_and_forget(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record()])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test", wait=False))
    assert out["mode"] == "fire_and_forget"
    assert out["run_id"] == "run_abc123"
    assert "action=status" in out["hint"]


@pytest.mark.asyncio
async def test_run_requires_workflow_id_or_prompt():
    out = json.loads(await _tool().execute())
    assert "error" in out
    assert "workflow_id or prompt" in out["error"]


@pytest.mark.asyncio
async def test_run_nl_prompt_generates_workflow(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record(status="success")])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(prompt="summarize daily reports"))
    assert out["status"] == "success"
    assert fake.generate_called == 1
    assert fake.run_bodies[0]["workflow"]["name"] == "gen wf"


@pytest.mark.asyncio
async def test_run_forwards_inputs(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record(status="success")])
    _mock_client(monkeypatch, fake.handler)
    await _tool().execute(workflow_id="wf_test", inputs={"city": "Beijing"}, timeout_seconds=2)
    assert fake.run_bodies[0]["inputs"] == {"city": "Beijing"}


@pytest.mark.asyncio
async def test_status_action_polls_existing_run(monkeypatch):
    record = _ok_run_record(status="failed")
    record["error"] = "node n1 boom"
    fake = FakeCowork(run_records=[record])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(action="status", run_id="run_abc123"))
    assert out["status"] == "failed"
    assert out["error"] == "node n1 boom"
    assert "pending" not in out


@pytest.mark.asyncio
async def test_status_action_requires_run_id():
    out = json.loads(await _tool().execute(action="status"))
    assert "error" in out
    assert "run_id is required" in out["error"]


@pytest.mark.asyncio
async def test_paused_returns_pending_not_block(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record(status="paused")])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test"))
    assert out["status"] == "paused"
    assert out["pending"] is True
    assert "action=status" in out["hint"]


@pytest.mark.asyncio
async def test_approval_stall_returns_pending_approval(monkeypatch):
    stalled = _ok_run_record(status="running", steps=[{"node_id": "n1", "status": "running", "output_data": {}}])
    fake = FakeCowork(run_records=[stalled], approvals=[{"action_id": "perm_1", "tool_name": "terminal", "source": "guard"}])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test", stall_seconds=3, timeout_seconds=20))
    assert out["status"] == "pending_approval"
    assert out["pending"] is True
    assert out["approvals"][0]["action_id"] == "perm_1"
    assert "confirm in cowork UI" in out["hint"]


@pytest.mark.asyncio
async def test_wait_timeout_returns_pollable_status(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record(status="running")])
    _mock_client(monkeypatch, fake.handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test", timeout_seconds=1))
    assert out["status"] == "running"
    assert out["pending"] is True
    assert "action=status" in out["hint"]


@pytest.mark.asyncio
async def test_upstream_unreachable_visible_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _mock_client(monkeypatch, handler)
    out = json.loads(await _tool().execute(workflow_id="wf_test"))
    assert "unreachable" in out["error"]
    assert "FUSION_COWORK_URL" in out["hint"]


@pytest.mark.asyncio
async def test_upstream_http_error_surfaces_detail(monkeypatch):
    def handler(request):
        if request.url.path == "/api/runs":
            return httpx.Response(404, json={"detail": {"code": "not_found", "message": "工作流不存在: wf_missing"}})
        return httpx.Response(200, json={})

    _mock_client(monkeypatch, handler)
    out = json.loads(await _tool().execute(workflow_id="wf_missing"))
    assert "404" in out["error"]
    assert "工作流不存在" in out["error"]


@pytest.mark.asyncio
async def test_api_key_header_from_env(monkeypatch):
    fake = FakeCowork(run_records=[_ok_run_record(status="success")])
    _mock_client(monkeypatch, fake.handler)
    monkeypatch.setenv(cw.COWORK_API_KEY_ENV, "secret-key")
    await _tool().execute(workflow_id="wf_test", timeout_seconds=2)
    assert fake.headers_seen.get("x-api-key") == "secret-key"


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv(cw.COWORK_URL_ENV, "http://10.0.0.5:11461")
    assert cw._base_url() == "http://10.0.0.5:11461"
    monkeypatch.delenv(cw.COWORK_URL_ENV)
    assert cw._base_url() == cw.DEFAULT_COWORK_URL
