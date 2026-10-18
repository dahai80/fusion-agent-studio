import os
import threading
import time

import pytest

from agent_runtime.dispatchers.memory import MemoryDispatcher
from agent_runtime.dispatchers.plugin import PluginDispatcher
from agent_runtime.dispatchers.skills import SkillsDispatcher
from agent_runtime.memory_engine import MemoryEngine
from agent_runtime.persona import (
    PersonaWatcher,
    export_agents_md,
    import_agents_md,
    merge_persona_layers,
    parse_declarations,
    parse_frontmatter,
    resolve_persona,
)
from agent_runtime.protocol_plane import (
    ProtocolPlane,
    merge_headers,
    read_extensions,
    write_extensions,
)
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _Audit:
    def __init__(self):
        self.calls = []

    def log_action(self, **kwargs):
        self.calls.append(kwargs)


class _MemDaemon:
    def __init__(self, mem, audit=None):
        self.mem = mem
        self.audit = audit

    def _get_memory(self):
        return self.mem

    def _get_audit_logger(self):
        return self.audit


class _PluginEcho(BaseTool):
    def __init__(self):
        self.name = "plugin_echo"
        self.description = "echo"
        self.parameters = {"text": {"type": "string"}}

    async def execute(self, **kwargs) -> str:
        return str(kwargs.get("text", ""))


class _RuntimeStub:
    def __init__(self):
        self.tool_registry = ToolRegistry()


class _PluginDaemon:
    def __init__(self):
        self.runtime = _RuntimeStub()
        self._protocol_plane = ProtocolPlane()

    def _get_runtime(self):
        return self.runtime

    def _get_protocol_plane(self):
        return self._protocol_plane


@pytest.fixture()
def memory(tmp_path):
    return MemoryEngine(tmp_path / "memory.db")


def test_tenant_team_isolation(memory):
    memory.store("alpha fact", tenant_id="t1", team_id="a")
    memory.store("beta fact", tenant_id="t2", team_id="a")
    memory.store("gamma fact", tenant_id="t1", team_id="b")
    assert memory.count(tenant_id="t1") == 2
    assert memory.count(tenant_id="t1", team_id="b") == 1
    recent = memory.list_recent(tenant_id="t2")
    assert [e.content for e in recent] == ["beta fact"]
    hits = memory.recall("fact", tenant_id="t1", team_id="a")
    assert [e.content for e in hits] == ["alpha fact"]


def test_decay_score_halves_at_half_life(memory):
    now = time.time()
    score_now = memory.decay_score(8, now, now=now, half_life_days=10)
    score_half = memory.decay_score(8, now - 10 * 86400, now=now, half_life_days=10)
    assert score_now == pytest.approx(8.0)
    assert score_half == pytest.approx(4.0)


def test_sweep_dry_run_keeps_rows(memory):
    old = memory.store("old low value", importance=1)
    memory.store("fresh high value", importance=9)
    result = memory.sweep(threshold=1.0, half_life_days=1, dry_run=True)
    ids = [c["id"] for c in result["candidates"]]
    assert old in ids
    assert result["deleted"] == 0
    assert memory.get(old) is not None


def test_sweep_deletes_when_requested(memory):
    old = memory.store("old low value", importance=1)
    result = memory.sweep(threshold=1.0, half_life_days=1, dry_run=False)
    assert result["deleted"] == 1
    assert memory.get(old) is None


def test_sweep_max_age_reason(memory):
    entry_id = memory.store("stale but important", importance=10)
    result = memory.sweep(threshold=0.0, max_age_days=1e-9, dry_run=True)
    hit = next(c for c in result["candidates"] if c["id"] == entry_id)
    assert hit["reason"] == "max_age"


def test_cleanup_suggestions_never_deletes(memory):
    memory.store("disposable", importance=1)
    result = memory.cleanup_suggestions(threshold=100, half_life_days=1)
    assert result["dry_run"] is True
    assert result["deleted"] == 0
    assert memory.count() == 1


def test_conflict_detection(memory):
    memory.store("deploy uses blue green strategy", importance=5)
    memory.store("deploy uses blue green strategy now", importance=7)
    memory.store("lunch is sushi", importance=5)
    conflicts = memory.detect_conflicts(threshold=0.5)
    assert conflicts
    assert conflicts[0]["similarity"] >= 0.5
    assert conflicts[0]["winner_id"]


