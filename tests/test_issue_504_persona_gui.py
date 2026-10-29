"""Tests for issue #504 — GUI persona editor backend surface.

Acceptance: a GUI surface can display the effective persona declarations
and edit a layer file, round-tripping through save_layer.

Here we cover the RPC/file plumbing the GUI drives:
- agent.list_persona_files / get_persona_file / set_persona_file
- path allowlist (traversal rejected, nothing written outside the layer dir)
- agent.resolve_persona returns declarations + conflicts for the panel
"""

from __future__ import annotations

import pytest

import agent_runtime.persona as persona_mod
from agent_runtime.persona import (
    list_layer_files,
    read_layer_file,
    write_layer_file,
)


class TestLayerFiles:
    def test_write_read_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona")
        write_layer_file("tenant", "acme", "persona/tone.md", "## Tone\n\nDry.\n")
        write_layer_file("tenant", "acme", "rules/deny-shell.md", "deny: bash.*\n")
        write_layer_file("tenant", "acme", "agent.md", "# Acme\n")
        assert read_layer_file("tenant", "acme", "persona/tone.md") == "## Tone\n\nDry.\n"
        assert list_layer_files("tenant", "acme") == [
            "agent.md",
            "persona/tone.md",
            "rules/deny-shell.md",
        ]

    def test_list_empty_or_missing_layer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona")
        assert list_layer_files("tenant", "ghost") == []
        assert list_layer_files("global") == []

    def test_path_allowlist_rejects_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona")
        for bad in (
            "../evil.md",
            "persona/../../evil.md",
            "/etc/passwd",
            "rules/x.py",
            "",
            "AGENT.MD",
        ):
            with pytest.raises(ValueError, match="not allowed"):
                write_layer_file("tenant", "acme", bad, "x")
        # nothing escaped the layer dir
        assert list((tmp_path / "persona").rglob("*")) == []

    def test_global_layer_requires_no_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona")
        write_layer_file("global", "", "agent.md", "# root\n")
        assert read_layer_file("global", "", "agent.md") == "# root\n"
        with pytest.raises(ValueError, match="requires name"):
            write_layer_file("tenant", "", "agent.md", "x")

    def test_rules_keep_multiline(self, tmp_path, monkeypatch):
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona")
        write_layer_file("team", "ops", "rules/extra.md", "deny: curl\n\n# comment\n")
        assert read_layer_file("team", "ops", "rules/extra.md") == "deny: curl\n\n# comment\n"


class TestRpcSurface:
    def test_web_rpc_allowlist_includes_persona_files(self):
        from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

        for m in (
            "agent.list_persona_files",
            "agent.get_persona_file",
            "agent.set_persona_file",
        ):
            assert m in WEB_RPC_ALLOWLIST

    async def test_rpc_roundtrip(self, tmp_path, monkeypatch):
        from agent_runtime.daemon_server import DaemonServer

        monkeypatch.setenv("FUSION_ENABLE_WS", "0")
        daemon = DaemonServer(
            socket_path=str(tmp_path / "test.sock"),
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "test.db"),
        )
        monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path / "persona_root")

        listed = await daemon._get_handler("agent.list_persona_files")(
            {"layer": "tenant", "name": "acme"}
        )
        assert listed["files"] == []

        saved = await daemon._get_handler("agent.set_persona_file")(
            {"layer": "tenant", "name": "acme", "path": "persona/tone.md", "content": "## T\n"}
        )
        assert saved["status"] == "ok"

        listed = await daemon._get_handler("agent.list_persona_files")(
            {"layer": "tenant", "name": "acme"}
        )
        assert listed["files"] == ["persona/tone.md"]

        got = await daemon._get_handler("agent.get_persona_file")(
            {"layer": "tenant", "name": "acme", "path": "persona/tone.md"}
        )
        assert got["content"] == "## T\n"

        bad = await daemon._get_handler("agent.get_persona_file")(
            {"layer": "tenant", "name": "acme", "path": "../evil.md"}
        )
        assert bad["status"] == "error"
        assert "not allowed" in bad["message"]

        set_layer = await daemon._get_handler("agent.set_persona_layer")(
            {"layer": "tenant", "name": "acme", "content": "# flat editor\n"}
        )
        assert set_layer["layer"] == "tenant"
        merged = await daemon._get_handler("agent.resolve_persona")({"agent_id": "a1"})
        assert "declarations" in merged and "conflicts" in merged
