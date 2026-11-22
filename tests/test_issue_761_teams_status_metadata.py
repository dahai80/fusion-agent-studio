# Issue #761: teams.status exposes per-task console metadata — armed wakeup
# rules (fired ones excluded, mirroring kernel _armed_wakeups) plus the
# steer counter (steers_used / steer_cap) derived from durable supplement
# receipts for the current run attempt, so the console stays a pure observer.
from __future__ import annotations

import asyncio

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
        "name": "meta-team",
        "namespace_key": "team:meta-team",
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
    added = await _run(
        daemon,
        "teams.add_task",
        {"team": "meta-team", "title": "watch CI", "assignee": "worker-a"},
    )
    assert added["status"] == "ok", added
    return added["task_id"]


def _fake_active_run(team_rt, task_id):
    team_rt._supplement_queues[task_id] = asyncio.Queue()


async def _status_task(daemon, task_id):
    res = await _run(daemon, "teams.status", {"team": "meta-team"})
    assert res["status"] == "ok", res
    return res["tasks"][task_id]


async def test_status_lists_armed_wakeups(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {
            "team": "meta-team",
            "task_id": task_id,
            "kind": "at",
            "at": 1763462400,
            "on_timeout": "wake",
        },
    )
    assert res["status"] == "ok", res
    task = await _status_task(daemon, task_id)
    assert task["wakeups"] == [
        {"wake_id": "wk_1", "kind": "at", "at": 1763462400.0, "on_timeout": "wake"}
    ]


async def test_status_excludes_fired_wakeups(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "meta-team", "task_id": task_id, "kind": "at", "at": 1},
    )
    team_rt = daemon._get_runtime().teams_registry["meta-team"]
    team_rt.transition_task(task_id, "blocked", blocked_by=["ci"])
    assert daemon._evaluate_team_wakeups() == 1
    task = await _status_task(daemon, task_id)
    assert task["wakeups"] == []


async def test_status_wakeup_fields_omitted_when_absent(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "meta-team", "task_id": task_id, "kind": "every", "every": 300},
    )
    task = await _status_task(daemon, task_id)
    assert task["wakeups"] == [
        {"wake_id": "wk_1", "kind": "every", "every": 300.0, "on_timeout": "wake"}
    ]


async def test_status_zero_wakeups_and_steers_by_default(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    task = await _status_task(daemon, task_id)
    assert task["wakeups"] == []
    assert task["steers_used"] == 0
    assert task["steer_cap"] == 3


async def test_status_steer_counter_tracks_supplements(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["meta-team"]
    _fake_active_run(team_rt, task_id)
    for i in range(2):
        res = await _run(
            daemon,
            "teams.supplement",
            {"team": "meta-team", "task_id": task_id, "content": f"note {i}"},
        )
        assert res["status"] == "ok", res
    task = await _status_task(daemon, task_id)
    assert task["steers_used"] == 2
    assert task["steer_cap"] == 3


async def test_status_steer_counter_scoped_to_current_attempt(daemon, tmp_path):
    # Receipts recorded under a previous run attempt must not leak into the
    # current attempt's counter.
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["meta-team"]
    task = team_rt.task(task_id)
    task.metadata.setdefault("supplements", []).append(
        {
            "supplement_id": "sp_1",
            "origin": "operator",
            "content": "old",
            "status": "delivered",
            "attempt": 1,
        }
    )
    import os

    teams_disp = next(d for d in daemon._sub_dispatchers if hasattr(d, "_runs_dir"))
    runs_dir = os.path.join(teams_disp._runs_dir(team_rt), task_id)
    os.makedirs(runs_dir, exist_ok=True)
    with open(os.path.join(runs_dir, "2.jsonl"), "w") as f:
        f.write("{}\n")
    view = await _status_task(daemon, task_id)
    assert view["steers_used"] == 0
