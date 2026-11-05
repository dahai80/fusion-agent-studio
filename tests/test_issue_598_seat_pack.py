# 598 rework acceptance: seat definitions are config, not code.
# - shipped defaults load from agent_runtime/seats/delivery_seats.yaml
# - user override dir (~/.fusion-agent-studio/seats/) wins per seat id
# - invalid override packs fail visibly (logged error + skipped), never
#   silently partial; shipped pack corruption raises
# - seeded manifests match pack contents
# - Doc / DevRel seats exist in the pack with the owner-ruling contracts
# - the Python seat constants are gone (loader-only code)

import logging

import pytest
import yaml

from agent_runtime import builtin_agents, seat_pack
from agent_runtime.builtin_agents import seed_builtin_agents
from agent_runtime.daemon_server import DaemonServer
from agent_runtime.seat_pack import load_seat_specs


@pytest.fixture
def pack_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv(
        "FUSION_AGENT_STUDIO_HOME", str(fake_home / ".fusion-agent-studio")
    )
    return fake_home / ".fusion-agent-studio"


def test_shipped_defaults_load():
    specs = load_seat_specs()
    assert set(specs) >= {
        "delivery-ba",
        "delivery-architect",
        "delivery-env-mock",
        "delivery-security-reviewer",
        "delivery-privacy-reviewer",
        "delivery-doc",
        "delivery-devrel",
    }
    for seat_id, spec in specs.items():
        assert spec["id"] == seat_id
        assert spec.get("system_prompt"), seat_id
        assert spec.get("tools"), seat_id


def test_reviewer_schema_checklist_are_pack_fields():
    specs = load_seat_specs()
    for seat_id in ("delivery-security-reviewer", "delivery-privacy-reviewer"):
        spec = specs[seat_id]
        schema = spec["findings_schema"]
        assert schema["verdict"] == ["pass", "blocking"]
        assert schema["finding_fields"] == ["severity", "file", "line", "desc"]
        assert schema["blocking_severities"] == ["P0", "P1"]
        assert spec["checklist"], seat_id
        # prompt is rendered from the data fields, not a code constant
        assert spec["checklist"][0] in spec["system_prompt"]


def test_doc_and_devrel_contracts_in_pack():
    specs = load_seat_specs()
    doc = specs["delivery-doc"]
    assert doc["triggers"] == [
        "n4_design_deliverables",
        "n7_pr_merge",
        "n10_release",
        "docs_drift_sweep_periodic",
    ]
    assert doc["doc_contract"]["mandatory_pack"] == "after_n8_before_n9"
    assert doc["doc_contract"]["rejection_loop"] == "doc_only"
    assert len(doc["doc_contract"]["gate_c_preconditions"]) == 4
    devrel = specs["delivery-devrel"]
    assert devrel["trigger"] == "after_gate_c"
    assert devrel["source"] == "doc_pack"
    assert devrel["publish_gate"]["auto_publish"] is False
    assert "no automatic posting" in devrel["system_prompt"].lower()
    assert devrel["guardrails"]["redaction_checklist"] == "security"


def test_user_override_dir_wins(pack_home):
    override_dir = pack_home / "seats"
    override_dir.mkdir(parents=True)
    (override_dir / "custom.yaml").write_text(
        yaml.safe_dump(
            {
                "seats": {
                    "delivery-ba": {
                        "name": "Custom BA",
                        "system_prompt": "custom ba prompt",
                        "tools": ["file_read"],
                        "description": "user override",
                    },
                    "delivery-growth": {
                        "name": "Growth Agent",
                        "system_prompt": "growth prompt",
                        "tools": ["http_request"],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    specs = load_seat_specs()
    assert specs["delivery-ba"]["name"] == "Custom BA"
    assert specs["delivery-ba"]["system_prompt"] == "custom ba prompt"
    assert "delivery-growth" in specs
    # untouched seats keep shipped definitions
    assert specs["delivery-doc"]["name"] == "Delivery Doc Agent"


def test_invalid_override_fails_visibly(pack_home, caplog):
    override_dir = pack_home / "seats"
    override_dir.mkdir(parents=True)
    (override_dir / "broken.yaml").write_text(
        "seats: [not, a, mapping]\n", encoding="utf-8"
    )
    (override_dir / "good.yaml").write_text(
        yaml.safe_dump(
            {
                "seats": {
                    "delivery-ux": {
                        "name": "UX Agent",
                        "system_prompt": "ux prompt",
                        "tools": ["file_read"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR, logger="agent_runtime.seat_pack"):
        specs = load_seat_specs()
    # broken file: loud error, skipped
    assert any("broken.yaml" in r.message for r in caplog.records)
    assert "delivery-ux" in specs
    # shipped seats unaffected — never silently partial
    assert "delivery-ba" in specs


def test_invalid_shipped_pack_raises(pack_home, monkeypatch):
    monkeypatch.setattr(seat_pack, "SHIPPED_PACK", pack_home / "nope.yaml")
    with pytest.raises(Exception):
        load_seat_specs()


def test_python_seat_constants_deleted():
    assert not hasattr(builtin_agents, "_delivery_seat_agents")
    assert not hasattr(builtin_agents, "DELIVERY_SEAT_IDS")
    assert not hasattr(builtin_agents, "REVIEWER_FINDINGS_SCHEMA")
    # steward seed untouched (#588 out of scope per ruling)
    assert len(builtin_agents.BUILTIN_AGENTS) == 1
    assert builtin_agents.BUILTIN_AGENTS[0]["id"] == "governance-steward"


@pytest.fixture
def daemon(pack_home):
    d = DaemonServer(socket_path=str(pack_home.parent / "test.sock"))
    d._load_agents_index()
    yield d


def test_seeded_manifests_match_pack(daemon):
    specs = load_seat_specs()
    # index load seeded all 7 seats already; re-seed is idempotent
    assert seed_builtin_agents(daemon) == []
    for seat_id in specs:
        assert seat_id in daemon._agents, seat_id
    for seat_id, spec in specs.items():
        meta = daemon._agents[seat_id]
        assert meta["name"] == spec["name"], seat_id
        assert meta["tools"] == spec["tools"], seat_id
        assert meta["capabilities"] == spec["capabilities"], seat_id
        assert meta["description"] == spec["description"], seat_id
        assert meta["status"] == spec["status"], seat_id
        from agent_runtime.agent_package import AgentPackage

        prompt = AgentPackage(daemon._agent_dir(seat_id)).load_soul()
        assert prompt == spec["system_prompt"], seat_id


def test_override_changes_seeded_manifest(pack_home, monkeypatch):
    override_dir = pack_home / "seats"
    override_dir.mkdir(parents=True)
    (override_dir / "seats.yaml").write_text(
        yaml.safe_dump(
            {
                "seats": {
                    "delivery-doc": {
                        "name": "Docs Bot",
                        "system_prompt": "overridden doc prompt",
                        "tools": ["file_write"],
                        "capabilities": ["docs"],
                        "description": "override",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    d = DaemonServer(socket_path=str(pack_home.parent / "t2.sock"))
    d._load_agents_index()
    meta = d._agents["delivery-doc"]
    assert meta["name"] == "Docs Bot"
    from agent_runtime.agent_package import AgentPackage

    prompt = AgentPackage(d._agent_dir("delivery-doc")).load_soul()
    assert prompt == "overridden doc prompt"
