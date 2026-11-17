# Issue #731: web review module round-trip (web-shell#163) —
# human_input.list_pending passes the bridge; planner approve/review
# round-trip persists structured feedback on the plan record so the
# revising agent reads which spans were addressed on its next turn
# (planner.get_plan returns plan.metadata verbatim).
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


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def _plan(daemon, task="ship the thing"):
    res = await _run(daemon, "planner.create_plan", {"task": task})
    assert res["plan"]["status"] == "pending_approval"
    return res["plan"]["id"]


async def test_list_pending_passes_bridge(daemon):
    res = await _run(daemon, "human_input.list_pending", {})
    assert res == {"inputs": []}


async def test_approve_with_review_persists_comments(daemon):
    plan_id = await _plan(daemon)
    review = {
        "comments": [
            {"anchor": {"kind": "step", "id": "s1", "range": [1, 3]}, "comment": "too broad"},
            {"comment": "risk note missing"},
        ],
        "verdict": "approve",
        "source": "web",
    }
    res = await _run(daemon, "planner.approve_plan", {"plan_id": plan_id, "review": review})
    assert res == {"approved": True}
    plan = (await _run(daemon, "planner.get_plan", {"plan_id": plan_id}))["plan"]
    stored = plan["metadata"]["review"]
    assert stored["verdict"] == "approve"
    assert stored["source"] == "web"
    assert len(stored["comments"]) == 2
    assert stored["comments"][0]["anchor"]["id"] == "s1"
    assert plan["status"] == "approved"


async def test_request_changes_persists_review_and_keeps_pending(daemon):
    plan_id = await _plan(daemon)
    review = {
        "comments": [{"anchor": {"kind": "line", "id": "plan.md:12"}, "comment": "step 2 unsafe"}],
        "verdict": "request_changes",
    }
    res = await _run(daemon, "planner.approve_plan", {"plan_id": plan_id, "review": review})
    assert res["approved"] is False
    assert res["review_attached"] is True
    assert res["status"] == "changes_requested"
    plan = (await _run(daemon, "planner.get_plan", {"plan_id": plan_id}))["plan"]
    assert plan["status"] == "pending_approval"
    stored = plan["metadata"]["review"]
    assert stored["verdict"] == "request_changes"
    assert stored["comments"][0]["anchor"]["id"] == "plan.md:12"


async def test_question_verdict_keeps_pending(daemon):
    plan_id = await _plan(daemon)
    res = await _run(
        daemon, "planner.approve_plan",
        {"plan_id": plan_id, "review": {"comments": [{"comment": "why this order?"}], "verdict": "question"}},
    )
    assert res["approved"] is False and res["review_attached"] is True
    plan = (await _run(daemon, "planner.get_plan", {"plan_id": plan_id}))["plan"]
    assert plan["status"] == "pending_approval"


async def test_invalid_review_rejected(daemon):
    plan_id = await _plan(daemon)
    bad = await _run(
        daemon, "planner.approve_plan",
        {"plan_id": plan_id, "review": {"comments": ["not-a-dict"], "verdict": "approve"}},
    )
    assert bad["status"] == "error"
    bad_verdict = await _run(
        daemon, "planner.approve_plan",
        {"plan_id": plan_id, "review": {"comments": [], "verdict": "maybe"}},
    )
    assert bad_verdict["status"] == "error"
    bad_shape = await _run(daemon, "planner.approve_plan", {"plan_id": plan_id, "review": [1]})
    assert bad_shape["status"] == "error"
    plan = (await _run(daemon, "planner.get_plan", {"plan_id": plan_id}))["plan"]
    assert plan["status"] == "pending_approval", "invalid reviews must not mutate the plan"


async def test_plain_approve_without_review_unchanged(daemon):
    plan_id = await _plan(daemon)
    res = await _run(daemon, "planner.approve_plan", {"plan_id": plan_id})
    assert res == {"approved": True}
    plan = (await _run(daemon, "planner.get_plan", {"plan_id": plan_id}))["plan"]
    assert plan["status"] == "approved"
    assert "review" not in plan["metadata"]


async def test_list_plans_passes_bridge(daemon):
    await _plan(daemon)
    res = await _run(daemon, "planner.list_plans", {"status": "pending_approval"})
    assert len(res["plans"]) == 1
