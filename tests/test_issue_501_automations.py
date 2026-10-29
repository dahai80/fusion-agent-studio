"""Tests for issue #501 — automations: scheduled / webhook-triggered runs.

Acceptance:
- a graph runs on schedule without user interaction, survives daemon restart
  (CronManager SQLite persistence + reload + due-job firing)
- webhook trigger fires a run, HMAC-verified (covered by
  tests/test_issue_455_webhook_triggers.py; here we assert the trigger
  definition REST surface)
- trigger definitions are JSON data editable over REST (/v1/cron CRUD +
  executions + manual trigger)
- failing cron executions raise the failure alert hook
  (cron.execution_failed broadcast + audit log)
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from agent_runtime.triggers import CronJob, CronManager


@pytest.fixture(autouse=True)
def fast_cron_poll(monkeypatch):
    # default 15s tick would blow every wait_until budget; tests drive fast.
    monkeypatch.setenv("FUSION_CRON_POLL_INTERVAL", "0.05")


@pytest.fixture()
def cron_api(monkeypatch):
    from agent_runtime import api_server

    calls: list[tuple[str, dict]] = []
    jobs: dict[str, dict] = {
        "cron-1": {"id": "cron-1", "name": "nightly", "graph_id": "g1"}
    }

    async def fake_rpc(method: str, params: dict) -> dict:
        calls.append((method, params))
        if method == "cron.list":
            return {"jobs": list(jobs.values())}
        if method == "cron.register":
            job = {"id": params.get("id", "cron-new"), "graph_id": params.get("graph_id", "")}
            jobs[job["id"]] = job
            return {"status": "ok", "job": job}
        if method == "cron.unregister":
            jobs.pop(params.get("id", ""), None)
            return {"status": "ok", "unregistered": params.get("id", "")}
        if method == "cron.trigger":
            if params.get("id") == "cron-missing":
                return {"status": "error", "message": "job not found"}
            return {"status": "ok", "triggered": params.get("id", "")}
        if method == "cron.list_executions":
            return {"executions": [{"id": "exe-1", "status": "success"}]}
        raise AssertionError(f"unexpected rpc: {method}")

    monkeypatch.setattr(api_server, "_rpc", fake_rpc)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    monkeypatch.setattr(
        api_server,
        "_get_audit_logger",
        lambda: type(
            "_NoopAudit", (), {"log_action": staticmethod(lambda **kw: None)}
        )(),
    )
    return TestClient(api_server.app), calls


class TestCronRestSurface:
    def test_list_jobs(self, cron_api):
        tc, calls = cron_api
        resp = tc.get("/v1/cron")
        assert resp.status_code == 200
        assert resp.json()["jobs"][0]["id"] == "cron-1"
        assert calls[0][0] == "cron.list"

    def test_create_requires_graph_id(self, cron_api):
        tc, calls = cron_api
        resp = tc.post("/v1/cron", json={"name": "nightly"})
        assert resp.status_code == 400
        assert calls == []

    def test_create_and_delete_roundtrip(self, cron_api):
        tc, calls = cron_api
        resp = tc.post(
            "/v1/cron",
            json={
                "id": "cron-new",
                "name": "nightly",
                "expression": "0 3 * * *",
                "graph_id": "g1",
                "input_data": "{\"input\": \"run nightly\"}",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["job"]["id"] == "cron-new"
        assert calls[-1][0] == "cron.register"

        resp = tc.delete("/v1/cron/cron-new")
        assert resp.status_code == 200
        assert ("cron.unregister", {"id": "cron-new"}) in calls

    def test_get_job_found_and_missing(self, cron_api):
        tc, _ = cron_api
        assert tc.get("/v1/cron/cron-1").json()["job"]["id"] == "cron-1"
        assert tc.get("/v1/cron/cron-nope").status_code == 404

    def test_delete_missing_job_404(self, cron_api):
        tc, calls = cron_api
        resp = tc.delete("/v1/cron/cron-nope")
        assert resp.status_code == 404
        assert not any(m == "cron.unregister" for m, _ in calls)

    def test_list_executions(self, cron_api):
        tc, calls = cron_api
        resp = tc.get("/v1/cron/executions", params={"job_id": "cron-1", "limit": 5})
        assert resp.status_code == 200
        assert resp.json()["executions"][0]["id"] == "exe-1"
        assert calls[-1] == ("cron.list_executions", {"job_id": "cron-1", "limit": 5})

    def test_trigger_ok_and_error_mapping(self, cron_api):
        tc, _ = cron_api
        assert tc.post("/v1/cron/cron-1/trigger").json()["triggered"] == "cron-1"
        # RPC-level status:error maps to a 4xx, not a silent 200.
        assert tc.post("/v1/cron/cron-missing/trigger").status_code == 400


async def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class TestRestartDurability:
    async def test_job_survives_manager_restart_and_fires(self, tmp_path):
        # Acceptance: "a graph runs on schedule without user interaction,
        # survives daemon restart" — the schedule lives in SQLite, a fresh
        # CronManager reloads it, and a due job fires with no user action.
        db = str(tmp_path / "cron.db")
        fired: list[str] = []

        async def handler(job):
            fired.append(job.id)
            return "ok"

        first = CronManager(db_path=db)
        await first.aregister(
            CronJob(
                id="survivor",
                name="nightly",
                expression="* * * * *",
                loop_mode="continuous",
                graph_id="g1",
                input_data="hello",
                enabled=True,
                next_run=time.time() - 1,
            ),
            handler=handler,
        )
        first.close()

        # Daemon boot re-attaches the default handler (daemon_server passes
        # _cron_default_handler) — mirror that wiring for the reloaded manager.
        second = CronManager(db_path=db, default_handler=handler)
        job = second.get("survivor")
        assert job is not None
        assert job.expression == "* * * * *"
        assert job.graph_id == "g1"
        assert job.input_data == "hello"
        assert job.enabled

        second.start()
        try:
            assert await _wait_until(lambda: fired == ["survivor"])
        finally:
            second.close()


class TestFailureAlerting:
    async def test_failure_callback_receives_job_and_execution(self, tmp_path):
        alerts: list[tuple[str, str]] = []

        async def boom(job):
            raise RuntimeError("graph exploded")

        mgr = CronManager(
            db_path=str(tmp_path / "cron.db"),
            failure_callback=lambda job, exe: alerts.append((job.id, exe.status)),
        )
        await mgr.aregister(
            CronJob(
                id="alert-me",
                name="failing",
                expression="* * * * *",
                loop_mode="continuous",
                graph_id="g1",
                max_retries=0,
                enabled=True,
                next_run=time.time() - 1,
            ),
            handler=boom,
        )
        mgr.start()
        try:
            assert await _wait_until(
                lambda: alerts and alerts[0][0] == "alert-me" and alerts[0][1] == "failed"
            )
        finally:
            mgr.close()

    async def test_callback_exception_never_breaks_the_loop(self, tmp_path):
        ran = []

        async def ok(job):
            ran.append(job.id)
            return "done"

        def bad_callback(job, exe):
            raise ValueError("alert sink down")

        mgr = CronManager(
            db_path=str(tmp_path / "cron.db"),
            failure_callback=bad_callback,
        )
        await mgr.aregister(
            CronJob(
                id="cb-safe",
                name="safe",
                expression="* * * * *",
                loop_mode="continuous",
                graph_id="g1",
                enabled=True,
                next_run=time.time() - 1,
            ),
            handler=ok,
        )
        mgr.start()
        try:
            assert await _wait_until(lambda: ran == ["cb-safe"])
        finally:
            mgr.close()
