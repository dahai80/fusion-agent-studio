import json
from pathlib import Path

import pytest

from agent_runtime.bundle_package import BundleInstaller, load_bundle_manifest
from agent_runtime.connectors import ConnectorManager
from agent_runtime.dispatchers.bundle import BundleDispatcher
from agent_runtime.hooks import HookEngine
from agent_runtime.skills_store import SkillsStore


class FakeGraphStore:
    def __init__(self):
        self.graphs = {}

    def save_graph(self, graph):
        self.graphs[graph.id] = graph

    def delete_graph(self, graph_id):
        return self.graphs.pop(graph_id, None) is not None


def _write_bundle(root: Path, manifest: dict, skill_body: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if skill_body:
        (root / "skills").mkdir(exist_ok=True)
        (root / "skills" / "review.md").write_text(skill_body, encoding="utf-8")
    return root


def _installer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUNDLE_TEST_KEY", "secret-value")
    registered = []
    unregistered = []

    async def mcp_register(config):
        registered.append(config)
        return {"status": "ok", "server_url": config.get("server_url", "")}

    async def mcp_unregister(server):
        unregistered.append(server)
        return {"status": "ok"}

    skills = SkillsStore(skills_dir=str(tmp_path / "skills"))
    hooks = HookEngine(auto_register_decorators=False)
    connectors = ConnectorManager(tmp_path)
    store = FakeGraphStore()
    installer = BundleInstaller(
        home_dir=tmp_path,
        skills_store=skills,
        hook_engine=hooks,
        connector_manager=connectors,
        store=store,
        agents_dir=tmp_path / "agents",
        mcp_register=mcp_register,
        mcp_unregister=mcp_unregister,
    )
    return installer, skills, hooks, connectors, store, registered, unregistered


def _full_manifest() -> dict:
    return {
        "name": "review-kit",
        "version": "1.2.0",
        "agent": {
            "name": "reviewer",
            "description": "Code review agent",
            "system_prompt": "Review changes",
        },
        "graph": {
            "id": "review-graph",
            "name": "Review graph",
            "nodes": {"start": {"type": "start"}, "end": {"type": "end"}},
            "edges": [{"source_id": "start", "target_id": "end"}],
            "start_node_id": "start",
        },
        "skills": [{"name": "review", "path": "skills/review.md"}],
        "hooks": [{"event": "POST_TOOL", "matcher": "write_file", "type": "log"}],
        "connectors": [
            {
                "id": "gh",
                "name": "GitHub",
                "type": "api_key",
                "auth_config": {"api_key": "env:BUNDLE_TEST_KEY"},
            }
        ],
        "mcp_servers": [{"server_url": "http://127.0.0.1:9999"}],
    }


def test_manifest_parses_all_facets(tmp_path):
    root = _write_bundle(tmp_path / "bundle", _full_manifest())
    manifest = load_bundle_manifest(root)
    assert manifest is not None
    assert manifest.name == "review-kit"
    assert len(manifest.agent_entries) == 1
    assert manifest.is_bundle
    assert manifest.to_dict()["mcp_servers"] == 1


@pytest.mark.asyncio
async def test_install_registers_every_facet_and_uninstall_removes_them(tmp_path, monkeypatch):
    installer, skills, hooks, connectors, store, registered, unregistered = _installer(
        tmp_path, monkeypatch
    )
    root = _write_bundle(
        tmp_path / "bundle",
        _full_manifest(),
        skill_body="---\nname: review\ndescription: review code\n---\nReview {{path}}\n",
    )
    result = await installer.install(root)
    assert result["ok"], result
    facets = result["facets"]
    assert facets["agents"] == ["reviewer"]
    assert facets["graphs"] == ["review-graph"]
    assert facets["skills"] == ["review"]
    assert len(facets["hooks"]) == 1
    assert facets["connectors"] == ["gh"]
    assert facets["mcp_servers"] == ["http://127.0.0.1:9999"]

    assert (tmp_path / "agents" / "reviewer" / ".fusion-agent" / "manifest.json").exists()
    index = json.loads((tmp_path / "agents" / "index.json").read_text())
    assert index["reviewer"]["name"] == "reviewer"
    assert skills.has_skill("review")
    assert len(hooks.list_hooks()) == 1
    assert store.graphs["review-graph"].id == "review-graph"
    gh = connectors.get("gh")
    assert gh is not None
    assert connectors._connectors["gh"].auth_config["api_key"] == "secret-value"
    assert len(registered) == 1

    listed = installer.list()
    assert len(listed) == 1
    assert listed[0]["name"] == "review-kit"

    removed = await installer.uninstall("review-kit")
    assert removed["ok"], removed
    assert not (tmp_path / "agents" / "reviewer").exists()
    assert not skills.has_skill("review")
    assert hooks.list_hooks() == []
    assert store.graphs == {}
    assert connectors.get("gh") is None
    assert unregistered == ["http://127.0.0.1:9999"]
    assert installer.list() == []


@pytest.mark.asyncio
async def test_inline_credentials_rollback_everything(tmp_path, monkeypatch):
    installer, skills, hooks, connectors, store, registered, unregistered = _installer(
        tmp_path, monkeypatch
    )
    manifest = _full_manifest()
    manifest["connectors"][0]["auth_config"] = {"api_key": "plain-text-key"}
    root = _write_bundle(tmp_path / "bundle", manifest, skill_body="---\nname: review\n---\nbody\n")
    result = await installer.install(root)
    assert not result["ok"]
    assert any("inline credentials" in e for e in result["errors"])
    assert result["rolled_back"]["removed"]["agents"] == ["reviewer"]
    assert not skills.has_skill("review")
    assert hooks.list_hooks() == []
    assert store.graphs == {}
    assert len(registered) == 1
    assert unregistered == ["http://127.0.0.1:9999"]
    assert installer.list() == []


@pytest.mark.asyncio
async def test_missing_env_reference_fails_install(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_BUNDLE_KEY", raising=False)
    installer, *_ = _installer(tmp_path, monkeypatch)
    manifest = _full_manifest()
    manifest["connectors"][0]["auth_config"] = {"api_key": "env:MISSING_BUNDLE_KEY"}
    root = _write_bundle(tmp_path / "bundle", manifest)
    result = await installer.install(root)
    assert not result["ok"]
    assert any("MISSING_BUNDLE_KEY" in e for e in result["errors"])
    assert installer.list() == []


@pytest.mark.asyncio
async def test_manifest_without_facets_is_rejected(tmp_path, monkeypatch):
    installer, *_ = _installer(tmp_path, monkeypatch)
    root = _write_bundle(tmp_path / "empty", {"name": "empty"})
    result = await installer.install(root)
    assert not result["ok"]
    assert "no bundle facets" in result["error"]


@pytest.mark.asyncio
async def test_dispatcher_is_wired_into_daemon(tmp_path, monkeypatch):
    from agent_runtime.daemon_server import DaemonServer

    daemon = DaemonServer(store_path=str(tmp_path / "store.db"))
    handlers = daemon._get_handler("bundle.list")
    assert handlers is not None
    monkeypatch.setattr(
        "agent_runtime.bundle_package.BundleInstaller.list", lambda self: [{"name": "x"}]
    )
    dispatcher = BundleDispatcher(daemon)
    out = await dispatcher._handle_list({})
    assert out["status"] == "ok"
    assert out["bundles"] == [{"name": "x"}]
