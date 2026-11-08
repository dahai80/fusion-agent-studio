"""Tests for issue #652 — consumer-driven contract tests for run/seat API.

Importers: pytest runner
Spec: verification-infra-spec-1108.md §3.1 contract-first — cowork
studio_run nodes consume POST /v1/runs, GET /v1/runs/{id},
GET /v1/runs/{id}/result, POST /v1/graphs/{id}/execute. Fixture files in
tests/contract/fixtures/ are the single source of truth (shared with the
cowork pack); any payload-shape PR that diverges fails here with the
diff named.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

FIXTURES = os.path.join(os.path.dirname(__file__), "contract", "fixtures")


def _load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def assert_keys_match(live: dict, fixture: dict, ctx: str) -> None:
    # #652: on divergence name the broken fields — CI red must show the
    # diff, not a bare mismatch.
    expect = {k for k in fixture if not k.startswith("_")}
    actual = set(live)
    missing = sorted(expect - actual)
    extra = sorted(actual - expect)
    assert not missing and not extra, (
        f"contract broken at {ctx}: "
        f"missing fields {missing}, unexpected fields {extra} — "
        "update tests/contract/fixtures/ in the same PR and mirror the "
        "change in the cowork consumer pack"
    )


def _fake_api(monkeypatch, task: dict):
    from agent_runtime import api_server

    async def fake_rpc(method, params):
        if method in ("task.submit", "task.get", "task.status"):
            return {"task": task, "deduped": False}
        if method == "subagent.run_task":
            return {"status": "ok", "task_id": params.get("task_id")}
        if method == "graph.execute_async":
            return {"status": "ok"}
        return {"status": "ok"}

    monkeypatch.setattr(api_server, "_rpc", fake_rpc)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    fake_audit = type("A", (), {"log_action": staticmethod(lambda **kw: None)})()
    monkeypatch.setattr(api_server, "_get_audit_logger", lambda: fake_audit)
    return TestClient(api_server.app)


def _task_from_fixtures() -> dict:
    status = _load("run_status_response.json")
    result = _load("run_result_response.json")
    return {
        "task_id": status["run_id"],
        "status": status["status"],
        "last_error": result["last_error"],
        "last_result": result["last_result"],
        "agent_id": status["agent_id"],
        "graph_id": status["graph_id"],
        "created_at": status["created_at"],
        "updated_at": status["updated_at"],
        "artifact_ids": result["artifacts"],
        "retry_count": status["progress"]["retry_count"],
    }


class TestRunsContract:
    def test_submit_request_fixture_is_valid_payload(self, monkeypatch):
        # The fixture body must be accepted as-is (400/422 = fixture drift).
        tc = _fake_api(monkeypatch, _task_from_fixtures())
        body = {k: v for k, v in _load("runs_submit_request.json").items() if not k.startswith("_")}
        resp = tc.post("/v1/runs", json=body)
        assert resp.status_code == 200, resp.text

    def test_submit_response_matches_fixture(self, monkeypatch):
        tc = _fake_api(monkeypatch, _task_from_fixtures())
        body = {k: v for k, v in _load("runs_submit_request.json").items() if not k.startswith("_")}
        resp = tc.post("/v1/runs", json=body).json()
        assert_keys_match(resp, _load("runs_submit_response.json"), "POST /v1/runs response")
        assert resp["run_id"] and isinstance(resp["deduped"], bool)

    def test_run_status_response_matches_fixture(self, monkeypatch):
        tc = _fake_api(monkeypatch, _task_from_fixtures())
        resp = tc.get("/v1/runs/run-1").json()
        assert_keys_match(resp, _load("run_status_response.json"), "GET /v1/runs/{id}")
        assert_keys_match(resp["progress"], _load("run_status_response.json")["progress"], "GET /v1/runs/{id} progress")

    def test_run_result_response_matches_fixture(self, monkeypatch):
        tc = _fake_api(monkeypatch, _task_from_fixtures())
        resp = tc.get("/v1/runs/run-1/result").json()
        assert_keys_match(resp, _load("run_result_response.json"), "GET /v1/runs/{id}/result")
        assert_keys_match(resp["last_result"], _load("run_result_response.json")["last_result"], "run result last_result")

    def test_graph_execute_request_fixture_graph_id_mandatory(self, monkeypatch):
        # cow#135 F-1: graph_id in the body is required — missing -> 422.
        from agent_runtime.api_server import GraphExecuteRequest

        with pytest.raises(Exception):
            GraphExecuteRequest(**{"input_text": "no graph id"})
        body = {k: v for k, v in _load("graph_execute_request.json").items() if not k.startswith("_")}
        req = GraphExecuteRequest(**body)
        assert req.graph_id == "graph-1"

    def test_graph_execute_response_matches_fixture(self):
        from agent_runtime.api_server import ExecutionResponse

        body = {k: v for k, v in _load("graph_execute_response.json").items() if not k.startswith("_")}
        model = ExecutionResponse(**body)
        assert model.status == "completed"
        assert list(body) == ["session_id", "status", "events"]


class TestStatusSemanticsContract:
    def test_statuses_valid_in_task_store(self):
        from agent_runtime.task_store import _VALID_STATUSES

        sem = _load("status_semantics.json")
        pinned = set(sem["terminal_statuses"]) | set(sem["non_terminal_statuses"])
        assert pinned <= _VALID_STATUSES, f"fixture names unknown statuses: {pinned - _VALID_STATUSES}"

    def test_report_only_completion_warning_pinned(self):
        # as#643: the warning string is a consumed field — renaming it
        # breaks cowork's transcript rendering.
        from agent_runtime.subagent import SubAgentReport

        sem = _load("status_semantics.json")
        report = SubAgentReport(name="x", ok=True, report="r", warning="max turns reached, report-only completion")
        d = report.to_dict()
        assert d["warning"] == sem["status_details"]["completed_max_turns"]["last_result.warning"]

    def test_parked_managed_run_fields_pinned(self):
        # as#646: ManagedRun.park_status() shape + the parked event are
        # consumed by orchestrators — pin exact fields.
        from agent_runtime.run_registry import ManagedRun

        sem = _load("status_semantics.json")["status_details"]["parked_managed_run"]
        run = ManagedRun(graph_id="g1")
        run.park(123.0, sem["park_reason"], {"graph_id": "g1"})
        status = run.park_status()
        assert_keys_match(
            status,
            {k: 0 for k in sem["park_status_fields"]},
            "ManagedRun.park_status()",
        )
        assert status["parked"] is True
        assert status["park_reason"] == "usage_limit"
        assert status["resumes_at"] == 123.0
        event = sem["event"]
        assert set(event) == {"type", "park_reason", "resumes_at", "message"}

    def test_budget_exhaustion_marker_matches(self):
        # as#640: enriched failure text must keep the cowork-recovery
        # marker 'max iterations' (case-insensitive match).
        sem = _load("status_semantics.json")
        marker = sem["status_details"]["failed"]["last_error_marker_budget_exhausted"]
        sample = "Max iterations exceeded after 24/24 turns (249019 tokens, report_len=0)"
        assert marker in sample.lower()

    def test_fixtures_have_contract_notes(self):
        # Every fixture self-documents its boundary — no anonymous pins.
        for name in sorted(os.listdir(FIXTURES)):
            with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            assert "_contract" in data, f"{name} missing _contract note"
