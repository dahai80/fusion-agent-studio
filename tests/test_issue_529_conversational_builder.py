from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_runtime.agent_platform import capabilities as caps_mod
from agent_runtime.agent_platform import door as door_mod
from agent_runtime.agent_platform.door import CapabilityApprovalStore
from agent_runtime.agent_platform.yaml_compiler import compile_doc

# #529: conversational builder parity — the compose endpoint accepts a
# `current_doc` refinement turn: the model receives the current draft and
# an instruction, and must answer with the FULL updated document. The
# gateway chat is stubbed at the daemon level; these tests pin the wire
# contract (prompt shape + user content carry the current draft) and the
# compile-on-both-paths behavior.


class _ChatResp:
    def __init__(self, content: str):
        self.content = content


class _StubGateway:
    def __init__(self):
        self.calls: list[dict] = []

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        user = messages[-1]["content"]
        if "Current document:" in user:
            # refinement turn: echo an updated full document
            return _ChatResp(
                "name: Reconciler\n"
                "description: reconciles ledgers nightly\n"
                "model:\n  id: base\n  effort: low\n"
                "system: |\n  You reconcile. Excel export included.\n"
                "tools:\n  - agent.list\n"
                "metadata:\n  template: custom\n"
            )
        return _ChatResp(
            "name: Reconciler\n"
            "description: reconciles ledgers\n"
            "model:\n  id: base\n  effort: low\n"
            "system: |\n  You reconcile.\n"
            "tools:\n  - agent.list\n"
            "metadata:\n  template: custom\n"
        )


class _StubDaemon:
    version = "0.13.3"

    def __init__(self, tmp_path):
        self.agents_dir = tmp_path / "agents"
        self._gateway = _StubGateway()

    def _agent_dir(self, agent_id):
        d = self.agents_dir / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d)


async def _rpc_stub(method, params):
    if method == "agent.list":
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
    monkeypatch.setattr(
        door_mod, "CapabilityApprovalStore",
        lambda db_path=None: CapabilityApprovalStore(
            str(tmp_path / "approvals.db")))
    door_mod._default_door = None

    stub = _StubDaemon(tmp_path)
    monkeypatch.setattr(api_server, "_daemon", stub, raising=False)
    monkeypatch.setattr(api_server, "_rpc", _rpc_stub)
    key = "fk-static-test-529"
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", key)
    with TestClient(api_server.app) as c:
        c.headers.update({"x-api-key": key})
        yield c


def test_compose_first_turn(client):
    r = client.post("/v1/agents-platform/compose",
                    json={"ask": "帮我做一个对账智能体"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["doc"]["name"] == "Reconciler"
    daemon = client.app  # noqa: F841 — readability only
    import agent_runtime.api_server as api_server

    gw = api_server._daemon._gateway
    assert len(gw.calls) == 1
    system = gw.calls[0]["messages"][0]["content"]
    user = gw.calls[0]["messages"][-1]["content"]
    assert "convert a one-sentence agent request" in system
    assert "Current document:" not in user
    assert user == "帮我做一个对账智能体"


def test_compose_refinement_turn_carries_current_doc(client):
    current = {
        "name": "Reconciler",
        "description": "reconciles ledgers",
        "model": {"id": "base", "effort": "low"},
        "system": "You reconcile.",
        "tools": ["agent.list"],
    }
    r = client.post("/v1/agents-platform/compose",
                    json={"ask": "加上 Excel 导出",
                          "current_doc": current})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # the model output compiled into a full updated doc
    assert "Excel export included" in body["doc"]["system"]
    assert body["doc"]["tools"] == ["agent.list"]
    import agent_runtime.api_server as api_server

    gw = api_server._daemon._gateway
    assert len(gw.calls) == 1
    system = gw.calls[0]["messages"][0]["content"]
    user = gw.calls[0]["messages"][-1]["content"]
    assert "refine an existing agent-authoring YAML" in system
    assert "Current document:" in user
    assert "You reconcile." in user
    assert "Instruction: 加上 Excel 导出" in user


def test_compose_refinement_bad_model_output_is_honest(client):
    import agent_runtime.api_server as api_server

    async def bad_chat(messages, **kw):
        return _ChatResp("this is not yaml at all {{{")

    api_server._daemon._gateway.chat = bad_chat
    r = client.post("/v1/agents-platform/compose",
                    json={"ask": "改一下", "current_doc": {
                        "name": "x", "system": "s", "tools": []}})
    body = r.json()
    assert body["ok"] is False
    assert "did not compile" in body["error"]


def test_compose_requires_ask(client):
    r = client.post("/v1/agents-platform/compose", json={})
    assert r.status_code != 200


def test_refined_doc_still_compiles_through_yaml_compiler(client):
    # the refinement output must satisfy the same authoring contract as a
    # fresh compose — no forked validation path
    doc = {
        "name": "Reconciler",
        "description": "d",
        "model": {"id": "base", "effort": "low"},
        "system": "You reconcile.",
        "tools": ["agent.list"],
    }
    out = compile_doc(doc)
    assert out["definition"]["status"] == "draft"
    assert caps_mod.get_capability("agent.list") is not None
