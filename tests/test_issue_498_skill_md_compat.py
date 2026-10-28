"""Tests for #498 AgentSkills SKILL.md compatibility.

Runner: pytest tests/test_issue_498_skill_md_compat.py
AC1: a stock AgentSkills SKILL.md (OpenHands repo) loads and triggers without
     modification — frontmatter triggers + keyword/path matching.
AC2: allowed-tools restriction enforced by the permission engine (role-scoped
     allow rules; non-allowed tools escalate to "ask" once armed).

User instruction: "修复fusion-agent-studio所有issue和pr，提交代码到代码仓，合并所有分支到主干，发布补丁版本"
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.context import AgentContext
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import AgentRuntime
from agent_runtime.skill_permissions import (
    permission_rules_for_skill,
    register_matched_skill_rules,
    register_skill_permission_rules,
    skill_role,
)
from agent_runtime.skills_store import SkillsStore, parse_skill_md
from tools import ToolRegistry
from tools.skill_tools import SkillInvokeTool, SkillSearchTool

# Stock OpenHands AgentSkills SKILL.md — untouched stock format: name,
# description, triggers (bare list = keywords), allowed-tools.
STOCK_SKILL_MD = """---
name: generate-pr-description
description: Generate a pull request description from a git diff
triggers:
  - generate pull request
  - pr description
allowed-tools:
  - file_read
  - terminal
---

# Generate PR description