async def test_memory_dispatcher_audits_writes(memory):
    audit = _Audit()
    dispatcher = MemoryDispatcher(_MemDaemon(memory, audit))
    out = await dispatcher.get_handlers()["memory.store"](
        {"content": "audited fact", "tenant_id": "t1", "team_id": "team"}
    )
    assert out["entry_id"]
    assert audit.calls
    call = audit.calls[-1]
    assert call["action"] == "memory.store"
    assert call["details"]["tenant_id"] == "t1"
    assert call["details"]["team_id"] == "team"


async def test_memory_dispatcher_new_rpcs(memory):
    dispatcher = MemoryDispatcher(_MemDaemon(memory, _Audit()))
    handlers = dispatcher.get_handlers()
    memory.store("forget me", importance=1)
    suggestions = await handlers["memory.cleanup_suggestions"]({"threshold": 100})
    assert suggestions["dry_run"] is True
    memory.store("deploy uses blue green strategy", importance=5)
    memory.store("deploy uses blue green strategy now", importance=7)
    conflicts = await handlers["memory.conflicts"]({"threshold": 0.5})
    assert conflicts["count"] >= 1
    swept = await handlers["memory.decay_sweep"]({"threshold": 100, "dry_run": True})
    assert swept["deleted"] == 0


def test_frontmatter_and_declarations():
    text = (
        "---\n"
        "name: reviewer\n"
        "description: strict review agent\n"
        "rules:\n"
        "  - cite evidence\n"
        "  - never guess\n"
        "memory_scope: project\n"
        "skills: [review, test]\n"
        "---\n"
        "## Role\n"
        "review code\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "reviewer"
    assert meta["rules"] == ["cite evidence", "never guess"]
    assert meta["skills"] == ["review", "test"]
    assert body.lstrip().startswith("## Role")
    declared = parse_declarations(text)
    assert declared["memory_scope"] == "project"
    assert declared["rules"] == ["cite evidence", "never guess"]


def test_merge_declarations_precedence():
    low = "---\nrules:\n  - base rule\nmemory_scope: global\nskills: [a]\n---\n## Tone\nlow\n"
    high = "---\nrules:\n  - high rule\nmemory_scope: team\nskills: [b]\n---\n## Tone\nhigh\n"
    result = merge_persona_layers([("global", low), ("team", high)])
    assert result["declarations"]["rules"] == ["base rule", "high rule"]
    assert result["declarations"]["skills"] == ["a", "b"]
    assert result["declarations"]["memory_scope"] == "team"


def test_resolve_persona_surfaces_declarations(tmp_path, monkeypatch):
    import agent_runtime.persona as persona_mod

    root = tmp_path / "persona"
    (root / "teams" / "ops").mkdir(parents=True)
    (root / "agent.md").write_text("---\nrules:\n  - global\n---\n## Role\nglobal\n")
    (root / "teams" / "ops" / "agent.md").write_text(
        "---\nrules:\n  - team\nskills: [deploy]\n---\n## Role\nteam\n"
    )
    monkeypatch.setattr(persona_mod, "PERSONA_ROOT", root)
    result = resolve_persona(team_id="ops", agents_root=tmp_path / "agents")
    assert result["declarations"]["rules"] == ["global", "team"]
    assert result["declarations"]["skills"] == ["deploy"]


def test_agents_md_import_export_roundtrip(tmp_path):
    src = tmp_path / "agents_md"
    src.mkdir()
    (src / "reviewer.md").write_text(
        "---\n"
        "name: Reviewer\n"
        "description: strict reviewer\n"
        "model: local-model\n"
        "tools: [read, grep]\n"
        "---\n"
        "You review code carefully.\n"
    )
    agents_root = tmp_path / "agents"
    imported = import_agents_md(src, agents_root=agents_root)
    assert len(imported["imported"]) == 1
    agent_id = imported["imported"][0]["agent_id"]
    assert (agents_root / agent_id / ".fusion-agent" / "soul.md").is_file()

    out_dir = tmp_path / "exported"
    out = export_agents_md(agent_id, out_dir, agents_root=agents_root)
    assert out["status"] == "ok"
    text = (out_dir / f"{agent_id}.md").read_text()
    meta, body = parse_frontmatter(text)
    assert meta["name"] == "Reviewer"
    assert "You review code carefully." in body


def test_persona_watcher_detects_change(tmp_path):
    root = tmp_path / "persona"
    root.mkdir()
    target = root / "agent.md"
    target.write_text("## Role\nold\n")
    seen = threading.Event()
    changed_paths = []

    def _on_change(paths):
        changed_paths.extend(paths)
        seen.set()

    watcher = PersonaWatcher(root=root, interval=0.2, on_change=_on_change)
    watcher.start()
    try:
        target.write_text("## Role\nnew content\n")
        assert seen.wait(timeout=3.0)
        assert any("agent.md" in path for path in changed_paths)
    finally:
        watcher.stop()


def test_x_fusion_extensions_roundtrip():
    extensions = {"origin": "studio", "readonly": True}
    fields = write_extensions(extensions)
    assert fields == {"x-fusion-origin": "studio", "x-fusion-readonly": True}
    assert read_extensions(fields) == extensions
    assert read_extensions({"name": "tool"}) == {}
    assert read_extensions(None) == {}
    merged = merge_headers({"x-fusion-origin": "override"}, extensions)
    assert merged["x-fusion-origin"] == "override"
    assert merged["x-fusion-readonly"] is True


def test_protocol_require_strict(monkeypatch):
    plane = ProtocolPlane()
    monkeypatch.setenv("FUSION_PROTOCOL_STRICT", "1")
    with pytest.raises(ValueError):
        plane.require("skill", "ghost")
    plane.register("skill", "known")
    assert plane.require("skill", "known").name == "known"


def test_protocol_ensure_registers_without_strict(monkeypatch):
    plane = ProtocolPlane()
    monkeypatch.delenv("FUSION_PROTOCOL_STRICT", raising=False)
    art = plane.require("plugin", "auto")
    assert art.kind == "plugin"
    assert plane.get("plugin", "auto") is art


def test_protocol_check_chain():
    plane = ProtocolPlane()
    check = plane.check_chain(hooks=object(), permissions=None, budget=object())
    assert check["missing"] == ["permission"]
    assert check["armed"]["hooks"] is True


async def test_plugin_install_package_uninstall(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugins"
    monkeypatch.setenv("FUSION_PLUGIN_DIR", str(plugin_dir))
    daemon = _PluginDaemon()
    dispatcher = PluginDispatcher(daemon)
    pm = dispatcher._pm()
    pm.plugin_dir = plugin_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "my_plugin.py"
    source.write_text(
        "from tools.base import BaseTool\n"
        "class MyPluginTool(BaseTool):\n"
        "    def __init__(self):\n"
        "        self.name = 'my_plugin'\n"
        "        self.description = 'x'\n"
        "        self.parameters = {}\n"
        "    async def execute(self, **kwargs):\n"
        "        return 'ok'\n"
    )
    handlers = dispatcher.get_handlers()
    out = await handlers["plugin.install"]({"source": str(source)})
    assert out["status"] == "ok"
    assert daemon._protocol_plane.get("plugin", "my_plugin") is not None

    invoked = await handlers["plugin.invoke"]({"tool_name": "my_plugin", "arguments": {}})
    assert invoked.get("error") is None

    packaged = await handlers["plugin.package"]({"name": "my_plugin", "dest_dir": str(tmp_path / "dist")})
    assert packaged["status"] == "ok"
    assert os.path.isfile(packaged["path"])

    removed = await handlers["plugin.uninstall"]({"name": "my_plugin"})
    assert removed["removed"] is True
    assert daemon._protocol_plane.get("plugin", "my_plugin") is None


async def test_plugin_invoke_strict_denies_unregistered(tmp_path, monkeypatch):
    daemon = _PluginDaemon()
    dispatcher = PluginDispatcher(daemon)
    registry = daemon.runtime.tool_registry
    registry.register(_PluginEcho())
    registry.plugin_tools.add("plugin_echo")
    monkeypatch.setenv("FUSION_PROTOCOL_STRICT", "1")
    out = await dispatcher.get_handlers()["plugin.invoke"](
        {"tool_name": "plugin_echo", "arguments": {"text": "hi"}}
    )
    assert "error" in out
    assert "not registered" in out["error"]


async def test_skills_dispatcher_registers_artifacts(tmp_path):
    from agent_runtime.skills_store import SkillsStore

    daemon = _PluginDaemon()
    daemon._skills_store = SkillsStore(str(tmp_path / "skills"))
    dispatcher = SkillsDispatcher(daemon)
    out = await dispatcher.get_handlers()["skills.install"](
        {"name": "demo", "content": "---\nname: demo\ndescription: d\n---\n# Demo\n"}
    )
    assert out["status"] == "ok"
    assert daemon._protocol_plane.get("skill", "demo") is not None
    listing = await dispatcher.get_handlers()["skills.list"]({})
    assert listing["status"] == "ok"


def test_rpc_allowlist_contains_new_methods():
    from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

    for method in (
        "memory.decay_sweep",
        "memory.cleanup_suggestions",
        "memory.conflicts",
        "agent.import_agents_md",
        "agent.export_agents_md",
    ):
        assert method in WEB_RPC_ALLOWLIST
