from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.skills_store import (
    SkillsStore,
    parse_skill_md,
    render_skill_prompt,
    validate_skill_params,
)

SKILL_MD = """---
name: summarize
description: Summarize a document
command: /sum
tools: file_read, knowledge_search
parameters:
  type: object
  properties:
    source:
      type: string
      description: File path or URL
    style:
      type: string
      enum: [brief, detailed]
      default: brief
    depth:
      type: integer
  required: [source]
output-template: |
  Return bullet points only.
---

Summarize {{source}} in {{style}} mode, depth {{depth}}.
"""


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


class TestSkillPackageParsing:
    def test_structured_frontmatter(self):
        meta, body = parse_skill_md(SKILL_MD, "fallback")
        assert meta.name == "summarize"
        assert meta.command == "/sum"
        assert meta.allowed_tools == ["file_read", "knowledge_search"]
        assert meta.parameters["required"] == ["source"]
        assert meta.output_template.strip() == "Return bullet points only."
        assert body.startswith("Summarize {{source}}")

    def test_command_gets_leading_slash(self):
        meta, _ = parse_skill_md("---\nname: x\ncommand: xrun\n---\nbody", "x")
        assert meta.command == "/xrun"

    def test_default_command_from_name(self):
        meta, _ = parse_skill_md("---\nname: plain\n---\nbody", "plain")
        assert meta.to_dict()["command"] == "/plain"


class TestSkillParamValidation:
    def test_required_missing(self):
        meta, _ = parse_skill_md(SKILL_MD, "summarize")
        cleaned, errors = validate_skill_params(meta, {"style": "brief"})
        assert "source is required" in errors
        assert cleaned["style"] == "brief"

    def test_type_coercion(self):
        meta, _ = parse_skill_md(SKILL_MD, "summarize")
        cleaned, errors = validate_skill_params(meta, {"source": "a.md", "depth": "3"})
        assert errors == []
        assert cleaned["depth"] == 3

    def test_bad_integer(self):
        meta, _ = parse_skill_md(SKILL_MD, "summarize")
        _, errors = validate_skill_params(meta, {"source": "a.md", "depth": "x"})
        assert errors == ["depth must be an integer"]

    def test_enum_rejected(self):
        meta, _ = parse_skill_md(SKILL_MD, "summarize")
        _, errors = validate_skill_params(meta, {"source": "a.md", "style": "loud"})
        assert errors and "style must be one of" in errors[0]

    def test_defaults_applied(self):
        meta, _ = parse_skill_md(SKILL_MD, "summarize")
        cleaned, errors = validate_skill_params(meta, {"source": "a.md"})
        assert errors == []
        assert cleaned["style"] == "brief"


class TestSkillRender:
    def test_placeholders_and_output_template(self):
        prompt = render_skill_prompt(
            "Do {{a}} and {b}. $ARGUMENTS",
            {"a": "x", "b": "y"},
            "Be terse.",
        )
        assert "Do x and y." in prompt
        assert "x y" in prompt
        assert prompt.endswith("## Output format\n\nBe terse.")

    def test_unknown_placeholder_kept(self):
        assert "{{missing}}" in render_skill_prompt("{{missing}}", {})


class TestSkillDiscovery:
    def _write(self, base, name, body):
        d = base / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def test_user_and_project_tiers(self, tmp_path):
        user = tmp_path / "user"
        project = tmp_path / "project"
        self._write(user, "alpha", "---\nname: alpha\ndescription: user\n---\nuser body")
        self._write(project, "beta", "---\nname: beta\ndescription: proj\n---\nproj body")
        self._write(project, "alpha", "---\nname: alpha\ndescription: override\n---\nproj body")
        store = SkillsStore(str(user), project_dir=str(project))
        names = [row["name"] for row in store.list_skills()]
        assert names == ["alpha", "beta"]
        alpha = store.load_skill("alpha")
        assert alpha["meta"]["description"] == "override"
        assert alpha["body"] == "proj body"
        assert alpha["meta"]["source"] == "project"

    def test_with_project_dirs_uses_dot_dirs(self, tmp_path, monkeypatch):
        # #498: with_project_dirs also scans the plugins dir by default —
        # pin it under tmp_path so the real ~/.fusion-agent-studio/plugins
        # can't leak into the assertion on dev machines.
        import agent_runtime.skills_store as ss

        monkeypatch.setattr(ss, "DEFAULT_PLUGIN_SKILLS_DIR", str(tmp_path / "plugins"))
        root = tmp_path / "repo"
        skills = root / ".claude" / "skills"
        self._write(skills, "compat", "---\nname: compat\n---\nbody")
        store = ss.SkillsStore.with_project_dirs(
            skills_dir=str(tmp_path / "user"), project_root=str(root)
        )
        assert [d.name for d in store.search_dirs() if d.exists()] == ["user", "skills"]
        assert store.has_skill("compat") is True
        assert [row["name"] for row in store.list_skills()] == ["compat"]

    def test_commands_rows(self, tmp_path):
        self._write(tmp_path / "user", "summarize", SKILL_MD)
        store = SkillsStore(str(tmp_path / "user"))
        rows = store.commands()
        assert rows[0]["command"] == "/sum"
        assert rows[0]["description"] == "Summarize a document"
        assert rows[0]["parameters"]["properties"]["source"]["type"] == "string"


class TestSkillInvoke:
    def test_invoke_success(self, tmp_path):
        d = tmp_path / "user" / "summarize"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        store = SkillsStore(str(tmp_path / "user"))
        result = store.invoke_skill("summarize", {"source": "doc.md", "depth": "2"})
        assert result["ok"] is True
        assert "Summarize doc.md in brief mode, depth 2." in result["prompt"]
        assert result["tools"] == ["file_read", "knowledge_search"]

    def test_invoke_validation_error(self, tmp_path):
        d = tmp_path / "user" / "summarize"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        store = SkillsStore(str(tmp_path / "user"))
        result = store.invoke_skill("summarize", {})
        assert result["ok"] is False
        assert "source is required" in result["errors"]

    def test_invoke_unknown(self, tmp_path):
        store = SkillsStore(str(tmp_path / "user"))
        assert store.invoke_skill("nope", {})["ok"] is False


class TestSkillRpcSurface:
    @pytest.mark.asyncio
    async def test_commands_and_invoke(self, daemon, tmp_path):
        daemon._skills_store = SkillsStore(str(tmp_path / "skills"))
        installed = await _run(
            daemon,
            "skills.install",
            {"name": "summarize", "content": SKILL_MD},
        )
        assert installed["status"] == "ok"

        listed = await _run(daemon, "skills.commands", {})
        assert listed["status"] == "ok"
        assert listed["commands"][0]["command"] == "/sum"

        invoked = await _run(
            daemon,
            "skills.invoke",
            {"name": "summarize", "params": {"source": "readme.md"}},
        )
        assert invoked["status"] == "ok"
        assert "Summarize readme.md" in invoked["prompt"]

        missing = await _run(daemon, "skills.invoke", {"name": "summarize", "params": {}})
        assert missing["status"] == "error"
        assert "source is required" in missing["errors"]

        unknown = await _run(daemon, "skills.invoke", {"name": "nope", "params": {}})
        assert unknown["status"] == "error"