Summarize the diff $ARGUMENTS into a PR description.
"""


def _store(tmp_path: Path) -> SkillsStore:
    return SkillsStore(
        skills_dir=str(tmp_path / "skills"),
        project_dir="",
        extra_dirs=[str(tmp_path / "plugins")],
    )


class TestTriggerParsing:
    def test_dict_trigger_shape(self):
        meta, _ = parse_skill_md(
            "---\nname: a\ntriggers:\n  keywords: [pdf, invoice]\n"
            "  paths: ['*.pdf']\n---\nbody"
        )
        assert meta.triggers == {"keywords": ["pdf", "invoice"], "paths": ["*.pdf"]}

    def test_bare_list_shape(self):
        # OpenHands stock shape: triggers as a plain list of keywords.
        meta, _ = parse_skill_md(
            "---\nname: a\ntriggers:\n  - generate pull request\n  - pr description\n---\nbody"
        )
        assert meta.triggers == {"keywords": ["generate pull request", "pr description"], "paths": []}

    def test_flat_keys_shape(self):
        meta, _ = parse_skill_md(
            "---\nname: a\nkeywords: [deploy]\npaths: ['**/deploy*']\n---\nbody"
        )
        assert meta.triggers == {"keywords": ["deploy"], "paths": ["**/deploy*"]}

    def test_string_trigger_shape(self):
        meta, _ = parse_skill_md("---\nname: a\ntriggers: summarize\n---\nbody")
        assert meta.triggers == {"keywords": ["summarize"], "paths": []}

    def test_no_triggers_stays_empty_and_out_of_extra(self):
        meta, _ = parse_skill_md("---\nname: a\ndescription: d\n---\nbody")
        assert meta.triggers == {}
        # trigger keys are consumed, never dumped into extra
        meta2, _ = parse_skill_md("---\nname: a\ntriggers: [x]\n---\nbody")
        assert "triggers" not in meta2.extra

    def test_to_dict_roundtrip_triggers(self):
        meta, _ = parse_skill_md("---\nname: a\nkeywords: [k]\n---\nbody")
        d = meta.to_dict()
        assert d["triggers"] == {"keywords": ["k"], "paths": []}


class TestMatching:
    def test_keyword_match_case_insensitive(self, tmp_path):
        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)
        hits = store.match_skills(text="Please generate pull request description for me")
        assert [h["name"] for h in hits] == ["generate-pr-description"]
        assert hits[0]["allowed_tools"] == ["file_read", "terminal"]

    def test_no_match(self, tmp_path):
        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)
        assert store.match_skills(text="what is the weather") == []

    def test_path_glob_match(self, tmp_path):
        store = _store(tmp_path)
        store.install_skill(
            "pdf-handler",
            "---\nname: pdf-handler\ntriggers:\n  paths: ['*.pdf', '**/invoices/*']\n---\nbody",
        )
        assert [h["name"] for h in store.match_skills(path="report.pdf")] == ["pdf-handler"]
        assert [h["name"] for h in store.match_skills(path="a/b/invoices/x.txt")] == ["pdf-handler"]
        assert store.match_skills(path="report.txt") == []


class TestPluginDirAcceptance:
    def test_skill_md_in_plugin_dir_discovered(self, tmp_path):
        # SKILL.md-format skill lives in the plugins dir alongside native
        # .py plugins — no modification needed to be discovered.
        plugin_dir = tmp_path / "plugins" / "gen-pr"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "SKILL.md").write_text(STOCK_SKILL_MD, encoding="utf-8")
        store = _store(tmp_path)
        names = [s["name"] for s in store.list_skills()]
        assert "generate-pr-description" in names
        # both the directory name and the frontmatter name resolve
        assert store.has_skill("gen-pr")
        assert store.has_skill("generate-pr-description")
        # and it triggers
        assert [h["name"] for h in store.match_skills(text="pr description please")] == ["generate-pr-description"]

    def test_with_project_dirs_includes_plugin_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plugin_dir = Path.home() / ".fusion-agent-studio" / "plugins" / "stock-skill-498"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "SKILL.md").write_text(STOCK_SKILL_MD, encoding="utf-8")
        try:
            store = SkillsStore.with_project_dirs(
                skills_dir=str(tmp_path / "skills"), project_root=str(tmp_path)
            )
            assert store.has_skill("stock-skill-498")
            assert store.has_skill("generate-pr-description")
        finally:
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)


class TestPermissionEnforcement:
    def test_rule_shape(self):
        rules = permission_rules_for_skill(
            {"name": "gen-pr", "allowed_tools": ["file_read", "terminal"]}
        )
        assert len(rules) == 2
        assert all(r.action == "allow" for r in rules)
        assert all(r.roles == [skill_role("gen-pr")] for r in rules)
        assert rules[0].matches("file_read")
        assert not rules[0].matches("file_write")

    def test_allowed_tool_allows_and_other_asks(self):
        # AC2: allowed tool evaluates allow under ctx.role="skill:<name>";
        # non-allowed tool escalates to ask once the engine is armed.
        engine = PermissionEngine()
        assert not engine.is_armed()
        register_skill_permission_rules(
            engine, {"name": "gen-pr", "allowed_tools": ["file_read", "terminal"]}
        )
        assert engine.is_armed()
        ctx = AgentContext(role=skill_role("gen-pr"))
        assert engine.evaluate("file_read", {}, ctx=ctx).decision == "allow"
        assert engine.evaluate("terminal", {"command": "ls"}, ctx=ctx).decision == "allow"
        assert engine.evaluate("file_write", {"path": "/tmp/x", "content": "y"}, ctx=ctx).decision == "ask"
        assert engine.evaluate("http_request", {"url": "https://example.com"}, ctx=ctx).decision == "ask"

    def test_role_mismatch_rules_skipped(self):
        engine = PermissionEngine()
        register_skill_permission_rules(
            engine, {"name": "gen-pr", "allowed_tools": ["file_read"]}
        )
        other = AgentContext(role="other-role")
        assert engine.evaluate("file_read", {}, ctx=other).decision == "ask"

    def test_skill_surface_tools_stay_allowed(self):
        # skill_search / skill_invoke / skill.invoke must not lock up once
        # skill rules arm the engine.
        engine = PermissionEngine()
        register_skill_permission_rules(engine, {"name": "gen-pr", "allowed_tools": []})
        ctx = AgentContext()
        for tool in ("skill_search", "skill_invoke", "skill.invoke"):
            assert engine.evaluate(tool, {}, ctx=ctx).decision == "allow", tool

    def test_registration_idempotent(self):
        engine = PermissionEngine()
        skill = {"name": "gen-pr", "allowed_tools": ["file_read"]}
        first = register_skill_permission_rules(engine, skill)
        second = register_skill_permission_rules(engine, skill)
        assert first > 0
        assert second == 0
        assert engine.rules_count() == first


class TestLlmSkillTools:
    def _providers(self, tmp_path):
        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)
        engine = PermissionEngine()
        return store, engine

    async def test_skill_search_matches_and_registers(self, tmp_path):
        store, engine = self._providers(tmp_path)
        tool = SkillSearchTool(lambda: store, lambda: engine)
        out = json.loads(await tool.execute(query="generate pull request description"))
        assert out["count"] == 1
        assert out["matched"][0]["name"] == "generate-pr-description"
        # rules registered as a side effect — role-scoped enforcement live
        ctx = AgentContext(role=skill_role("generate-pr-description"))
        assert engine.evaluate("file_read", {}, ctx=ctx).decision == "allow"
        assert engine.evaluate("file_write", {"path": "/tmp/x"}, ctx=ctx).decision == "ask"

    async def test_skill_invoke_renders_prompt(self, tmp_path):
        store, engine = self._providers(tmp_path)
        tool = SkillInvokeTool(lambda: store, lambda: engine)
        out = json.loads(await tool.execute(name="gen-pr", params={"diff": "abc"}))
        assert out["ok"] is True
        assert "Generate PR description" in out["prompt"]
        # invoke registers the skill's allowed-tools too
        ctx = AgentContext(role=skill_role("generate-pr-description"))
        assert engine.evaluate("terminal", {"command": "ls"}, ctx=ctx).decision == "allow"

    async def test_skill_invoke_missing_name(self, tmp_path):
        store, _ = self._providers(tmp_path)
        tool = SkillInvokeTool(lambda: store, None)
        out = json.loads(await tool.execute())
        assert "error" in out


class TestRuntimeWiring:
    def test_register_skill_tools_explicit(self):
        # daemon/api_server call _register_skill_tools(); subagent runtimes
        # with caller-filtered registries must stay untouched.
        rt = AgentRuntime(tool_registry=ToolRegistry())
        assert not rt.tools.has("skill_search")
        rt._register_skill_tools()
        assert rt.tools.has("skill_search")
        assert rt.tools.has("skill_invoke")
        schemas = rt.tools.to_openai_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert {"skill_search", "skill_invoke"} <= names

    def test_register_skill_tools_no_registry(self):
        rt = AgentRuntime()
        rt._register_skill_tools()
        assert rt.tools is None

    def test_runtime_skills_store_lazy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        rt = AgentRuntime(tool_registry=ToolRegistry())
        store = rt._get_skills_store()
        assert store is not None
        assert rt._get_skills_store() is store


class TestSkillsMatchRpc:
    async def test_match_registers_rules_and_returns_matched(self, tmp_path):
        from agent_runtime.dispatchers.skills import SkillsDispatcher

        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)
        rt = AgentRuntime(tool_registry=ToolRegistry())

        class FakeDaemon:
            _skills_store = store

            def _get_runtime(self):
                return rt

        d = SkillsDispatcher(FakeDaemon())
        res = await d._handle_match({"text": "please write a pr description"})
        assert res["status"] == "ok"
        assert res["count"] == 1
        assert res["matched"][0]["name"] == "generate-pr-description"
        # enforcement live on the runtime's engine
        ctx = AgentContext(role=skill_role("generate-pr-description"))
        assert rt.permission_engine.evaluate("file_read", {}, ctx=ctx).decision == "allow"
        assert rt.permission_engine.evaluate("file_write", {"path": "/tmp/x"}, ctx=ctx).decision == "ask"

    async def test_match_empty_text(self, tmp_path):
        from agent_runtime.dispatchers.skills import SkillsDispatcher

        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)

        class FakeDaemon:
            _skills_store = store

        d = SkillsDispatcher(FakeDaemon())
        res = await d._handle_match({})
        assert res == {"status": "ok", "matched": [], "count": 0}


class TestRegisterMatchedBundle:
    def test_register_matched_skills(self, tmp_path):
        store = _store(tmp_path)
        store.install_skill("gen-pr", STOCK_SKILL_MD)
        engine = PermissionEngine()
        matched = register_matched_skill_rules(engine, store, text="pr description")
        assert [m["name"] for m in matched] == ["generate-pr-description"]
        assert engine.is_armed()
