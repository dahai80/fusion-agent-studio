from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_runtime.agent_platform import capabilities as caps_mod
from agent_runtime.agent_platform import door as door_mod
from agent_runtime.agent_platform.door import (
    CapabilityApprovalStore,
    CapabilityDoor,
    DoorError,
)
from agent_runtime.agent_platform.templates import AGENT_TEMPLATES
from agent_runtime.agent_platform.yaml_compiler import (
    YamlCompileError,
    compile_doc,
    compile_yaml,
    decompile,
)

# ── fixtures: isolate the door's stores + the global registry ──────────

@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    from agent_runtime.agent_platform import publish_registry as pr_mod

    monkeypatch.setattr(
        door_mod, "CapabilityApprovalStore",
        lambda db_path=None: CapabilityApprovalStore(
            str(tmp_path / "approvals.db")))
    real_pr = pr_mod.PublishRegistry
    monkeypatch.setattr(
        pr_mod, "PublishRegistry",
        lambda path="": real_pr(str(tmp_path / "publish.json")))
    door_mod._default_door = None
    yield tmp_path
    door_mod._default_door = None


@pytest.fixture
def clean_registry():
    import copy

    saved = copy.deepcopy(caps_mod.CAPABILITIES)
    yield
    caps_mod.CAPABILITIES[:] = copy.deepcopy(saved)


def _door() -> CapabilityDoor:
    return door_mod.get_default_door(daemon=None)


# ── yaml_compiler: the authoring contract ──────────────────────────────

def test_compile_valid_doc_maps_effort_and_tools(clean_registry):
    out = compile_doc({
        "name": "Watcher",
        "description": "watches things",
        "model": {"id": "base", "effort": "high"},
        "system": "You watch.",
        "tools": ["agent.list"],
    })
    d = out["definition"]
    assert d["name"] == "Watcher"
    assert d["model"]["temperature"] == 0.7
    assert d["model"]["name"] == "base"
    assert [t["name"] for t in d["tools"]] == ["agent.list"]
    assert d["tools"][0]["endpoint"] == "/v1/agent-platform/call"
    assert d["status"] == "draft"


def test_compile_rejects_unknown_capability(clean_registry):
    with pytest.raises(YamlCompileError) as e:
        compile_doc({"name": "x", "system": "s",
                     "tools": ["no.such.capability"]})
    assert "unknown capability" in str(e.value)


def test_compile_requires_name_and_system(clean_registry):
    with pytest.raises(YamlCompileError):
        compile_doc({"name": "", "system": ""})


def test_roundtrip_decompile_preserves_contract(clean_registry):
    compiled = compile_doc({
        "name": "Ops monitor",
        "description": "ops",
        "model": {"id": "base", "effort": "low"},
        "system": "You monitor ops.",
        "tools": ["agent.list"],
        "metadata": {"template": "ops-agent"},
    })
    text = decompile(compiled["definition"])
    again = compile_yaml(text)
    assert again["definition"]["name"] == "Ops monitor"
    assert again["definition"]["system_prompt"] == "You monitor ops."
    assert [t["name"] for t in again["definition"]["tools"]] == ["agent.list"]
    assert again["definition"]["metadata"]["template"] == "ops-agent"


def test_templates_all_compile(clean_registry):
    for t in AGENT_TEMPLATES:
        out = compile_yaml(t["yaml"])
        assert out["definition"]["name"] == t["name"]


# ── the door: policy, gating, approvals ────────────────────────────────

def test_door_unknown_capability_404(isolated_stores, clean_registry):
    with pytest.raises(DoorError) as e:
        import asyncio

        asyncio.run(_door().call({"capability_id": "nope"}))
    assert e.value.status_code == 404


def test_door_mutation_denied_for_draft(isolated_stores, clean_registry):
    import asyncio

    with pytest.raises(DoorError) as e:
        asyncio.run(_door().call({
            "capability_id": "graph.create", "version": 0,
            "agent_id": "a1", "publisher": "alice",
            "params": {"name": "g", "description": "d"}}))
    assert e.value.status_code == 403
    assert "draft" in str(e.value.detail)


def test_door_mutation_creates_approval(isolated_stores, clean_registry):
    import asyncio

    status, data = asyncio.run(_door().call({
        "capability_id": "graph.create", "version": 2,
        "agent_id": "a1", "publisher": "alice",
        "params": {"name": "g", "description": "d"}}))
    assert status == 200
    assert data["status"] == "pending_approval"
    approval_id = data["approval_id"]
    rec = _door().approvals.get(approval_id)
    assert rec["capability_id"] == "graph.create"
    assert rec["requester"] == "agent:a1"


