"""Tests for #421/#422/#428/#434 — declarative subagent definitions,
physical tool deny, per-subagent compaction window + effort, memory scopes."""

from __future__ import annotations

import json

import pytest

from agent_runtime.subagent import SubAgentRunner, SubAgentSpec
from agent_runtime.subagent_defs import (
    AgentDefinition,
    AgentDefinitionRegistry,
    parse_agent_md,
)
from tests.test_issue_341_subagent import T1Tool, T2Tool, _llm_response, _make_parent
from tools.base import BaseTool
from tools.registry import ToolRegistry


class MCPStubTool(BaseTool):
    name = "mcp__docs__search"
    description = "MCP search stub"
    parameters = {"q": {"type": "string"}}

    def __init__(self):
        super().__init__()
        self.mcp_server = "docs"

    async def execute(self, q="", **kwargs) -> str:
        return f"docs:{q}"


class FakeMemoryEngine:
    def __init__(self):
        self.recall_calls: list[dict] = []
        self.store_calls: list[dict] = []

    def recall_relevant(self, query, limit=5, scope="", memory_type=""):
        self.recall_calls.append({"query": query, "scope": scope})
        return f"remembered:{scope}"

    def store(self, content, scope="default", tags="", importance=5,
              metadata=None, tier="", is_summary=False, memory_type="",
              tenant_id="", team_id=""):
        self.store_calls.append({"content": content, "scope": scope, "memory_type": memory_type})
        return "mem-1"


FRONTMATTER_MD = """---
name: reviewer
description: Read-only reviewer
model: qwen-review
tools: read_file, search
disallowedTools: write_file, terminal
effort: high
memory: project
maxTurns: 4
autoCompactWindow: 4096
background: true
permissionMode: plan
mcpServers: docs
skills: checklist
---
You review code and report findings.
"""


