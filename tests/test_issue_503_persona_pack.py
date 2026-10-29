"""Tests for issue #503 — persona-pack marketplace distribution.

Acceptance:
- persona-pack publish/install round-trip with signature verification,
  tenant-scoped install, audit entries — each with a test
- installing a pack whose rules remove an existing deny is rejected
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.agent_marketplace import AgentMarketplace
from agent_runtime.persona_pack import (
    PersonaPackManager,
    build_manifest,
    denies_removed,
    verify_manifest,
)


def _make_pack(base: Path, layer: str = "tenant", name: str = "acme",
               rules: dict[str, str] | None = None) -> Path:
    d = base / "pack"
    (d / "persona").mkdir(parents=True)
    (d / "agent.md").write_text("# Acme persona\n\nBe terse.\n", encoding="utf-8")
    (d / "persona" / "tone.md").write_text("## Tone\n\nDry humor.\n", encoding="utf-8")
    (d / "rules").mkdir()
    for fname, rule in (
        rules if rules is not None else {"deny-shell.md": "deny: bash.*"}
    ).items():
        (d / "rules" / fname).write_text(f"{rule}\n", encoding="utf-8")
    build_manifest(d, layer=layer, name=name)
    return d


@pytest.fixture()
def env(tmp_path):
    mp_store = tmp_path / "mp_store"
    mp = AgentMarketplace(store_dir=mp_store)
    persona_root = tmp_path / "persona_root"
    persona_root.mkdir()
    ppm = PersonaPackManager(
        store_dir=tmp_path / "pack_store", marketplace=mp, persona_root=persona_root
    )
    return ppm, mp, persona_root


class TestManifest:
    def test_build_and_verify_roundtrip(self, tmp_path):
        d = _make_pack(tmp_path)
        manifest = verify_manifest(d)
        assert manifest["layer"] == "tenant"
        assert manifest["name"] == "acme"
        paths = {f["path"] for f in manifest["files"]}
        assert paths == {"agent.md", "persona/tone.md", "rules/deny-shell.md"}

    def test_tampered_file_rejected(self, tmp_path):
        d = _make_pack(tmp_path)
        (d / "persona" / "tone.md").write_text("## Tone\n\nChanged.\n", encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            verify_manifest(d)

    def test_tampered_signature_rejected(self, tmp_path):
        d = _make_pack(tmp_path)
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="signature mismatch"):
            verify_manifest(d)

    def test_missing_manifest_rejected(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError, match="manifest missing"):
            verify_manifest(tmp_path / "empty")


class TestRoundTrip:
    def test_publish_install_roundtrip_tenant_scoped(self, env, tmp_path):
        ppm, mp, persona_root = env
        pack_dir = _make_pack(tmp_path)
        pack_id = ppm.publish(pack_dir, tenant="acme-tenant")
        assert pack_id
        result = ppm.install(pack_id, tenant="acme-tenant")
        assert result["status"] == "ok"
        installed = persona_root / "tenants" / "acme-tenant"
        assert (installed / "agent.md").is_file()
        assert (installed / "persona" / "tone.md").is_file()
        assert (installed / "rules" / "deny-shell.md").is_file()

    def test_install_records_audit_entries(self, env, tmp_path):
        ppm, mp, _ = env
        pack_id = ppm.publish(_make_pack(tmp_path), tenant="t1")
        ppm.install(pack_id, tenant="t1")
        events = [r["event"] for r in mp.audit_records(tenant=None, limit=50)]
        assert "persona_pack_publish" in events
        assert "persona_pack_install" in events

    def test_install_private_pack_hidden_from_other_tenant(self, env, tmp_path):
        ppm, _, _ = env
        d = _make_pack(tmp_path)
        build_manifest(d, layer="tenant", name="acme", visibility="private")
        pack_id = ppm.publish(d, tenant="tenant-a")
        assert ppm.get(pack_id, tenant="tenant-b") is None
        result = ppm.install(pack_id, tenant="tenant-b")
        assert result["status"] == "error"
        assert "not visible" in result["message"]

    def test_global_pack_installs_at_root(self, env, tmp_path):
        ppm, _, persona_root = env
        d = _make_pack(tmp_path, layer="global", name="")
        pack_id = ppm.publish(d, tenant="default")
        result = ppm.install(pack_id)
        assert result["status"] == "ok"
        assert (persona_root / "agent.md").is_file()

    def test_signature_verification_blocks_swapped_pack(self, env, tmp_path):
        ppm, _, _ = env
        pack_id = ppm.publish(_make_pack(tmp_path), tenant="t1")
        stored = ppm.packs_dir / pack_id
        # tamper INSIDE the store after publish — install must refuse
        (stored / "rules" / "deny-shell.md").write_text("allow: .*", encoding="utf-8")
        result = ppm.install(pack_id, tenant="t1")
        assert result["status"] == "error"
        assert "hash mismatch" in result["message"]


class TestDenyLoosening:
    def test_removing_existing_deny_rejected(self, env, tmp_path):
        ppm, _, persona_root = env
        target = persona_root / "tenants" / "t1"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "deny-shell.md").write_text("deny: bash.*\n", encoding="utf-8")

        pack_id = ppm.publish(_make_pack(tmp_path, rules={}), tenant="t1")
        result = ppm.install(pack_id, tenant="t1")
        assert result["status"] == "error"
        assert "deny rule" in result["message"]
        # nothing was written
        assert not (target / "agent.md").exists()

    def test_restating_same_deny_allowed(self, env, tmp_path):
        ppm, _, persona_root = env
        target = persona_root / "tenants" / "t2"
        (target / "rules").mkdir(parents=True)
        (target / "rules" / "deny-shell.md").write_text("deny: bash.*\n", encoding="utf-8")

        pack_id = ppm.publish(
            _make_pack(tmp_path, rules={"deny-shell.md": "deny: bash.*"}), tenant="t2"
        )
        result = ppm.install(pack_id, tenant="t2")
        assert result["status"] == "ok"

    def test_denies_removed_unit(self):
        assert denies_removed(["deny: bash.*", "allow: git"], ["allow: git"]) == ["deny: bash.*"]
        assert denies_removed(["deny: bash.*"], ["deny: bash.*"]) == []
        assert denies_removed([], ["allow: .*"]) == []


class TestRpcSurface:
    async def test_rpc_publish_install_roundtrip(self, tmp_path, monkeypatch):
        from agent_runtime.daemon_server import DaemonServer

        monkeypatch.setenv("FUSION_ENABLE_WS", "0")
        daemon = DaemonServer(
            socket_path=str(tmp_path / "test.sock"),
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "test.db"),
        )
        import agent_runtime.persona as persona_mod
        from agent_runtime.agent_marketplace import AgentMarketplace as _AM

        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona_root")
        # temp marketplace BEFORE the pack manager snapshot takes it for audit
        daemon._marketplace = _AM(store_dir=tmp_path / "mp_store")
        ppm = daemon._get_persona_pack_manager()
        ppm.persona_root = tmp_path / "persona_root"
        ppm.store_dir = tmp_path / "pack_store"
        ppm.index_path = tmp_path / "pack_store" / "persona_packs_index.json"
        ppm.packs_dir = tmp_path / "pack_store" / "persona_packs"
        ppm._index = {}

        pack_dir = _make_pack(tmp_path)
        res = await daemon._get_handler("marketplace.persona_pack_publish")(
            {"pack_dir": str(pack_dir), "layer": "tenant", "name": "acme", "tenant": "t1"}
        )
        assert "pack_id" in res
        pack_id = res["pack_id"]

        listed = await daemon._get_handler("marketplace.persona_pack_list")({"tenant": "t1"})
        assert listed["packs"][0]["id"] == pack_id

        res = await daemon._get_handler("marketplace.persona_pack_install")(
            {"pack_id": pack_id, "tenant": "t1"}
        )
        assert res["status"] == "ok"
        # tenant packs install into the installing tenant's scope
        assert (tmp_path / "persona_root" / "tenants" / "t1" / "agent.md").is_file()