def test_door_mutation_param_validation(isolated_stores, clean_registry):
    import asyncio

    with pytest.raises(DoorError) as e:
        asyncio.run(_door().call({
            "capability_id": "graph.create", "version": 2,
            "agent_id": "a1", "publisher": "alice",
            "params": {"bogus": 1}}))
    assert e.value.status_code == 422


def test_door_disabled_capability_409(isolated_stores, clean_registry):
    import asyncio

    caps_mod.set_status("agent.list", "disabled")
    with pytest.raises(DoorError) as e:
        asyncio.run(_door().call({"capability_id": "agent.list"}))
    assert e.value.status_code == 409
    assert "disabled" in str(e.value.detail)


def test_approve_rejects_self_approval(isolated_stores, clean_registry):
    import asyncio

    _, data = asyncio.run(_door().call({
        "capability_id": "graph.create", "version": 1,
        "agent_id": "a1", "publisher": "alice",
        "params": {"name": "g"}}))
    with pytest.raises(DoorError) as e:
        asyncio.run(_door().approve(data["approval_id"],
                                    decided_by="agent:a1"))
    assert e.value.status_code == 403


def test_reject_records_decision(isolated_stores, clean_registry):
    import asyncio

    _, data = asyncio.run(_door().call({
        "capability_id": "graph.create", "version": 1,
        "agent_id": "a1", "publisher": "alice",
        "params": {"name": "g"}}))
    rec = asyncio.run(_door().reject(data["approval_id"],
                                     decided_by="bob", reason="no"))
    assert rec["status"] == "rejected"
    assert rec["decided_by"] == "bob"


def test_disable_source_flags_capabilities(clean_registry):
    caps_mod.add_rag_capabilities("kb1", "KB one", "docs",
                                  wired_by="alice")
    affected = caps_mod.disable_source("rag", "kb1")
    assert affected == ["knowledge.kb1.search"]
    cap = caps_mod.get_capability("knowledge.kb1.search")
    assert cap["status"] == "disabled"


def test_validate_params_rejects_unknown_and_type_errors(clean_registry):
    cap = caps_mod.add_rag_capabilities("kb2", "KB", "docs",
                                        wired_by="alice")
    errs = caps_mod.validate_params(cap, {"query": 5, "bogus": 1})
    assert any("must be string" in e for e in errs)
    assert any("unknown param" in e for e in errs)
    assert caps_mod.validate_params(cap, {"query": "hi"}) == []


# ── REST surface (stubbed daemon, like test_issue_330) ─────────────────

class _StubDaemon:
    version = "0.13.0"

    def __init__(self, tmp_path):
        self.agents_dir = tmp_path / "agents"

    def _agent_dir(self, agent_id):
        d = self.agents_dir / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)


async def _rpc_stub(method, params):
    if method == "agent.list":
        return {"agents": [{
            "id": "a1", "name": "Watcher", "status": "draft",
            "version_int": 1, "model": "base",
            "tools": ["agent.list"], "description": "d"}]}
    if method == "agent.create":
        return {"agent_id": "new1", "manifest": {}}
    if method == "agent.get":
        return {"agent": {
            "id": params["agent_id"], "name": "Watcher",
            "status": "draft", "version_int": 1, "model": "base",
            "temperature": 0.2, "system_prompt": "You watch.",
            "tools": ["agent.list"], "description": "d", "metadata": {}}}
    if method == "agent.update":
        return {"updated": True, "manifest": {}}
    if method == "agent.publish":
        return {"agent_id": params["agent_id"], "status": "published",
                "version": 2}
    if method == "agent.delete":
        return {"deleted": True}
    if method == "agent.test":
        return {"status": "completed",
                "events": [{"type": "message", "content": "hello"}]}
    if method == "graph.create":
        return {"graph_id": "graph-e2e", "name": params.get("name", ""),
                "status": "created"}
    raise AssertionError(f"unexpected rpc {method}")


@pytest.fixture
def client(monkeypatch, tmp_path, isolated_stores, clean_registry):
    import agent_runtime.api_server as api_server

    stub = _StubDaemon(tmp_path)
    # the router resolves _rpc/_daemon from api_server globals at call time
    # (late-binding lambdas), so patching here reaches every route
    monkeypatch.setattr(api_server, "_daemon", stub, raising=False)
    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    # deterministic auth posture: a static key we always present
    key = "fk-static-test-437"
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", key)
    with TestClient(api_server.app) as c:
        c.headers.update({"x-api-key": key})
        yield c


def test_console_static_served(client):
    r = client.get("/console/agents.html")
    assert r.status_code == 200
    assert "Start from a sample" in r.text
    assert "/v1/agents-platform" in r.text
    r = client.get("/console/app.css")
    assert r.status_code == 200


