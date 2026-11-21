# Issue #791: wire fusion-harness handoff_verify into the teams spawn path
# (FUSION_HANDOFF_VERIFY=1) and failure_attribution (MAST) into the run
# postmortem surface (runs.attribution + run.history_get, gated by
# FUSION_MAST_ATTRIBUTION=1). Default flows unchanged when flags unset.
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.failure_attribution import attribute_run, records_from_events
from tests.test_issue_341_subagent import _llm_response
from tests.test_issue_707_teams_dispatcher import (
    _brief,
    _definition,
    _run,
    _runtime,
)

HONEST_META = {
    "permission_ledger": [
        {"tool": "file_read", "decision": "allow"},
        {"tool": "file_write", "decision": "allow"},
    ],
    "handoff_claims": [{"tool": "file_read"}],
}
FABRICATED_META = {
    "permission_ledger": [{"tool": "file_read", "decision": "allow"}],
    "handoff_claims": [{"tool": "terminal_exec"}],
}


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def _setup(daemon):
    rt, _client = _runtime(responses=[_llm_response(content="member final report")])
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "do the work"})
    return rt, added["task_id"]


class TestHandoffVerifySpawnGate:
    async def test_flag_off_brief_without_metadata_passes(self, daemon, _home, monkeypatch):
        monkeypatch.delenv("FUSION_HANDOFF_VERIFY", raising=False)
        rt, tid = await _setup(daemon)
        res = await _run(
            daemon,
            "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": _brief()},
        )
        assert res["status"] == "ok"
        assert res["result"]["status"] == "completed"

    async def test_flag_on_honest_claims_pass(self, daemon, _home, monkeypatch):
        monkeypatch.setenv("FUSION_HANDOFF_VERIFY", "1")
        rt, tid = await _setup(daemon)
        res = await _run(
            daemon,
            "teams.run_member",
            {
                "team": "pair",
                "member": "worker-a",
                "task_id": tid,
                "brief": _brief(metadata=HONEST_META),
            },
        )
        assert res["status"] == "ok"
        assert res["result"]["status"] == "completed"

    async def test_flag_on_fabricated_claim_blocked_typed(self, daemon, _home, monkeypatch):
        monkeypatch.setenv("FUSION_HANDOFF_VERIFY", "1")
        rt, tid = await _setup(daemon)
        before = rt.kernel["orchestrator"]["runner"]._counter
        res = await _run(
            daemon,
            "teams.run_member",
            {
                "team": "pair",
                "member": "worker-a",
                "task_id": tid,
                "brief": _brief(metadata=FABRICATED_META),
            },
        )
        assert res["status"] == "error"
        assert res["reason_code"] == "handoff_verification_failed"
        assert "terminal_exec" in res["message"]
        assert rt.kernel["orchestrator"]["runner"]._counter == before, "spawn must be blocked"

    async def test_flag_on_missing_payload_blocked_typed(self, daemon, _home, monkeypatch):
        monkeypatch.setenv("FUSION_HANDOFF_VERIFY", "1")
        rt, tid = await _setup(daemon)
        res = await _run(
            daemon,
            "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": _brief()},
        )
        assert res["status"] == "error"
        assert res["reason_code"] == "handoff_verification_failed"
        assert "missing ledger or claims" in res["message"]


def _ev(type_, ts, node_id="n1", name="", content="", args=None):
    return {
        "type": type_,
        "name": name,
        "content": content,
        "args": args or {},
        "node_id": node_id,
        "timestamp": ts,
        "metadata": {},
        "event_id": "",
        "parent_id": "",
    }


