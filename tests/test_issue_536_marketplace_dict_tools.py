from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# #536 regression: builder drafts written through the connector path carry
# dict-shaped tool entries ({"name": ..., "type": ...}) instead of bare
# strings. The marketplace listing used to key catalog titles by that raw
# entry (TypeError: unhashable type: 'dict') and 500ed every published agent
# in the workspace. Dict entries now normalize to their name; unrecognizable
# entries are skipped for that agent only.

_DICT_AGENT = {
    "id": "conn1",
    "name": "Inventory agent",
    "status": "published",
    "description": "stock levels",
    "tools": [
        "agent.list",
        {"name": "inventory.list", "type": "http", "url": "http://inventory.local"},
        {"type": "http"},  # no name/id — skipped with a warning
        {"id": "inventory.legacy", "type": "http"},
    ],
}


async def _rpc_stub(method, params):
    if method == "agent.list":
        return {"agents": [_DICT_AGENT]}
    if method == "agent.get":
        if params.get("agent_id") == "conn1":
            return {"agent": _DICT_AGENT}
        return {"agent": None}
    if method == "analytics.agent_usage":
        return {"agents": []}
    raise AssertionError(f"unexpected rpc {method}")


@pytest.fixture
def client(monkeypatch, tmp_path):
    import agent_runtime.api_server as api_server
    from agent_runtime.agent_platform import publish_registry as pr_mod

    real_pr = pr_mod.PublishRegistry
    monkeypatch.setattr(
        pr_mod, "PublishRegistry",
        lambda path="": real_pr(str(tmp_path / "publish.json")))
    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    monkeypatch.setattr(api_server, "_auth_configured", lambda: False)
    with TestClient(api_server.app) as c:
        yield c


def test_dict_shaped_tools_do_not_break_listing(client):
    r = client.get("/v1/marketplace/agents")
    assert r.status_code == 200, r.text
    entries = r.json()["agents"]
    assert [e["agent_id"] for e in entries] == ["conn1"]
    caps = entries[0]["capabilities"]
    ids = [c["id"] for c in caps]
    # string passthrough + name normalization + id fallback; nameless skipped
    assert ids == ["agent.list", "inventory.list", "inventory.legacy"]


def test_dict_tool_definition_preserved(client):
    r = client.get("/v1/marketplace/agents/conn1")
    assert r.status_code == 200, r.text
    caps = {c["id"]: c for c in r.json()["capabilities"]}
    assert caps["inventory.list"]["definition"]["type"] == "http"
    assert caps["inventory.list"]["definition"]["url"] == "http://inventory.local"
    # string entries carry no definition key
    assert "definition" not in caps["agent.list"]
