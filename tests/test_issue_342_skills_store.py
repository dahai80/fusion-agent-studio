"""N5 Skills store - Anthropic SKILL.md format + progressive disclosure (issue #342)."""

import pytest

from agent_runtime.skills_store import SkillsStore, parse_skill_md

SAMPLE = """---
name: pdf-extract
description: Extract structured data from PDFs
allowed-tools:
  - file_read
  - artifact_create
version: "1.0"
---

# PDF extract

Step 1: read the file.
"""


def test_parse_frontmatter_and_body():
    meta, body = parse_skill_md(SAMPLE, "fallback")
    assert meta.name == "pdf-extract"
    assert meta.description == "Extract structured data from PDFs"
    assert meta.allowed_tools == ["file_read", "artifact_create"]
    assert meta.version == "1.0"
    assert "# PDF extract" in body
    assert "frontmatter" not in body


def test_parse_no_frontmatter():
    meta, body = parse_skill_md("just body text", "plain")
    assert meta.name == "plain"
    assert body == "just body text"


def test_store_install_list_load_delete(tmp_path):
    store = SkillsStore(str(tmp_path / "skills"))
    out = store.install_skill("pdf-extract", SAMPLE)
    assert out["name"] == "pdf-extract"
    listed = store.list_skills()
    assert len(listed) == 1
    # Progressive disclosure: metadata only, no body in list.
    assert "body" not in listed[0]
    assert listed[0]["description"] == "Extract structured data from PDFs"
    loaded = store.load_skill("pdf-extract")
    assert loaded is not None
    assert "# PDF extract" in loaded["body"]
    assert loaded["meta"]["name"] == "pdf-extract"
    assert store.uninstall_skill("pdf-extract") is True
    assert store.list_skills() == []
    assert store.load_skill("pdf-extract") is None


def test_store_rejects_path_traversal(tmp_path):
    store = SkillsStore(str(tmp_path / "skills"))
    assert store.has_skill("../etc") is False
    assert store.load_skill("../etc") is None
    assert store.uninstall_skill("../etc") is False
    with pytest.raises(ValueError):
        store.install_skill("../evil", SAMPLE)


def test_store_load_missing(tmp_path):
    store = SkillsStore(str(tmp_path / "skills"))
    assert store.load_skill("nope") is None


def test_anthropic_style_comma_allowed_tools(tmp_path):
    store = SkillsStore(str(tmp_path / "skills"))
    store.install_skill("comma", """---
name: comma
description: d
allowed-tools: file_read, file_write
---

body
""")
    listed = store.list_skills()
    assert listed[0]["allowed_tools"] == ["file_read", "file_write"]


class _StubDaemon:
    pass


async def test_skills_dispatcher_roundtrip(tmp_path):
    from agent_runtime.dispatchers.skills import SkillsDispatcher

    d = _StubDaemon()
    disp = SkillsDispatcher(d)

    disp._store = lambda: SkillsStore(str(tmp_path / "skills"))
    r = await disp._handle_install({"name": "pdf-extract", "content": SAMPLE})
    assert r["status"] == "ok"
    r = await disp._handle_list({})
    assert r["status"] == "ok" and len(r["skills"]) == 1
    r = await disp._handle_load({"name": "pdf-extract"})
    assert r["status"] == "ok" and "# PDF extract" in r["skill"]["body"]
    r = await disp._handle_load({"name": "missing"})
    assert r["status"] == "error"
    r = await disp._handle_delete({"name": "pdf-extract"})
    assert r["status"] == "ok"
    r = await disp._handle_delete({"name": "pdf-extract"})
    assert r["status"] == "error"
    r = await disp._handle_install({"name": "", "content": SAMPLE})
    assert r["status"] == "error"
