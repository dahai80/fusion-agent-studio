# Issue #750: teams.runs_list / run_messages / run_usage — read-only replay
# surface over the per-run JSONL streams fusion-harness #63 writes at
# <state_dir>/runs/<task_id>/<attempt>.jsonl. Usage comes straight from the
# run_finished event payload (funnel ledger snapshot, tolerance 0).
from __future__ import annotations

import os

import pytest

from agent_runtime.daemon_server import DaemonServer


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition(**kw):
    d = {
        "name": "runs-team",
        "namespace_key": "team:runs-team",
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
        ],
    }
    d.update(kw)
    return d


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


async def _team_with_task(daemon, tmp_path):
    reg = await _run(daemon, "teams.register", {"definition": _definition()})
    assert reg["status"] == "ok", reg
    added = await _run(daemon, "teams.add_task", {"team": "runs-team", "title": "fix bug"})
    assert added["status"] == "ok", added
    task_id = added["task_id"]
    team_rt = daemon._get_runtime().teams_registry["runs-team"]
    runs_dir = os.path.join(team_rt.state_dir, "runs", task_id)
    os.makedirs(runs_dir, exist_ok=True)
    return task_id, runs_dir


def _write_run(runs_dir, attempt, events, member="worker-a"):
    from fusion_harness.session_log import SessionEventLog

    log = SessionEventLog(session_id=str(attempt), log_dir=runs_dir)
    base = {"task_id": "t", "member": member, "team": "runs-team"}
    for ev_type, payload in events:
        log.append(ev_type, {**base, **payload})
    log.close()


async def test_runs_list_reports_attempts(daemon, tmp_path):
    task_id, runs_dir = await _team_with_task(daemon, tmp_path)
    _write_run(
        runs_dir,
        1,
        [
            ("prompt", {"text": "reproduce failing test first"}),
            ("tool_call", {"tool": "bash", "input": "pytest -x"}),
            (
                "run_finished",
                {
                    "status": "ok",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            ),
        ],
    )
    res = await _run(daemon, "teams.runs_list", {"team": "runs-team", "task_id": task_id})
    assert res["status"] == "ok"
    assert len(res["runs"]) == 1
    run = res["runs"][0]
    assert run["attempt"] == 1
    assert run["member"] == "worker-a"
    assert run["events"] == 3
    assert run["status"] == "ok"
    assert run["usage"]["total_tokens"] == 120


async def test_runs_list_empty_when_no_runs(daemon, tmp_path):
    task_id, _ = await _team_with_task(daemon, tmp_path)
    res = await _run(daemon, "teams.runs_list", {"team": "runs-team", "task_id": task_id})
    assert res["status"] == "ok"
    assert res["runs"] == []


async def test_run_messages_replays_full_run(daemon, tmp_path):
    task_id, runs_dir = await _team_with_task(daemon, tmp_path)
    _write_run(
        runs_dir,
        2,
        [
            ("prompt", {"text": "reproduce failing test first"}),
            ("tool_error", {"error": "AssertionError (3.1s)"}),
            ("run_finished", {"status": "failed", "usage": {"total_tokens": 55}}),
        ],
    )
    res = await _run(
        daemon, "teams.run_messages", {"team": "runs-team", "task_id": task_id, "attempt": 2}
    )
    assert res["status"] == "ok"
    assert res["attempt"] == 2
    types = [m["type"] for m in res["messages"]]
    assert types == ["prompt", "tool_error", "run_finished"]
    assert res["messages"][1]["payload"]["error"] == "AssertionError (3.1s)"
    assert res["messages"][0]["seq"] < res["messages"][2]["seq"]


async def test_run_messages_missing_attempt(daemon, tmp_path):
    task_id, _ = await _team_with_task(daemon, tmp_path)
    res = await _run(
        daemon, "teams.run_messages", {"team": "runs-team", "task_id": task_id, "attempt": 9}
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "task_not_found"
    assert "no run attempt 9" in res["message"]


async def test_run_messages_attempt_required(daemon, tmp_path):
    task_id, _ = await _team_with_task(daemon, tmp_path)
    res = await _run(daemon, "teams.run_messages", {"team": "runs-team", "task_id": task_id})
    assert res["status"] == "error"


async def test_run_usage_rows_match_run_finished(daemon, tmp_path):
    task_id, runs_dir = await _team_with_task(daemon, tmp_path)
    _write_run(
        runs_dir,
        1,
        [
            (
                "run_finished",
                {
                    "status": "ok",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "cache_hit_tokens": 0,
                        "total_tokens": 15,
                    },
                },
            )
        ],
    )
    _write_run(
        runs_dir,
        2,
        [
            (
                "run_finished",
                {
                    "status": "ok",
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 9,
                        "cache_hit_tokens": 12,
                        "total_tokens": 39,
                    },
                },
            )
        ],
        member="worker-b",
    )
    res = await _run(daemon, "teams.run_usage", {"team": "runs-team", "task_id": task_id})
    assert res["status"] == "ok"
    assert [r["attempt"] for r in res["usage"]] == [1, 2]
    assert res["usage"][1]["usage"]["cache_hit_tokens"] == 12
    assert res["usage"][0]["usage"]["total_tokens"] == 15


async def test_runs_rpcs_unknown_task(daemon, tmp_path):
    await _run(daemon, "teams.register", {"definition": _definition()})
    for method in ("teams.runs_list", "teams.run_messages", "teams.run_usage"):
        params = {"team": "runs-team", "task_id": "t-ghost"}
        if method == "teams.run_messages":
            params["attempt"] = 1
        res = await _run(daemon, method, params)
        assert res["status"] == "error", method
        assert res["reason_code"] == "task_not_found", method


async def test_runs_rpcs_unknown_team(daemon):
    res = await _run(daemon, "teams.runs_list", {"team": "ghost", "task_id": "t-1"})
    assert res["status"] == "error"
    assert res["reason_code"] == "team_not_registered"


async def test_run_messages_returns_stored_payloads_verbatim(daemon, tmp_path):
    # Redaction is a WRITE-side harness behavior (team_runtime._sink, #63);
    # the read surface replays the stored JSONL verbatim with no transform.
    task_id, runs_dir = await _team_with_task(daemon, tmp_path)
    _write_run(runs_dir, 1, [("tool_call", {"input": "pytest -x tests/test_auth.py"})])
    res = await _run(
        daemon, "teams.run_messages", {"team": "runs-team", "task_id": task_id, "attempt": 1}
    )
    assert res["status"] == "ok"
    assert res["messages"][0]["payload"]["input"] == "pytest -x tests/test_auth.py"
