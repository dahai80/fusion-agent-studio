"""#482: persona declarations consumed at runtime + shard directory layout.

ACs:
1. Declarations consumed: rules reach the permission engine (deny enforced),
   memory_scope restricts the memory tool group's scope, skills restrict the
   skill invoke surface — each wired at runtime, each with a test.
2. Shard layout: persona/*.md merged in deterministic sorted order +
   rules/*.md one rule per file, backward-compatible with single agent.md.
"""

from pathlib import Path

import pytest

import agent_runtime.persona as persona_mod
from agent_runtime.persona_gate import (
    MEMORY_SCOPE_TOOLS,
    PersonaGate,
    PersonaScopedFunnel,
)


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def persona_root(tmp_path, monkeypatch):
    monkeypatch.setattr(persona_mod, "PERSONA_ROOT", tmp_path)
    return tmp_path


class TestPersonaGate:
    def test_structured_rule_denies_tool(self):
        gate = PersonaGate({"rules": ["deny: terminal", "allow: file_read"]})
        assert not gate.active or gate.engine is not None
        ok, _, reason = gate.apply("terminal", {})
        assert not ok
        assert "terminal" in reason
        ok, _, _ = gate.apply("file_read", {})
        assert ok
        ok, _, _ = gate.apply("unlisted_tool", {})
        assert ok, "persona engine mode=approved auto-allows non-denied"

    def test_free_text_rule_is_advisory(self):
        gate = PersonaGate({"rules": ["cite evidence for claims"]})
        assert gate.engine is None
        assert gate.advisory_rules == ["cite evidence for claims"]
        ok, _, _ = gate.apply("terminal", {})
        assert ok, "advisory rules must not hard-block"

    def test_memory_scope_forces_scope(self):
        gate = PersonaGate({"memory_scope": "project"})
        for tool in sorted(MEMORY_SCOPE_TOOLS):
            ok, args, _ = gate.apply(tool, {"query": "x"})
            assert ok
            assert args["scope"] == "project"
        ok, args, _ = gate.apply("memory_search", {"query": "x"})
        assert ok and "scope" not in args, "memory_search has no scope param"

    def test_skills_allowlist(self):
        gate = PersonaGate({"skills": ["review", "test"]})
        assert gate.skill_allowed("review")
        assert not gate.skill_allowed("deploy")
        open_gate = PersonaGate({})
        assert open_gate.skill_allowed("anything"), "no declaration = open surface"


class TestFunnelWiring:
    async def test_persona_deny_blocks_at_funnel(self):
        from agent_runtime.context import AgentContext

        funnel = PersonaScopedFunnel()
        ctx = AgentContext()
        ctx.persona_declarations = {"rules": ["deny: terminal"]}
        out = await funnel.admit(ctx=ctx, tool_name="terminal", args={"command": "ls"})
        assert not out.allowed
        assert out.blocked_by == "persona"
        assert out.args == {"command": "ls"}
        assert out.decision_log[0]["stage"] == "persona"

    async def test_memory_scope_rewritten_args_flow_through(self):
        from agent_runtime.context import AgentContext

        funnel = PersonaScopedFunnel()
        ctx = AgentContext()
        ctx.persona_declarations = {"memory_scope": "project"}
        out = await funnel.admit(ctx=ctx, tool_name="memory_read", args={"query": "x"})
        assert out.args.get("scope") == "project"

    async def test_no_declarations_no_behavior_change(self):
        from agent_runtime.context import AgentContext

        funnel = PersonaScopedFunnel(permissions=None)
        ctx = AgentContext()
        out = await funnel.admit(ctx=ctx, tool_name="anything", args={})
        # bare funnel has no permission engine -> fail-closed, but the point
        # is the persona stage did not add a deny: blocked_by stays empty
        assert out.blocked_by != "persona"

    async def test_gate_cached_per_ctx(self):
        from agent_runtime.context import AgentContext

        funnel = PersonaScopedFunnel()
        ctx = AgentContext()
        ctx.persona_declarations = {"rules": ["deny: terminal"]}
        await funnel.admit(ctx=ctx, tool_name="terminal", args={})
        cached = getattr(ctx, "_persona_gate")
        assert cached is not None
        await funnel.admit(ctx=ctx, tool_name="terminal", args={})
        assert getattr(ctx, "_persona_gate") is cached


