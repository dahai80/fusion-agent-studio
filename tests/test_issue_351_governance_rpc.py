"""#351 R4: governance page RPC surface — budget.status/set + SLO via bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC_KEY = "fk-static-test-351"


class _StubDaemon:
    def __init__(self):
        self.calls = []

    def _get_handler(self, method):
        known = {
            "budget.status": self._handle_budget_status,
            "budget.set": self._handle_budget_set,
            "telemetry.session_slo": self._handle_slo,
            "telemetry.metrics": self._handle_metrics,
            "permission.list": self._handle_perms,
            "hooks.list": self._handle_hooks,
        }
        return known.get(method)

    async def _handle_budget_status(self, params):
        self.calls.append("budget.status")
        return {"max_tokens": 100000, "spent_tokens": 42000, "exceeded": False}

    async def _handle_budget_set(self, params):
        self.calls.append(("budget.set", params.get("max_tokens")))
        return {"max_tokens": params.get("max_tokens", 0), "spent_tokens": 0, "exceeded": False}

    async def _handle_slo(self, params):
        self.calls.append("telemetry.session_slo")
        return {"sessions_completed": 3, "compactions_total": 1, "approvals_total": 2}

    async def _handle_metrics(self, params):
        self.calls.append("telemetry.metrics")
        return {"counters": {}, "latencies": {}}

    async def _handle_perms(self, params):
        self.calls.append("permission.list")
        return {"permissions": [], "denied_tools": [], "tools": ["shell"]}

    async def _handle_hooks(self, params):
        self.calls.append("hooks.list")
        return {"hooks": []}


@pytest.fixture
def static_key_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", STATIC_KEY)
    fake_home = tmp_path / "home"
    (fake_home / ".fusion-agent-studio").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return STATIC_KEY


@pytest.fixture
def stub_daemon():
    from agent_runtime import api_server

    daemon = _StubDaemon()
    api_server.set_daemon(daemon)
    yield daemon
    api_server.set_daemon(None)


def _client():
    from fastapi.testclient import TestClient

    from agent_runtime.api_server import app

    return TestClient(app)


class TestGovernanceBridge:
    def test_budget_status_allowed(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "budget.status", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["max_tokens"] == 100000

    def test_budget_set_allowed_and_audited(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "budget.set", "params": {"max_tokens": 555000}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["max_tokens"] == 555000
        assert ("budget.set", 555000) in stub_daemon.calls

    def test_telemetry_session_slo_allowed(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "telemetry.session_slo", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["sessions_completed"] == 3

    def test_telemetry_metrics_allowed(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "telemetry.metrics", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200

    def test_permission_list_allowed(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "permission.list", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200

    def test_hooks_list_allowed(self, static_key_env, stub_daemon):
        resp = _client().post(
            "/v1/rpc",
            json={"method": "hooks.list", "params": {}},
            headers={"x-api-key": STATIC_KEY},
        )
        assert resp.status_code == 200

    def test_no_key_401(self, static_key_env, stub_daemon):
        resp = _client().post("/v1/rpc", json={"method": "budget.status", "params": {}})
        assert resp.status_code == 401


def test_allowlist_contains_governance_methods():
    from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

    for m in (
        "budget.status",
        "budget.set",
        "telemetry.session_slo",
        "telemetry.metrics",
        "permission.list",
        "hooks.list",
    ):
        assert m in WEB_RPC_ALLOWLIST