def test_whoami_and_health(client):
    assert client.get("/v1/agents-platform/whoami").json()["role"] == "admin"
    h = client.get("/v1/agents-platform/health").json()
    assert h["ok"] is True and h["engine"] == "fusion-agent-studio"


def test_capability_catalog_counts(client):
    body = client.get("/v1/agents-platform/capability-catalog").json()
    ids = [c["id"] for c in body["capabilities"]]
    assert "agent.list" in ids
    assert body["counts"]["rest"] >= 1


def test_agents_crud_roundtrip(client, tmp_path):
    doc = {"name": "Watcher", "description": "d",
           "model": {"id": "base", "effort": "low"},
           "system": "You watch.", "tools": ["agent.list"]}
    r = client.post("/v1/agents-platform/agents",
                    json={"definition_doc": doc})
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "new1"
    # sidecar persisted — the YAML contract survives the round-trip
    sidecar = Path(_sidecar_path(client, "new1"))
    assert sidecar.is_file()
    assert "You watch." in sidecar.read_text()

    g = client.get("/v1/agents-platform/agents/new1").json()
    assert g["agent"]["name"] == "Watcher"
    assert "You watch." in g["yaml"]
    assert g["doc"]["system"] == "You watch."

    u = client.put("/v1/agents-platform/agents/new1",
                   json={"definition_doc": doc})
    assert u.json()["updated"] is True

    t = client.post("/v1/agents-platform/agents/new1/test",
                    json={"message": "hi"})
    assert t.json()["events"][0]["content"] == "hello"

    d = client.delete("/v1/agents-platform/agents/new1")
    assert d.json()["deleted"] is True


def _sidecar_path(client, agent_id):
    import agent_runtime.api_server as api_server

    return str(api_server._daemon._agent_dir(agent_id)) + "/authoring.yaml"


def test_publish_writes_registry(client, tmp_path, monkeypatch):
    from agent_runtime.agent_platform import publish_registry as pr_mod

    # isolated_stores already routes PublishRegistry to tmp_path
    real = pr_mod.PublishRegistry
    r = client.post("/v1/agents-platform/agents/a1/publish")
    assert r.status_code == 200
    assert r.json()["version"] == 2
    reg_path = tmp_path / "publish.json"
    assert reg_path.exists()
    rec = real(str(reg_path)).get("a1")
    assert rec["publisher"] and rec["version"] == 2


def test_compile_endpoint_ok_and_error(client):
    r = client.post("/v1/agents-platform/compile",
                    json={"yaml": "name: [unclosed\nsystem: hi\n"})
    assert r.json()["ok"] is False
    good = ("name: X\nsystem: hi\nmodel:\n  id: base\n  effort: low\n"
            "tools:\n  - agent.list\n")
    r2 = client.post("/v1/agents-platform/compile", json={"yaml": good})
    body = r2.json()
    assert body["ok"] is True
    assert body["doc"]["name"] == "X"


def test_templates_endpoint(client):
    body = client.get("/v1/agents-platform/templates").json()
    keys = {t["key"] for t in body["templates"]}
    assert {"research", "ops"} <= keys


def test_door_call_via_http_gates_mutation(client):
    r = client.post("/v1/agent-platform/call", json={
        "capability_id": "graph.create", "version": 1,
        "agent_id": "a1", "publisher": "alice",
        "params": {"name": "g"}})
    body = r.json()
    assert body["status"] == "pending_approval"
    approval_id = body["approval_id"]
    # requester cannot approve their own request
    r2 = client.post(
        f"/v1/agent-platform/approvals/{approval_id}/approve",
        json={"decided_by": "agent:a1"})
    assert r2.status_code == 403
    # a different human can reject
    r3 = client.post(
        f"/v1/agent-platform/approvals/{approval_id}/reject",
        json={"decided_by": "bob", "reason": "not now"})
    assert r3.json()["status"] == "rejected"


def test_agent_platform_capabilities_endpoint(client):
    body = client.get("/v1/agent-platform/capabilities").json()
    assert any(c["id"] == "agent.list" for c in body["capabilities"])


def test_approved_mutation_executes_graph_create(client):
    r = client.post("/v1/agent-platform/call", json={
        "capability_id": "graph.create", "version": 1,
        "agent_id": "a1", "publisher": "alice",
        "params": {"name": "approved-probe"}})
    approval_id = r.json()["approval_id"]
    rec = client.post(
        f"/v1/agent-platform/approvals/{approval_id}/approve",
        json={"decided_by": "bob"}).json()
    assert rec["status"] == "approved"
    # the door dispatched the approved mutation to the execution route
    assert rec["dispatch_status_code"] == 200
    assert "graph-e2e" in rec["result"]