class TestAgentDefinitionParsing:
    def test_markdown_frontmatter_camelcase(self):
        definition = parse_agent_md(FRONTMATTER_MD, fallback_name="ignored")
        assert definition.name == "reviewer"
        assert definition.description == "Read-only reviewer"
        assert definition.prompt == "You review code and report findings."
        assert definition.tools == ["read_file", "search"]
        assert definition.disallowed_tools == ["write_file", "terminal"]
        assert definition.effort == "high"
        assert definition.memory == "project"
        assert definition.max_turns == 4
        assert definition.auto_compact_window == 4096
        assert definition.background is True
        assert definition.permission_mode == "plan"
        assert definition.mcp_servers == ["docs"]
        assert definition.skills == ["checklist"]

    def test_fallback_name_when_no_frontmatter(self):
        definition = parse_agent_md("Plain prompt body.", fallback_name="plain")
        assert definition.name == "plain"
        assert definition.prompt == "Plain prompt body."
        assert definition.to_dict()["disallowedTools"] == []

    def test_unknown_effort_and_memory_disabled(self):
        definition = AgentDefinition.from_dict(
            {"name": "x", "effort": "turbo", "memory": "galaxy"}
        )
        assert definition.effort == ""
        assert definition.memory == ""

    def test_to_spec_maps_fields(self):
        definition = parse_agent_md(FRONTMATTER_MD, fallback_name="r")
        spec = definition.to_spec(task="check patch")
        assert isinstance(spec, SubAgentSpec)
        assert spec.task == "check patch"
        assert spec.system_prompt == "You review code and report findings."
        assert spec.disallowed_tools == ["write_file", "terminal"]
        assert spec.effort == "high"
        assert spec.memory_scope == "project"
        assert spec.auto_compact_window == 4096
        assert spec.background is True
        assert spec.skills == ["checklist"]

    def test_spec_roundtrip_keeps_new_fields(self):
        spec = SubAgentSpec(
            name="n", task="t", disallowed_tools=["x"], effort="low",
            auto_compact_window=1024, memory_scope="user", background=True,
            skills=["s"], mcp_servers=["docs"], permission_mode="plan",
        )
        assert SubAgentSpec.from_dict(spec.to_dict()) == spec

    def test_registry_discover_project_overrides_user(self, tmp_path, monkeypatch):
        user_dir = tmp_path / "user"
        project_dir = tmp_path / "proj"
        (user_dir / ".fusion-agent-studio" / "agents").mkdir(parents=True)
        (project_dir / ".fusion-agent-studio" / "agents").mkdir(parents=True)
        (user_dir / ".fusion-agent-studio" / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\ntools: read_file\n---\nuser tier prompt\n",
            encoding="utf-8",
        )
        (project_dir / ".fusion-agent-studio" / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\ntools: read_file, write_file\n---\nproject tier prompt\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("agent_runtime.subagent_defs.DEFAULT_USER_AGENTS_DIR", str(user_dir / ".fusion-agent-studio" / "agents"))
        registry = AgentDefinitionRegistry()
        count = registry.discover(project_dir=str(project_dir))
        definition = registry.get("reviewer")
        assert count >= 2
        assert definition is not None
        assert definition.tools == ["read_file", "write_file"]
        assert definition.prompt == "project tier prompt"
        assert definition.source == "project"

    def test_programmatic_register_requires_name(self):
        registry = AgentDefinitionRegistry()
        with pytest.raises(ValueError):
            registry.register(AgentDefinition(name=""))


class TestPhysicalToolDeny:
    @pytest.mark.asyncio
    async def test_disallowed_patterns_removed_from_registry(self):
        tools = ToolRegistry()
        tools.register(T1Tool())
        tools.register(T2Tool())
        rt, _ = _make_parent([_llm_response(content="ok")], tools=tools)
        runner = SubAgentRunner(rt)
        spec = SubAgentSpec(
            name="ro", task="t", tools=["t1", "t2"], disallowed_tools=["t2"]
        )
        reg, missing = runner._build_registry(spec)
        assert missing == []
        assert reg.has("t1") is True
        assert reg.has("t2") is False

    @pytest.mark.asyncio
    async def test_glob_pattern_deny(self):
        tools = ToolRegistry()
        tools.register(T1Tool())
        tools.register(T2Tool())
        rt, _ = _make_parent([_llm_response(content="ok")], tools=tools)
        runner = SubAgentRunner(rt)
        reg, _ = runner._build_registry(
            SubAgentSpec(name="g", task="t", tools=["t1", "t2"], disallowed_tools=["t*"])
        )
        assert reg.count == 0

    @pytest.mark.asyncio
    async def test_mcp_server_pattern_deny(self):
        tools = ToolRegistry()
        tools.register(T1Tool())
        tools.register(MCPStubTool())
        rt, _ = _make_parent([_llm_response(content="ok")], tools=tools)
        runner = SubAgentRunner(rt)
        spec = SubAgentSpec(
            name="m",
            task="t",
            tools=["t1", "mcp__docs__search"],
            disallowed_tools=["mcp__docs"],
        )
        reg, _ = runner._build_registry(spec)
        assert reg.has("mcp__docs__search") is False
        assert reg.has("t1") is True

    @pytest.mark.asyncio
    async def test_mcp_wildcard_pattern_deny(self):
        tools = ToolRegistry()
        tools.register(MCPStubTool())
        rt, _ = _make_parent([_llm_response(content="ok")], tools=tools)
        runner = SubAgentRunner(rt)
        reg, _ = runner._build_registry(
            SubAgentSpec(
                name="m2",
                task="t",
                tools=["mcp__docs__search"],
                disallowed_tools=["mcp__docs__*"],
            )
        )
        assert reg.count == 0

    @pytest.mark.asyncio
    async def test_run_with_deny_has_no_tool_schema(self):
        tools = ToolRegistry()
        tools.register(T1Tool())
        rt, _ = _make_parent([_llm_response(content="done")], tools=tools)
        runner = SubAgentRunner(rt)
        report = await runner.run(
            SubAgentSpec(
                name="deny-run", task="t", tools=["t1"], disallowed_tools=["t1"]
            )
        )
        assert report.ok is True
        assert runner.last_runtime.tools.count == 0


class TestPerSubagentCompactionAndEffort:
    @pytest.mark.asyncio
    async def test_auto_compact_window_applied(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        await runner.run(
            SubAgentSpec(name="small", task="t", auto_compact_window=2048)
        )
        compactor = runner.last_runtime.compactor
        assert compactor is not None
        assert compactor.config.context_window == 2048

    @pytest.mark.asyncio
    async def test_no_window_keeps_global_default(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        await runner.run(SubAgentSpec(name="plain", task="t"))
        compactor = runner.last_runtime.compactor
        assert compactor is None or compactor.config.context_window != 2048

    def test_effort_forwarded_to_llm_node(self):
        rt, _ = _make_parent([_llm_response(content="ok")])
        runner = SubAgentRunner(rt)
        graph = runner._build_graph(
            SubAgentSpec(name="e", task="t", effort="high"), "m1"
        )
        node = graph.nodes["llm1"]
        assert node.effort == "high"

    @pytest.mark.asyncio
    async def test_two_subagents_different_windows(self):
        rt, _ = _make_parent([_llm_response(content="a"), _llm_response(content="b")])
        runner_a = SubAgentRunner(rt)
        await runner_a.run(SubAgentSpec(name="a", task="t", auto_compact_window=1024))
        runner_b = SubAgentRunner(rt)
        await runner_b.run(SubAgentSpec(name="b", task="t", auto_compact_window=8192))
        assert runner_a.last_runtime.compactor.config.context_window == 1024
        assert runner_b.last_runtime.compactor.config.context_window == 8192


class TestMemoryScopes:
    def test_scope_keys(self):
        assert SubAgentRunner._memory_scope_key("user") == "user"
        assert SubAgentRunner._memory_scope_key("local").startswith("local:")
        assert SubAgentRunner._memory_scope_key("project", "/tmp/p1").startswith("project:/")
        assert SubAgentRunner._memory_scope_key("") == ""

    @pytest.mark.asyncio
    async def test_inject_and_commit_project_scope(self, tmp_path):
        engine = FakeMemoryEngine()
        rt, _ = _make_parent([_llm_response(content="learned outcome")])
        rt.memory_engine = engine
        runner = SubAgentRunner(rt)
        spec = SubAgentSpec(
            name="mem", task="do work", memory_scope="project", team=str(tmp_path)
        )
        report = await runner.run(spec)
        assert report.ok is True
        assert engine.recall_calls and engine.recall_calls[0]["scope"].startswith("project:")
        seeded = runner.last_ctx.messages[0]
        assert seeded["role"] == "system"
        assert "[Scoped memory project:" in seeded["content"]
        assert engine.store_calls
        assert engine.store_calls[0]["scope"].startswith("project:")
        assert engine.store_calls[0]["memory_type"] == "episodic"

    @pytest.mark.asyncio
    async def test_local_scope_isolated_from_project(self, tmp_path):
        engine = FakeMemoryEngine()
        rt, _ = _make_parent([_llm_response(content="x")])
        rt.memory_engine = engine
        runner = SubAgentRunner(rt)
        await runner.run(
            SubAgentSpec(name="loc", task="t", memory_scope="local", team=str(tmp_path))
        )
        assert engine.recall_calls[0]["scope"].startswith("local:")

    @pytest.mark.asyncio
    async def test_no_scope_no_memory_traffic(self):
        engine = FakeMemoryEngine()
        rt, _ = _make_parent([_llm_response(content="x")])
        rt.memory_engine = engine
        runner = SubAgentRunner(rt)
        await runner.run(SubAgentSpec(name="none", task="t"))
        assert engine.recall_calls == []
        assert engine.store_calls == []

    @pytest.mark.asyncio
    async def test_scope_without_engine_logs_and_continues(self):
        rt, _ = _make_parent([_llm_response(content="x")])
        runner = SubAgentRunner(rt)
        report = await runner.run(SubAgentSpec(name="noengine", task="t", memory_scope="user"))
        assert report.ok is True


class TestDefinitionRpcSurface:
    @pytest.mark.asyncio
    async def test_registry_list_payload_is_json_safe(self):
        registry = AgentDefinitionRegistry()
        registry.register(parse_agent_md(FRONTMATTER_MD, fallback_name="r"))
        payload = registry.list()
        assert payload[0]["name"] == "reviewer"
        json.dumps(payload)