class TestFailureAttribution:
    def test_records_from_events_maps_fields(self):
        recs = records_from_events(
            "run-1",
            [
                _ev("tool_call", 1.0, name="terminal", args={"command": "ls"}),
                _ev("error", 2.0, content="boom: timeout"),
            ],
        )
        assert [r.event for r in recs] == ["tool_call", "error"]
        assert recs[1].payload["error"] == "boom: timeout"
        assert recs[0].payload["tool"] == "terminal"

    def test_attribute_run_error_stream(self):
        events = [
            _ev("start", 0.5),
            _ev("tool_call", 1.0, name="terminal", args={"command": "ls"}),
            _ev("tool_call", 2.0, name="terminal", args={"command": "ls"}),
            _ev("error", 3.0, content="run failed: timeout after 30s"),
        ]
        out = attribute_run("run-1", events)
        assert out is not None
        assert out["run_id"] == "run-1"
        assert out["mode"]
        assert out["cluster"]
        assert out["verdicts"]

    def test_attribute_run_clean_stream_none(self):
        assert attribute_run("run-2", [_ev("start", 0.5), _ev("end", 9.0)]) is None

    def test_attribute_run_empty_none(self):
        assert attribute_run("run-3", []) is None

    async def test_rpc_disabled_by_default(self, daemon, monkeypatch):
        monkeypatch.delenv("FUSION_MAST_ATTRIBUTION", raising=False)

        class _Run:
            buffer = [_ev("error", 1.0, content="failed")]

        daemon._managed_run_registry = type(
            "R", (), {"get": staticmethod(lambda rid: _Run() if rid == "run-1" else None)}
        )()
        out = await _run(daemon, "runs.attribution", {"run_id": "run-1"})
        assert out["enabled"] is False
        assert out["attribution"] is None

    async def test_rpc_enabled_returns_verdict(self, daemon, monkeypatch):
        monkeypatch.setenv("FUSION_MAST_ATTRIBUTION", "1")

        class _Run:
            buffer = [
                _ev("tool_call", 1.0, name="terminal", args={"command": "ls"}),
                _ev("error", 2.0, content="run failed: timeout"),
            ]

        daemon._managed_run_registry = type(
            "R", (), {"get": staticmethod(lambda rid: _Run() if rid == "run-1" else None)}
        )()
        out = await _run(daemon, "runs.attribution", {"run_id": "run-1"})
        assert out["enabled"] is True
        assert out["attribution"]["mode"]
        assert out["attribution"]["verdicts"]

    async def test_rpc_unknown_run(self, daemon, monkeypatch):
        monkeypatch.setenv("FUSION_MAST_ATTRIBUTION", "1")
        daemon._managed_run_registry = type("R", (), {"get": staticmethod(lambda rid: None)})()
        out = await _run(daemon, "runs.attribution", {"run_id": "nope"})
        assert out["status"] == "error" if "status" in out else "not found" in out["error"]

    async def test_history_get_failed_run_carries_attribution(self, daemon, _home, monkeypatch):
        from agent_runtime.run_history import RunHistoryStore

        monkeypatch.setenv("FUSION_MAST_ATTRIBUTION", "1")
        store = RunHistoryStore(db_path=str(_home.parent / "history.db"))
        daemon._run_history_store = store
        frames = [
            _ev("tool_call", 1.0, name="terminal", args={"command": "ls"}),
            _ev("error", 2.0, content="run failed: timeout"),
        ]
        store.save_run("run-9", "g1", "Graph One", frames)
        out = await _run(daemon, "run.history_get", {"run_id": "run-9"})
        run = out["run"]
        assert run["status"] == "failed"
        assert "failure_attribution" in run
        assert run["failure_attribution"]["mode"]

    async def test_history_get_failed_run_no_attribution_when_disabled(
        self, daemon, _home, monkeypatch
    ):
        from agent_runtime.run_history import RunHistoryStore

        monkeypatch.delenv("FUSION_MAST_ATTRIBUTION", raising=False)
        store = RunHistoryStore(db_path=str(_home.parent / "history.db"))
        daemon._run_history_store = store
        store.save_run("run-10", "g1", "Graph One", [_ev("error", 1.0, content="boom")])
        out = await _run(daemon, "run.history_get", {"run_id": "run-10"})
        assert "failure_attribution" not in out["run"]
