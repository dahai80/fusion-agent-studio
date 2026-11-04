#588: built-in multi-repo governance steward agent — seeded on daemon
# startup, visible in agent.list with zero manual setup; manifest carries
# the G1-G5 gate model, the approval/webhook/labels surface, decided_by=owner
# default and the requester != reviewer self-approval guard.

import pytest

from agent_runtime.builtin_agents import (
    GOVERNANCE_STEWARD_ID,
    seed_builtin_agents,
)
from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(
        "FUSION_AGENT_STUDIO_HOME", str(fake_home / ".fusion-agent-studio")
    )
    d = DaemonServer(socket_path=str(tmp_path / "test.sock"))
    d._load_agents_index()
    yield d


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


def test_seed_is_idempotent(daemon):
    # The daemon fixture already ran the index load, which seeds once.
    assert GOVERNANCE_STEWARD_ID in daemon._agents
    second = seed_builtin_agents(daemon)
    assert second == []


async def test_agent_visible_in_list_after_restart(daemon):
    # A fresh DaemonServer (restart) sees the seeded agent with no setup.
    d2 = DaemonServer(socket_path=str(daemon.socket_path))
    result = await _run(d2, "agent.list", {"dedupe": False})
    ids = {a["id"] for a in result["agents"]}
    assert GOVERNANCE_STEWARD_ID in ids


async def test_manifest_carries_governance_surface(daemon):
    result = await _run(daemon, "agent.get", {"agent_id": GOVERNANCE_STEWARD_ID})
    meta = result["agent"]
    assert meta["id"] == GOVERNANCE_STEWARD_ID
    from agent_runtime.agent_package import AgentPackage

    pkg = AgentPackage(daemon._agent_dir(GOVERNANCE_STEWARD_ID))
    prompt = pkg.load_soul()
    for gate in ("G1", "G2", "G3", "G4", "G5"):
        assert gate in prompt, f"gate {gate} missing from prompt"
    assert "decided_by" in prompt and "owner" in prompt
    assert "self-approval" in prompt
    assert "127.0.0.1:11442" in prompt
    caps = meta.get("capabilities", [])
    assert set(caps) >= {"governance", "approvals", "webhooks", "labels"}
    assert "http_request" in meta.get("tools", [])
    tags = meta.get("tags", [])
    assert "built-in" in tags
    assert meta.get("status") == "published"
    assert meta.get("visibility") == "public"


def test_pilot_repos_referenced(daemon):
    meta = daemon._agents[GOVERNANCE_STEWARD_ID]
    text = meta.get("system_prompt", "") + meta.get("description", "")
    for repo in ("gateway", "web-shell", "agent-studio", "artifacts-engine", "cowork"):
        assert repo in text, f"pilot repo {repo} not referenced"


async def test_governance_dry_run_create_and_decide(daemon, monkeypatch):
    # Dry-run flow: create a G4 approval task through the same gateway the
    # steward drives, decide it as owner, confirm the record is terminal.
    monkeypatch.setenv("FUSION_AGENT_STUDIO_API_KEY", "fk-gov-588")
    from agent_runtime.agent_platform.door import get_default_door

    door = get_default_door()
    rec = door.approvals.create_external(
        title="G4 in-acceptance verification (dry run)",
        requester="pipeline-bot",
        assigned_to="owner",
        params={"gate": "G4", "repo": "fusion-agent-studio"},
        agent_id=GOVERNANCE_STEWARD_ID,
    )
    assert rec["status"] == "pending"
    assert rec["assigned_to"] == "owner"
    with pytest.raises(Exception) as ei:
        door._check_decider(rec, "pipeline-bot")
    assert "cannot" in str(ei.value).lower()
    decided = door.approvals.decide(rec["id"], "approved", decided_by="owner")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "owner"
