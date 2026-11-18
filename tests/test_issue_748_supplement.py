# Issue #748: teams.supplement — mid-run steering RPC over the harness #64
# steer queue. Operator/lead origins only, 3-per-run cap (daemon-owned,
# keyed on current run attempt), receipts (delivered / delivered_as_inbox),
# late notes degrade to the assignee's inbox.
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
        "name": "steer-team",
        "namespace_key": "team:steer-team",
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


async def _team_with_task(daemon, tmp_path, **kw):
    reg = await _run(daemon, "teams.register", {"definition": _definition(**kw)})
    assert reg["status"] == "ok", reg
    added = await _run(
        daemon,
        "teams.add_task",
        {"team": "steer-team", "title": "fix bug", "assignee": "worker-a"},
    )
    assert added["status"] == "ok", added
    return added["task_id"]


def _fake_active_run(team_rt, task_id):
    # Harness run_member mounts the steer queue for the duration of a run;
    # tests mount it directly to simulate a running member.
    team_rt._supplement_queues[task_id] = asyncio.Queue()


async def test_supplement_delivered_to_running_run(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["steer-team"]
    _fake_active_run(team_rt, task_id)
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "tests only, skip migration"},
    )
    assert res["status"] == "ok"
    assert res["receipt"] == "delivered"
    assert res["supplement_id"] == "sp_1"
    assert res["steers_used"] == 1
    assert res["steer_cap"] == 3
    note = team_rt._supplement_queues[task_id].get_nowait()
    assert note == "[steer] operator note: tests only, skip migration"


async def test_supplement_lead_origin_prefix(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["steer-team"]
    _fake_active_run(team_rt, task_id)
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "wrap up", "origin": "lead"},
    )
    assert res["status"] == "ok"
    note = team_rt._supplement_queues[task_id].get_nowait()
    assert note.startswith("[steer] lead note:")


async def test_supplement_cap_reached(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["steer-team"]
    _fake_active_run(team_rt, task_id)
    for i in range(3):
        res = await _run(
            daemon,
            "teams.supplement",
            {"team": "steer-team", "task_id": task_id, "content": f"note {i}"},
        )
        assert res["status"] == "ok", res
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "one too many"},
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "supplement_cap_reached"
    assert res["steer_cap"] == 3 if "steer_cap" in res else True


async def test_supplement_late_degrades_to_inbox(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "ci went green, retry"},
    )
    assert res["status"] == "ok"
    assert res["receipt"] == "delivered_as_inbox"
    inbox = await _run(daemon, "teams.inbox", {"team": "steer-team", "member": "worker-a"})
    assert inbox["status"] == "ok"
    bodies = [m.get("body", "") for m in inbox.get("messages", [])]
    assert any("[steer-late] operator note: ci went green, retry" in b for b in bodies)


async def test_supplement_late_without_assignee_rejected(daemon, tmp_path):
    reg = await _run(daemon, "teams.register", {"definition": _definition()})
    assert reg["status"] == "ok"
    added = await _run(daemon, "teams.add_task", {"team": "steer-team", "title": "no owner"})
    task_id = added["task_id"]
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "hello"},
    )
    assert res["status"] == "error"
    assert "no assignee" in res["message"]


async def test_supplement_content_required(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "   "},
    )
    assert res["status"] == "error"


async def test_supplement_origin_validated(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "x", "origin": "worker-a"},
    )
    assert res["status"] == "error"
    assert "origin" in res["message"]


async def test_supplement_unknown_task(daemon, tmp_path):
    await _run(daemon, "teams.register", {"definition": _definition()})
    res = await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": "t-ghost", "content": "x"},
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "task_not_found"


async def test_supplement_receipt_persisted_in_task_metadata(daemon, tmp_path):
    task_id = await _team_with_task(daemon, tmp_path)
    team_rt = daemon._get_runtime().teams_registry["steer-team"]
    _fake_active_run(team_rt, task_id)
    await _run(
        daemon,
        "teams.supplement",
        {"team": "steer-team", "task_id": task_id, "content": "focus on tests"},
    )
    task = team_rt.task(task_id)
    receipts = task.metadata.get("supplements")
    assert receipts and receipts[0]["supplement_id"] == "sp_1"
    assert receipts[0]["status"] == "delivered"
