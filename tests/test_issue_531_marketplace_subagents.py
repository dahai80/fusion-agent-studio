from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# #531: marketplace Sub-agents API for the Cowork capability marketplace
# tab. Deployed (published) agents list with the invoke contract; drafts
# never appear; unpublish/disable delists on the next fetch (status-based,
# no second store).


_AGENTS = [
    {"id": "dep1", "name": "Reconciler", "status": "published",
     "description": "daily reconciliation", "tools": ["agent.list"]},
    {"id": "draft1", "name": "WIP", "status": "draft",
     "description": "not ready", "tools": []},
]


async def _rpc_stub(method, params):
    if method == "agent.list":
        return {"agents": _AGENTS}
    if method == "agent.get":
        for a in _AGENTS:
            if a["id"] == params["agent_id"]:
                return {"agent": a}
        return {"agent": None}
    if method == "analytics.agent_usage":
        return {"agents": [{"agent_id": "dep1", "runs": 7}]}
    raise AssertionError(f"unexpected rpc {method}")


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agent_runtime.api_server as api_server
    from agent_runtime.agent_platform import publish_registry as pr_mod

    real_pr = pr_mod.PublishRegistry
    monkeypatch.setattr(
        pr_mod, "PublishRegistry",
        lambda path="": real_pr(str(tmp_path / "publish.json")))
    real_pr(str(tmp_path / "publish.json")).set("dep1", "owner-1", "v3")

    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    # repo test convention: pin the unconfigured-auth posture (see
    # test_c9_true_streaming.py) — machines may carry a static module key
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    with TestClient(api_server.app) as c:
        yield c


def test_published_agents_listed_with_contract_drafts_hidden(client):
    r = client.get("/v1/marketplace/agents")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [a["agent_id"] for a in body["agents"]]
    assert ids == ["dep1"]
    assert body["total"] == 1

    e = body["agents"][0]
    assert e["name"] == "Reconciler"
    assert e["description"] == "daily reconciliation"
    assert e["run_count"] == 7
    assert e["eval_score"] is None
    # registry enrichment (§5.3 publish record)
    assert e["publisher"] == "owner-1"
    assert e["version"] == "v3"
    assert e["published_at"] is not None
    # capabilities carry catalog titles + mutation flag
    assert e["capabilities"][0]["id"] == "agent.list"
    assert e["capabilities"][0]["title"]
    # invoking instructions for the marketplace card
    assert e["invoke"]["test"] == "/v1/agents-platform/agents/dep1/test"
    assert "Bearer" in e["invoke"]["auth"]


def test_unpublish_delists_on_next_fetch(client):
    r = client.get("/v1/marketplace/agents")
    assert r.json()["total"] == 1

    _AGENTS[0]["status"] = "draft"  # disable/unpublish in the Dashboard
    try:
        r2 = client.get("/v1/marketplace/agents")
        assert r2.json()["total"] == 0
        assert r2.json()["agents"] == []

        # detail hides an unpublished agent behind the same 404 as missing
        r3 = client.get("/v1/marketplace/agents/dep1")
        assert r3.status_code == 404, r3.text
    finally:
        _AGENTS[0]["status"] = "published"


def test_detail_serves_usage_example_draft_404(client):
    r = client.get("/v1/marketplace/agents/dep1")
    assert r.status_code == 200, r.text
    e = r.json()
    assert "curl -X POST" in e["usage_example"]
    assert "/v1/agents-platform/agents/dep1/test" in e["usage_example"]

    r2 = client.get("/v1/marketplace/agents/draft1")
    assert r2.status_code == 404, "drafts must never be marketable"

    r3 = client.get("/v1/marketplace/agents/nope")
    assert r3.status_code == 404
