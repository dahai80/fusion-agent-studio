from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# #530: API key flow for deployed agents + conversational starter library.
# (a) starters endpoint serves the preset library with ask + tools;
# (b) an ApiKeyManager key authorizes an external Bearer call against a
#     deployed agent (REST path), and rotating it invalidates the old
#     secret — the Claude-console key flow on the shared key base.


class _StubDaemon:
    version = "0.13.3"

    def __init__(self, tmp_path):
        self.agents_dir = tmp_path / "agents"

    def _agent_dir(self, agent_id):
        d = self.agents_dir / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)


async def _rpc_stub(method, params):
    if method == "agent.list":
        return {"agents": [
            {"id": "dep1", "name": "Reconciler", "status": "published",
             "tools": ["agent.list"], "description": "d"}]}
    if method == "agent.get":
        return {"agent": {"id": params["agent_id"], "name": "Reconciler",
                          "status": "published", "tools": ["agent.list"],
                          "description": "d", "model": "base",
                          "system_prompt": "s", "metadata": {}}}
    if method == "agent.test":
        return {"status": "completed",
                "events": [{"type": "message", "content": "ok"}]}
    raise AssertionError(f"unexpected rpc {method}")


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agent_runtime.api_server as api_server
    from agent_runtime.agent_platform import publish_registry as pr_mod

    real_pr = pr_mod.PublishRegistry
    monkeypatch.setattr(
        pr_mod, "PublishRegistry",
        lambda path="": real_pr(str(tmp_path / "publish.json")))

    stub = _StubDaemon(tmp_path)
    monkeypatch.setattr(api_server, "_daemon", stub, raising=False)
    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    # isolated HOME so the ApiKeyManager store is test-local
    monkeypatch.setenv("HOME", str(tmp_path))
    with TestClient(api_server.app) as c:
        yield c


def test_starters_endpoint_serves_presets(client):
    r = client.get("/v1/agents-platform/starters")
    assert r.status_code == 200, r.text
    starters = r.json()["starters"]
    keys = [s["key"] for s in starters]
    assert keys == ["reconcile", "weekly-report", "support", "researcher"]
    for s in starters:
        assert s["ask"].strip(), f"starter {s['key']} must pre-seed an ask"
        assert isinstance(s["tools"], list)


def test_external_bearer_key_calls_deployed_agent_and_rotate_invalidates(client):
    import os

    from agent_runtime.apikey_manager import ApiKeyManager
    from agent_runtime.paths import studio_home

    mgr = ApiKeyManager(studio_home() / "apikeys")
    created = mgr.create(name="ops-key")
    secret = created["key_secret"]
    key_id = created["key_id"]

    # a static env key would also authorize; make sure the dynamic key is
    # what we are exercising by clearing any static posture
    saved = os.environ.pop("FUSION_AGENT_STUDIO_API_KEY", None)
    try:
        # no key configured -> the manager key alone flips _auth_configured
        hdr = {"Authorization": f"Bearer {secret}",
               "content-type": "application/json"}
        r = client.post("/v1/agents-platform/agents/dep1/test", headers=hdr,
                        json={"message": "hello"})
        assert r.status_code == 200, r.text
        assert r.json()["events"][0]["content"] == "ok"

        # missing/invalid key must be rejected now that keys exist
        r2 = client.post("/v1/agents-platform/agents/dep1/test",
                         headers={"Authorization": "Bearer fk-wrong",
                                  "content-type": "application/json"},
                         json={"message": "hello"})
        assert r2.status_code in (401, 403), r2.text

        # rotate -> old secret stops working, new one works
        rotated = mgr.rotate(key_id)
        new_secret = rotated["key_secret"]
        assert new_secret != secret
        r3 = client.post("/v1/agents-platform/agents/dep1/test",
                         headers={"Authorization": f"Bearer {secret}",
                                  "content-type": "application/json"},
                         json={"message": "hello"})
        assert r3.status_code in (401, 403), "old key must be invalid after rotate"
        r4 = client.post("/v1/agents-platform/agents/dep1/test",
                         headers={"Authorization": f"Bearer {new_secret}",
                                  "content-type": "application/json"},
                         json={"message": "hello"})
        assert r4.status_code == 200, r4.text
    finally:
        if saved is not None:
            os.environ["FUSION_AGENT_STUDIO_API_KEY"] = saved
