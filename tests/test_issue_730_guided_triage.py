# Issue #730: guided triage — one LLM call sorts the pending review set into
# ordered chapters; manifest schema is validated; every failure mode falls
# back to the unsorted single-chapter manifest; feedback comments carry their
# chapter context through the #731 plan-review round-trip.
from __future__ import annotations

import json

import pytest

from agent_runtime.review_triage import ReviewTriageEngine


class _Resp:
    def __init__(self, content: str, finish_reason: str = "stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.usage = {}


class _FakeGateway:
    def __init__(self, content: str = "", fail: bool = False, finish_reason: str = "stop"):
        self._content = content
        self._fail = fail
        self._finish_reason = finish_reason
        self.calls = 0

    async def chat(self, messages=None, capability=None, **kwargs):
        self.calls += 1
        if self._fail:
            raise RuntimeError("boom")
        return _Resp(self._content, self._finish_reason)


GOOD_ITEMS = [
    {"id": "plan-1", "kind": "plan", "title": "DB migration plan", "content": "drops a table"},
    {"id": "diff-2", "kind": "diff", "title": "auth change", "content": "token refresh"},
    {"id": "test-3", "kind": "test_results", "title": "unit tests", "content": "all green"},
]

GOOD_JSON = json.dumps(
    {
        "chapters": [
            {
                "title": "Destructive change",
                "overview": "Drops a table; read first.",
                "item_ids": ["plan-1"],
                "rationale": "data loss risk",
            },
            {
                "title": "Code changes",
                "overview": "Auth diff and green tests.",
                "item_ids": ["diff-2", "test-3"],
                "rationale": "routine",
            },
        ]
    }
)


def _items_payload():
    return [
        {"id": "plan-1", "kind": "plan", "title": "DB migration plan"},
        {"id": "diff-2", "kind": "diff", "title": "auth change"},
        {"id": "test-3", "kind": "test_results", "title": "unit tests"},
    ]


async def test_single_llm_call_sorted_manifest():
    gw = _FakeGateway(GOOD_JSON)
    engine = ReviewTriageEngine(gw)
    m = await engine.triage(GOOD_ITEMS)
    assert gw.calls == 1, "chapter sort must be exactly one LLM call"
    assert m["sorted"] is True
    assert m["item_count"] == 3
    assert [c["title"] for c in m["chapters"]] == ["Destructive change", "Code changes"]
    assert m["chapters"][0]["item_ids"] == ["plan-1"]
    assert m["chapters"][0]["overview"]


async def test_every_item_covered_exactly_once():
    # Model dropped an item and duplicated another — manifest must self-heal.
    partial = json.dumps(
        {
            "chapters": [
                {"title": "A", "overview": "", "item_ids": ["plan-1", "plan-1"], "rationale": ""},
                {"title": "B", "overview": "", "item_ids": ["diff-2", "ghost-id"], "rationale": ""},
            ]
        }
    )
    m = await ReviewTriageEngine(_FakeGateway(partial)).triage(GOOD_ITEMS)
    all_ids = [iid for c in m["chapters"] for iid in c["item_ids"]]
    assert sorted(all_ids) == ["diff-2", "plan-1", "test-3"]
    assert len(all_ids) == len(set(all_ids)), "duplicates must be dropped"


async def test_unknown_ids_dropped_missing_go_to_other():
    partial = json.dumps(
        {
            "chapters": [
                {
                    "title": "Only plan",
                    "overview": "",
                    "item_ids": ["plan-1", "nope"],
                    "rationale": "",
                }
            ]
        }
    )
    m = await ReviewTriageEngine(_FakeGateway(partial)).triage(GOOD_ITEMS)
    assert m["chapters"][0]["item_ids"] == ["plan-1"]
    other = m["chapters"][-1]
    assert other["title"] == "Other"
    assert sorted(other["item_ids"]) == ["diff-2", "test-3"]


async def test_garbage_llm_output_falls_back_unsorted():
    m = await ReviewTriageEngine(_FakeGateway("I cannot do that")).triage(GOOD_ITEMS)
    assert m["sorted"] is False
    assert m["chapters"][0]["item_ids"] == ["plan-1", "diff-2", "test-3"]


async def test_gateway_error_finish_reason_falls_back():
    gw = _FakeGateway(GOOD_JSON, finish_reason="error")
    m = await ReviewTriageEngine(gw).triage(GOOD_ITEMS)
    assert m["sorted"] is False
    assert "gateway error" in m["chapters"][0]["rationale"]


async def test_gateway_exception_falls_back():
    m = await ReviewTriageEngine(_FakeGateway(fail=True)).triage(GOOD_ITEMS)
    assert m["sorted"] is False


async def test_no_gateway_falls_back_immediately():
    gw = _FakeGateway(GOOD_JSON)
    m = await ReviewTriageEngine(None).triage(GOOD_ITEMS)
    assert gw.calls == 0, "no gateway must not attempt an LLM call"
    assert m["sorted"] is False


def test_invalid_items_raise():
    engine = ReviewTriageEngine(None)
    with pytest.raises(ValueError):
        engine._validate_items([])
    with pytest.raises(ValueError):
        engine._validate_items([{"title": "no id"}])
    with pytest.raises(ValueError):
        engine._validate_items([{"id": "a"}, {"id": "a"}])


async def test_manifest_item_lookup_survives_roundtrip():
    m = await ReviewTriageEngine(_FakeGateway(GOOD_JSON)).triage(GOOD_ITEMS)
    item = m["items"]["plan-1"]
    assert item["title"] == "DB migration plan"


async def test_feedback_comment_carries_chapter_context(daemon=None):
    # #730 acceptance: human feedback rides along with the chapter it was
    # left in — comment dicts pass through the #731 review payload verbatim.
    import tempfile

    from agent_runtime.daemon_server import DaemonServer

    tmp = tempfile.mkdtemp(prefix="f730-")
    daemon = DaemonServer(socket_path=f"{tmp}/t.sock", store_path=f"{tmp}/store.db")

    async def _run(method, params=None):
        handler = daemon._get_handler(method)
        out = handler(params or {})
        if hasattr(out, "__await__"):
            return await out
        return out

    plan = (await _run("planner.create_plan", {"task": "triaged work"}))["plan"]
    review = {
        "comments": [
            {
                "chapter": "Destructive change",
                "anchor": {"kind": "step", "id": "s1"},
                "comment": "scope this down",
            }
        ],
        "verdict": "request_changes",
    }
    res = await _run("planner.approve_plan", {"plan_id": plan["id"], "review": review})
    assert res["status"] == "changes_requested"
    stored = (await _run("planner.get_plan", {"plan_id": plan["id"]}))["plan"]["metadata"]["review"]
    assert stored["comments"][0]["chapter"] == "Destructive change"
    assert stored["comments"][0]["anchor"]["id"] == "s1"


async def test_review_triage_rpc_sorted_path():
    import tempfile

    from agent_runtime.daemon_server import DaemonServer

    tmp = tempfile.mkdtemp(prefix="f730-rpc-")
    daemon = DaemonServer(socket_path=f"{tmp}/t.sock", store_path=f"{tmp}/store.db")
    daemon._gateway = _FakeGateway(GOOD_JSON)

    async def _run(method, params=None):
        handler = daemon._get_handler(method)
        out = handler(params or {})
        if hasattr(out, "__await__"):
            return await out
        return out

    res = await _run("review.triage", {"items": GOOD_ITEMS})
    m = res["manifest"]
    assert m["sorted"] is True
    assert m["chapters"][0]["item_ids"] == ["plan-1"]


async def test_review_triage_rpc_fallback_and_validation():
    import tempfile

    from agent_runtime.daemon_server import DaemonServer

    tmp = tempfile.mkdtemp(prefix="f730-rpc2-")
    daemon = DaemonServer(socket_path=f"{tmp}/t.sock", store_path=f"{tmp}/store.db")

    async def _run(method, params=None):
        handler = daemon._get_handler(method)
        out = handler(params or {})
        if hasattr(out, "__await__"):
            return await out
        return out

    res = await _run("review.triage", {"items": GOOD_ITEMS})
    assert res["manifest"]["sorted"] is False, "no LLM client on test gateway must fall back"
    err = await _run("review.triage", {"items": []})
    assert err["status"] == "error"


def test_review_triage_allowlisted():
    from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

    assert "review.triage" in WEB_RPC_ALLOWLIST
