# 598: product-delivery pipeline agent seats (BA / Architect / Env-Mock /
# Security-Reviewer / Privacy-Reviewer) — seeded as published public
# presets next to the governance steward; reviewer prompts carry the fixed
# machine-consumable findings schema {severity, file, line, desc} +
# verdict pass|blocking; doc seats carry their artifact contracts.

import json

import pytest

from agent_runtime.builtin_agents import (
    DELIVERY_SEAT_IDS,
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


def _pkg_prompt(daemon, agent_id):
    from agent_runtime.agent_package import AgentPackage

    return AgentPackage(daemon._agent_dir(agent_id)).load_soul()


def test_all_five_seats_seeded(daemon):
    for seat_id in DELIVERY_SEAT_IDS:
        assert seat_id in daemon._agents, f"seat {seat_id} not seeded"
    # idempotent
    assert seed_builtin_agents(daemon) == []


async def test_seats_visible_after_restart(daemon):
    d2 = DaemonServer(socket_path=str(daemon.socket_path))
    d2._load_agents_index()
    ids = {a["id"] for a in d2._agents.values()}
    for seat_id in DELIVERY_SEAT_IDS:
        assert seat_id in ids


def test_seats_published_public_builtin(daemon):
    for seat_id in DELIVERY_SEAT_IDS:
        meta = daemon._agents[seat_id]
        assert meta.get("status") == "published", seat_id
        assert meta.get("visibility") == "public", seat_id
        assert "built-in" in meta.get("tags", []), seat_id
        assert "delivery" in meta.get("tags", []), seat_id
        assert meta.get("tools"), f"{seat_id} has no tools"


def test_reviewer_findings_schema_contract(daemon):
    # Both reviewers share ONE machine-consumable schema: findings entries
    # with severity/file/line/desc, verdict pass|blocking, blocking iff
    # P0/P1 present.
    for seat_id in (
        "delivery-security-reviewer",
        "delivery-privacy-reviewer",
    ):
        prompt = _pkg_prompt(daemon, seat_id)
        for key in ("verdict", "findings", "severity", "file", "line", "desc"):
            assert key in prompt, f"{seat_id} schema key {key} missing"
        assert '"pass" | "blocking"' in prompt, seat_id
        for sev in ("P0", "P1", "P2", "P3"):
            assert sev in prompt, f"{seat_id} severity {sev} missing"
        assert "P0 or P1" in prompt, seat_id


def test_reviewer_checklists(daemon):
    sec = _pkg_prompt(daemon, "delivery-security-reviewer")
    for kw in ("injection", "authentication", "secrets", "dependency", "high-risk"):
        assert kw in sec, f"security checklist term {kw} missing"
    priv = _pkg_prompt(daemon, "delivery-privacy-reviewer")
    for kw in ("PII collection", "storage", "transit", "logs", "third-party"):
        assert kw in priv, f"privacy checklist term {kw} missing"
    # seats differ — security prompt never talks PII exfiltration checklist
    assert "PII collection" not in sec


def test_doc_seat_contracts(daemon):
    ba = _pkg_prompt(daemon, "delivery-ba")
    for kw in ("background", "goals", "user stories", "feature list", "acceptance criteria", "artifact_create"):
        assert kw in ba, f"BA contract term {kw} missing"
    arch = _pkg_prompt(daemon, "delivery-architect")
    for kw in ("architecture", "plan", "task breakdown", "artifact_create"):
        assert kw in arch, f"Architect contract term {kw} missing"
    mock = _pkg_prompt(daemon, "delivery-env-mock")
    for kw in ("stub", "fixture", "compose", "seed", "artifact_create"):
        assert kw in mock, f"Env/Mock contract term {kw} missing"


def test_rest_index_loader_seeds_seats(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".fusion-agent-studio").mkdir(parents=True)
    monkeypatch.setenv(
        "FUSION_AGENT_STUDIO_HOME", str(fake_home / ".fusion-agent-studio")
    )
    from agent_runtime import api_server

    index = api_server._load_agents_index()
    for seat_id in DELIVERY_SEAT_IDS:
        assert seat_id in index, seat_id
    idx = fake_home / ".fusion-agent-studio" / "agents" / "index.json"
    persisted = json.loads(idx.read_text())
    for seat_id in DELIVERY_SEAT_IDS:
        assert seat_id in persisted, seat_id
