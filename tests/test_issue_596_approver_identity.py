#596: approver identity is a selection, not free text — the approvers
# endpoint backs the GUI select; default ["owner"], FUSION_APPROVERS
# (comma-separated) overrides verbatim.

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-596")
    from fastapi.testclient import TestClient

    from agent_runtime import api_server

    with TestClient(api_server.app, raise_server_exceptions=False) as c:
        yield c


def test_approvers_default_is_owner(client):
    r = client.get("/v1/agent-platform/approvers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approvers"] == ["owner"]
    assert body["source"] == "default"


def test_approvers_env_override_verbatim(client, monkeypatch):
    monkeypatch.setenv("FUSION_APPROVERS", "owner, owner-a, reviewer-b")
    r = client.get("/v1/agent-platform/approvers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approvers"] == ["owner", "owner-a", "reviewer-b"]
    assert body["source"] == "env"


def test_approvers_env_strips_and_drops_empty(client, monkeypatch):
    monkeypatch.setenv("FUSION_APPROVERS", " owner , , admin ")
    r = client.get("/v1/agent-platform/approvers")
    assert r.json()["approvers"] == ["owner", "admin"]


def test_approve_still_stores_decided_by_verbatim(client, monkeypatch):
    # The ledger contract: decided_by is stored exactly as selected.
    monkeypatch.delenv("FUSION_APPROVERS", raising=False)
    from agent_runtime.agent_platform.door import get_default_door

    door = get_default_door()
    rec = door.approvals.create_external(
        title="G1 gate (596)",
        requester="pipeline-bot",
        assigned_to="owner",
        params={},
    )
    decided = door.approvals.decide(rec["id"], "approved", decided_by="owner")
    assert decided["decided_by"] == "owner"