class TestRuntimeConsumption:
    def _runtime(self):
        from agent_runtime.runtime import AgentRuntime
        from tools.registry import ToolRegistry

        return AgentRuntime(None, ToolRegistry())

    async def test_resolve_persona_declarations_from_tenant_layer(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nrules:\n  - deny: terminal\nmemory_scope: project\n---\n\n## Role\n\nbe safe\n",
        )
        rt = self._runtime()
        from agent_runtime.context import AgentContext

        ctx = AgentContext()
        ctx.agent_id = "a1"
        ctx.metadata["tenant_id"] = "acme"
        rt._resolve_persona_declarations(ctx)
        assert ctx.persona_declarations["rules"] == ["deny: terminal"]
        assert ctx.persona_declarations["memory_scope"] == "project"

    async def test_no_agent_id_skips_resolution(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nrules:\n  - deny: terminal\n---\n",
        )
        rt = self._runtime()
        from agent_runtime.context import AgentContext

        ctx = AgentContext()
        ctx.metadata["tenant_id"] = "acme"
        rt._resolve_persona_declarations(ctx)
        assert ctx.persona_declarations == {}

    async def test_tool_node_blocked_by_persona_rule(self):
        from agent_runtime.context import AgentContext
        from agent_runtime.graph import AgentGraph, NodeConfig
        from agent_runtime.runtime import AgentRuntime
        from tools.base import BaseTool
        from tools.registry import ToolRegistry

        class _BoomTool(BaseTool):
            def __init__(self):
                self.name = "boom_tool"
                self.description = "test tool"
                self.parameters = {"x": {"type": "string"}}

            async def execute(self, **kwargs):
                return {"ok": True}

        registry = ToolRegistry()
        registry.register(_BoomTool())
        rt = AgentRuntime(None, registry)
        graph = AgentGraph(name="persona-deny-482")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("boom", NodeConfig(type="tool", label="Boom", tool_name="boom_tool"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "boom")
        graph.add_edge("boom", "end")
        ctx = AgentContext()
        ctx.persona_declarations = {"rules": ["deny: boom_tool"]}

        events = [e async for e in rt.execute_graph(graph, "go", context=ctx)]
        assert ctx.error and "persona" in ctx.error.lower(), ctx.error
        assert any(getattr(e, "type", None) and "ERROR" in str(e.type) for e in events)


class TestSkillsAllowlist:
    def _dispatcher(self):
        from agent_runtime.dispatchers.skills import SkillsDispatcher

        return SkillsDispatcher(daemon=object())

    async def test_declared_skill_allowed(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nskills:\n  - review\n---\n",
        )
        d = self._dispatcher()
        assert d._persona_skill_check(
            {"agent_id": "a1", "tenant_id": "acme"}, "review"
        ) is None

    async def test_undeclared_skill_refused(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nskills:\n  - review\n---\n",
        )
        d = self._dispatcher()
        out = d._persona_skill_check({"agent_id": "a1", "tenant_id": "acme"}, "deploy")
        assert out is not None
        assert out["status"] == "error"
        assert "allowlist" in out["message"]

    async def test_no_agent_id_open_surface(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nskills:\n  - review\n---\n",
        )
        d = self._dispatcher()
        assert d._persona_skill_check({"tenant_id": "acme"}, "deploy") is None


class TestShardLayout:
    def test_shards_and_rule_files_merged(self, persona_root):
        _write(
            persona_root / "agent.md",
            "---\nmemory_scope: global\n---\n\n## Core\n\nbase\n",
        )
        _write(
            persona_root / "persona" / "b-shard.md",
            "---\nrules:\n  - cite evidence\nskills:\n  - review\n---\n\n## Extra\n\nb\n",
        )
        _write(
            persona_root / "persona" / "a-shard.md",
            "---\nmemory_scope: project\nrules:\n  - deny: terminal\n---\n\n## Alpha\n\na\n",
        )
        _write(persona_root / "rules" / "10-no-shell.md", "deny: shell\n")
        _write(persona_root / "rules" / "05-no-write.md", "deny: file_write\n")

        text = persona_mod.load_layer("global")
        declared = persona_mod.parse_declarations(text)
        # deterministic order: agent.md first, then shards sorted, then
        # rules/*.md sorted by filename
        assert declared["rules"] == [
            "deny: terminal",
            "cite evidence",
            "deny: file_write",
            "deny: shell",
        ]
        # shard overrides agent.md memory_scope (later block wins)
        assert declared["memory_scope"] == "project"
        assert declared["skills"] == ["review"]
        # body sections from all shards survive, frontmatter stripped
        sections = persona_mod.parse_sections(text)
        assert sections.get("Core") == "base"
        assert sections.get("Alpha") == "a"
        assert sections.get("Extra") == "b"
        assert "---" not in (persona_mod._strip_frontmatter_blocks(text))

    def test_single_agent_md_backward_compatible(self, persona_root):
        _write(
            persona_root / "agent.md",
            "---\nrules:\n  - deny: terminal\nmemory_scope: project\nskills:\n  - review\n---\n\n## Role\n\none file\n",
        )
        text = persona_mod.load_layer("global")
        declared = persona_mod.parse_declarations(text)
        assert declared == {
            "rules": ["deny: terminal"],
            "memory_scope": "project",
            "skills": ["review"],
        }
        assert persona_mod.parse_sections(text) == {"Role": "one file"}

    def test_tenant_layer_shards(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nmemory_scope: tenant-wide\n---\n",
        )
        _write(
            persona_root / "tenants" / "acme" / "persona" / "s.md",
            "---\nrules:\n  - deny: terminal\n---\n",
        )
        declared = persona_mod.parse_declarations(persona_mod.load_layer("tenant", "acme"))
        assert declared["rules"] == ["deny: terminal"]
        assert declared["memory_scope"] == "tenant-wide"

    def test_hr_lines_not_eaten(self):
        md = "## Notes\n\nbefore\n\n---\n\nafter hr\n"
        assert persona_mod._strip_frontmatter_blocks(md) == md

    async def test_resolve_persona_picks_up_shards(self, persona_root):
        _write(
            persona_root / "tenants" / "acme" / "agent.md",
            "---\nmemory_scope: base\n---\n",
        )
        _write(
            persona_root / "tenants" / "acme" / "persona" / "z.md",
            "---\nrules:\n  - deny: terminal\nmemory_scope: project\nskills:\n  - review\n---\n",
        )
        result = persona_mod.resolve_persona(
            agent_id="a1", tenant_id="acme", fallback="soul text"
        )
        assert result["persona"] == "soul text"
        assert result["declarations"] == {
            "rules": ["deny: terminal"],
            "memory_scope": "project",
            "skills": ["review"],
        }
        assert "tenant:acme" in result["layers_applied"]
