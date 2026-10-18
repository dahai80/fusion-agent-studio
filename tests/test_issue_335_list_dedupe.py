"""#335 acceptance F-4: dedupe graph/agent lists and purge test residue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.dispatchers.agent import AgentDispatcher
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.persistence import AgentStore


def _graph(graph_id: str, name: str) -> AgentGraph:
    g = AgentGraph(name=name, start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "end")
    g.id = graph_id
    return g


@pytest.fixture
def store(tmp_path):
    s = AgentStore(db_path=tmp_path / "store.db")
    s.save_graph(_graph("g1", "dup"))
    s.save_graph(_graph("g2", "dup"))
    s.save_graph(_graph("g3", "dup"))
    s.save_graph(_graph("g4", "unique"))
    return s


class TestGraphListDedupe:
    def test_store_raw_vs_deduped(self, store):
        assert len(store.list_graphs()) == 4
        deduped = store.list_graphs(dedupe_names=True)
        assert len(deduped) == 2
        by_name = {g["name"]: g for g in deduped}
        assert by_name["dup"]["duplicate_count"] == 2
        assert by_name["unique"]["duplicate_count"] == 0

    @pytest.mark.asyncio
    async def test_handler_dedupes_by_default(self, store):
        class Stub:
            pass

        stub = Stub()
        stub.store = store
        result = await DaemonServer._handle_graph_list(stub, {})
        assert len(result["graphs"]) == 2
        assert all("duplicate_count" in g for g in result["graphs"])

    @pytest.mark.asyncio
    async def test_handler_dedupe_false_returns_all(self, store):
        class Stub:
            pass

        stub = Stub()
        stub.store = store
        result = await DaemonServer._handle_graph_list(stub, {"dedupe": False})
        assert len(result["graphs"]) == 4
        assert not any("duplicate_count" in g for g in result["graphs"])


class TestAgentListDedupe:
    def _daemon(self):
        class Stub:
            def __init__(self):
                self._agents = {
                    "a1": {"name": "dup", "version_int": 1, "created_at": "2026-01-01"},
                    "a2": {"name": "dup", "version_int": 3, "created_at": "2026-03-01"},
                    "a3": {"name": "dup", "version_int": 2, "created_at": "2026-02-01"},
                    "a4": {"name": "solo", "version_int": 1, "created_at": "2026-01-01"},
                }
                self.persisted = 0

            def _load_agents_index(self):
                pass

            def _persist_agents_index(self):
                self.persisted += 1

        return Stub()

    @pytest.mark.asyncio
    async def test_dedupe_keeps_newest_version(self):
        daemon = self._daemon()
        result = await AgentDispatcher(daemon)._handle_agent_list({})
        assert len(result["agents"]) == 2
        by_name = {a["name"]: a for a in result["agents"]}
        assert by_name["dup"]["id"] == "a2"
        assert by_name["dup"]["duplicate_count"] == 2

    @pytest.mark.asyncio
    async def test_dedupe_false_returns_all(self):
        daemon = self._daemon()
        result = await AgentDispatcher(daemon)._handle_agent_list({"dedupe": False})
        assert len(result["agents"]) == 4


class TestAgentPurgeTest:
    def _daemon(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        agents_root = tmp_path / ".fusion-agent-studio" / "agents"
        agents = {}
        for aid, name in [
            ("c12_a", "c12_config"),
            ("c12_b", "c12_stream"),
            ("bot_a", "PyBot"),
            ("real", "抖音电商带货"),
        ]:
            pkg_dir = agents_root / aid / ".fusion-agent"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "manifest.json").write_text(json.dumps({"name": name}), encoding="utf-8")
            agents[aid] = {"name": name}

        class Stub:
            def __init__(self):
                self._agents = agents
                self.persisted = 0

            def _load_agents_index(self):
                pass

            def _persist_agents_index(self):
                self.persisted += 1

            @staticmethod
            def _agent_dir(agent_id):
                return agents_root / agent_id

        return Stub(), agents_root

    @pytest.mark.asyncio
    async def test_dry_run(self, tmp_path, monkeypatch):
        daemon, root = self._daemon(tmp_path, monkeypatch)
        result = await AgentDispatcher(daemon)._handle_agent_purge_test(
            {"prefixes": ["c12_"], "names": ["PyBot"], "dry_run": True}
        )
        assert result == {"dry_run": True, "would_delete": 3, "total": 4}
        assert (root / "c12_a").exists()

    @pytest.mark.asyncio
    async def test_purge_removes_packages_and_keeps_real(self, tmp_path, monkeypatch):
        daemon, root = self._daemon(tmp_path, monkeypatch)
        result = await AgentDispatcher(daemon)._handle_agent_purge_test(
            {"prefixes": ["c12_"], "names": ["PyBot"]}
        )
        assert result["deleted"] == 3
        assert result["after"] == 1
        assert daemon.persisted == 1
        assert not (root / "c12_a" / ".fusion-agent").exists()
        assert not (root / "bot_a" / ".fusion-agent").exists()
        assert (root / "real" / ".fusion-agent" / "manifest.json").exists()
        assert list(daemon._agents) == ["real"]
