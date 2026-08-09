"""Tests for issue #100 — /api/v1/agents contract + disk index rebuild.

Importers: pytest runner
API: /api/v1/agents alias routes, _rebuild_agents_index_from_disk
Data schemas: agents index.json (dict of agent_id -> manifest+id)
User instruction: "处理issue和pr，提交代码到代码仓，合并所有分支到主干，确保ci和lint全绿，发布补丁版本"
"""

from __future__ import annotations

import json
from pathlib import Path


def _make_manifest(name: str = "BotA") -> dict:
    return {
        "name": name,
        "version": "0.1.0",
        "description": "",
        "model": "",
        "system_prompt": f"You are {name}.",
        "temperature": 0.7,
        "max_tokens": 4096,
        "tools": [],
        "capabilities": [],
        "safety_level": "L1",
        "tags": [],
        "author": "",
        "created_at": "",
    }


class TestApiV1AgentsAlias:
    def test_api_v1_agents_resolves(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        resp = client.get("/api/v1/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert "total" in data

    def test_api_v1_agents_published_resolves(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        resp = client.get("/api/v1/agents/published")
        assert resp.status_code == 200

    def test_api_v1_agents_definition_404_for_unknown(self):
        from fastapi.testclient import TestClient

        from agent_runtime.api_server import app

        client = TestClient(app)
        resp = client.get("/api/v1/agents/nonexistent_id/definition")
        assert resp.status_code == 404


class TestRebuildAgentsIndexFromDisk:
    def test_rebuild_from_disk_manifests(self, tmp_path, monkeypatch):
        from agent_runtime import api_server

        fake_home = tmp_path / "home"
        agents_root = fake_home / ".fusion-agent-studio" / "agents"
        agents_root.mkdir(parents=True)
        aid = "abc123def456"
        agent_dir = agents_root / aid
        (agent_dir / ".fusion-agent").mkdir(parents=True)
        manifest_path = agent_dir / ".fusion-agent" / "manifest.json"
        manifest_path.write_text(
            json.dumps(_make_manifest("BotA")), encoding="utf-8"
        )

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(api_server, "_daemon", None)

        index = api_server._rebuild_agents_index_from_disk()
        assert aid in index
        assert index[aid]["id"] == aid
        assert index[aid]["name"] == "BotA"

        idx_path = agents_root / "index.json"
        assert idx_path.exists()

    def test_rebuild_skips_unreadable_manifest(self, tmp_path, monkeypatch):
        from agent_runtime import api_server

        fake_home = tmp_path / "home"
        agents_root = fake_home / ".fusion-agent-studio" / "agents"
        agents_root.mkdir(parents=True)
        agent_dir = agents_root / "badid0000000"
        (agent_dir / ".fusion-agent").mkdir(parents=True)
        (agent_dir / ".fusion-agent" / "manifest.json").write_text(
            "{not json", encoding="utf-8"
        )

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(api_server, "_daemon", None)

        index = api_server._rebuild_agents_index_from_disk()
        assert "badid0000000" not in index

    def test_rebuild_empty_when_no_agents_dir(self, tmp_path, monkeypatch):
        from agent_runtime import api_server

        fake_home = tmp_path / "emptyhome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setattr(api_server, "_daemon", None)

        index = api_server._rebuild_agents_index_from_disk()
        assert index == {}


class TestDaemonRebuildIndex:
    def test_daemon_rebuild_populates_agents(self, tmp_path, monkeypatch):
        from agent_runtime.daemon_server import DaemonServer

        fake_home = tmp_path / "daemonhome"
        agents_root = fake_home / ".fusion-agent-studio" / "agents"
        aid = "xyz789abc123"
        agent_dir = agents_root / aid
        (agent_dir / ".fusion-agent").mkdir(parents=True)
        (agent_dir / ".fusion-agent" / "manifest.json").write_text(
            json.dumps(_make_manifest("BotDaemon")), encoding="utf-8"
        )

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        daemon = DaemonServer(socket_path=str(tmp_path / "test.sock"))
        daemon._load_agents_index()
        assert aid in daemon._agents
        assert daemon._agents[aid]["id"] == aid
        assert daemon._agents[aid]["name"] == "BotDaemon"
