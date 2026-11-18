# Issue #749: teams.wakeup_create RPC + daemon-side wakeup evaluation loop.
# Harness #65 stores rules in task.metadata["wakeups"] (kind at|every,
# on_timeout wake|end, 8-per-task cap); the daemon worker fires due rules —
# blocked -> queued for wake, cancelled with reason wakeup_timeout for end.
from __future__ import annotations

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
        "name": "wake-team",
        "namespace_key": "team:wake-team",
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


async def _team_with_blocked_task(daemon, tmp_path):
    reg = await _run(daemon, "teams.register", {"definition": _definition()})
    assert reg["status"] == "ok", reg
    added = await _run(daemon, "teams.add_task", {"team": "wake-team", "title": "await CI"})
    assert added["status"] == "ok", added
    task_id = added["task_id"]
    tr = await _run(
        daemon,
        "teams.transition",
        {"team": "wake-team", "task_id": task_id, "to": "blocked", "blocked_by": ["external-ci"]},
    )
    assert tr["status"] == "ok", tr
    team_rt = daemon._get_runtime().teams_registry["wake-team"]
    return task_id, team_rt


async def test_wakeup_create_at(daemon, tmp_path):
    task_id, team_rt = await _team_with_blocked_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 9999999999},
    )
    assert res["status"] == "ok"
    assert res["wakeup"]["wake_id"] == "wk_1"
    assert res["wakeup"]["kind"] == "at"
    rules = team_rt.task(task_id).metadata.get("wakeups")
    assert rules and rules[0]["wake_id"] == "wk_1"


async def test_wakeup_create_rule_dict_form(daemon, tmp_path):
    task_id, _ = await _team_with_blocked_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "rule": {"kind": "every", "every": 300}},
    )
    assert res["status"] == "ok"
    assert res["wakeup"]["kind"] == "every"


async def test_wakeup_create_rejects_bad_kind(daemon, tmp_path):
    task_id, _ = await _team_with_blocked_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "hourly", "every": 60},
    )
    assert res["status"] == "error"
    assert "kind" in res["message"]


async def test_wakeup_create_rejects_on_event(daemon, tmp_path):
    task_id, _ = await _team_with_blocked_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 123, "on_event": "ci_green"},
    )
    assert res["status"] == "error"
    assert "phase 2" in res["message"]


async def test_wakeup_cap_eight_per_task(daemon, tmp_path):
    task_id, _ = await _team_with_blocked_task(daemon, tmp_path)
    for i in range(8):
        res = await _run(
            daemon,
            "teams.wakeup_create",
            {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 9999999999 + i},
        )
        assert res["status"] == "ok", (i, res)
    res = await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 9999999999 + 99},
    )
    assert res["status"] == "error"
    assert "wakeup cap" in res["message"]


async def test_wakeup_eval_wake_returns_blocked_to_queued(daemon, tmp_path):
    task_id, team_rt = await _team_with_blocked_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 1},
    )
    fired = daemon._evaluate_team_wakeups()
    assert fired == 1
    task = team_rt.task(task_id)
    assert task.status == "queued"
    rule = task.metadata["wakeups"][0]
    assert rule["fired"] is True


async def test_wakeup_eval_end_cancels_with_wakeup_timeout(daemon, tmp_path):
    task_id, team_rt = await _team_with_blocked_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 1, "on_timeout": "end"},
    )
    fired = daemon._evaluate_team_wakeups()
    assert fired == 1
    task = team_rt.task(task_id)
    assert task.status == "cancelled"
    assert task.metadata["cancel_reason"] == "wakeup_timeout"
    assert task.metadata["cancelled_by_wakeup"] == "wk_1"


async def test_wakeup_eval_every_stays_armed(daemon, tmp_path):
    task_id, team_rt = await _team_with_blocked_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "every", "every": 60},
    )
    # First fire is due after one interval; rewind the anchor to simulate it.
    task = team_rt.task(task_id)
    task.metadata["wakeups"][0]["created"] = 0
    assert daemon._evaluate_team_wakeups() == 1
    task = team_rt.task(task_id)
    assert task.status == "queued"
    rule = task.metadata["wakeups"][0]
    assert rule["fired"] is False
    assert rule["last_fired"] is not None
    # Second round before the next interval: no double-fire.
    assert daemon._evaluate_team_wakeups() == 0


async def test_wakeup_eval_skips_future_rules_and_other_states(daemon, tmp_path):
    task_id, _ = await _team_with_blocked_task(daemon, tmp_path)
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 9999999999},
    )
    assert daemon._evaluate_team_wakeups() == 0
    # Non-blocked tasks are never touched by wake action.
    team_rt = daemon._get_runtime().teams_registry["wake-team"]
    team_rt.transition_task(task_id, "in_progress")
    await _run(
        daemon,
        "teams.wakeup_create",
        {"team": "wake-team", "task_id": task_id, "kind": "at", "at": 1},
    )
    fired = daemon._evaluate_team_wakeups()
    assert fired == 1
    assert team_rt.task(task_id).status == "in_progress"


async def test_wakeup_timeout_code_in_enum():
    from agent_runtime.reason_codes import HINTS, REASON_CODES

    assert "wakeup_timeout" in REASON_CODES
    assert "supplement_cap_reached" in REASON_CODES
    assert HINTS["wakeup_timeout"]
