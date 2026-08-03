"""Tests for artifact FC tools — 8 BaseTool subclasses wrapping ArtifactManager.

Importers: pytest (test runner).
Affected API: ArtifactGetSourceTool, ArtifactCreateTool, ArtifactUpdateTool,
             ArtifactCreateSnapshotTool, ArtifactListAllTool,
             ArtifactPatchTool, ArtifactLoadTool, ArtifactContextBudgetTool.
Data schemas: ArtifactManager, ArtifactRecord (from artifact_tools).
Issue #62 — AS-1~8: load with preview/section, auto-trigger, patch ops,
             pagination, budget-aware context, compaction, system prompt.
"""

import json
import tempfile

import pytest

from agent_runtime.artifact_tools import ArtifactManager
from tools.artifact_fc_tools import (
    ArtifactContextBudgetTool,
    ArtifactCreateSnapshotTool,
    ArtifactCreateTool,
    ArtifactGetSourceTool,
    ArtifactListAllTool,
    ArtifactLoadTool,
    ArtifactPatchTool,
    ArtifactUpdateTool,
)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ArtifactManager(artifacts_dir=tmp)
        mgr.set_policy(
            "agent_1",
            type(
                "P",
                (),
                {
                    "creation_triggers": ["create"],
                    "update_triggers": ["update"],
                },
            )(),
        )
        yield mgr


@pytest.fixture
def tools(manager):
    return {
        "get_source": ArtifactGetSourceTool(manager),
        "create": ArtifactCreateTool(manager),
        "update": ArtifactUpdateTool(manager),
        "snapshot": ArtifactCreateSnapshotTool(manager),
        "list_all": ArtifactListAllTool(manager),
        "patch": ArtifactPatchTool(manager),
        "load": ArtifactLoadTool(manager),
        "budget": ArtifactContextBudgetTool(manager),
    }


class TestArtifactGetSourceTool:
    async def test_get_existing(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc1", "document", "hello")
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid)
        data = json.loads(out)
        assert data["artifact_id"] == aid
        assert data["content"] == "hello"

    async def test_get_missing(self, tools):
        out = await tools["get_source"].execute(artifact_id="nonexistent")
        assert "not found" in out.lower()

    async def test_get_no_id(self, tools):
        out = await tools["get_source"].execute()
        assert "required" in out.lower()


class TestArtifactCreateTool:
    async def test_create_success(self, tools):
        out = await tools["create"].execute(
            agent_id="agent_1", name="new_doc", content="test content"
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["artifact_id"]

    async def test_create_no_name(self, tools):
        out = await tools["create"].execute(agent_id="agent_1", content="x")
        assert "required" in out.lower()

    async def test_create_invalid_type(self, tools):
        out = await tools["create"].execute(
            agent_id="agent_1", name="bad", artifact_type="invalid", content="x"
        )
        data = json.loads(out)
        assert data["status"] == "error"


class TestArtifactUpdateTool:
    async def test_update_content(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc1", "document", "original")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid, agent_id="agent_1", content="updated"
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["record"]["content"] == "updated"
        assert data["record"]["version"] == 2

    async def test_update_missing(self, tools):
        out = await tools["update"].execute(artifact_id="nope", agent_id="a")
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_update_no_id(self, tools):
        out = await tools["update"].execute()
        assert "required" in out.lower()


class TestArtifactCreateSnapshotTool:
    async def test_snapshot_existing(self, tools, manager):
        result = manager.create_artifact(
            "agent_1", "snap_doc", "document", "snap content"
        )
        aid = result["artifact_id"]
        out = await tools["snapshot"].execute(artifact_id=aid)
        data = json.loads(out)
        assert data["artifact_id"] == aid
        assert data["snapshot_status"] == "ok"

    async def test_snapshot_missing(self, tools):
        out = await tools["snapshot"].execute(artifact_id="nope")
        assert "not found" in out.lower()


class TestArtifactListAllTool:
    async def test_list_empty(self, tools):
        out = await tools["list_all"].execute()
        data = json.loads(out)
        assert data["total"] == 0

    async def test_list_with_artifacts(self, tools, manager):
        manager.create_artifact("agent_1", "d1", "document", "c1")
        manager.create_artifact("agent_1", "d2", "code", "c2")
        out = await tools["list_all"].execute(agent_id="agent_1")
        data = json.loads(out)
        assert data["total"] == 2

    async def test_list_no_manager(self):
        tool = ArtifactListAllTool()
        out = await tool.execute()
        assert "not available" in out.lower()


class TestToolNoManager:
    async def test_get_source_no_manager(self):
        tool = ArtifactGetSourceTool()
        out = await tool.execute(artifact_id="x")
        assert "not available" in out.lower()

    async def test_create_no_manager(self):
        tool = ArtifactCreateTool()
        out = await tool.execute(agent_id="a", name="n")
        assert "not available" in out.lower()

    async def test_update_no_manager(self):
        tool = ArtifactUpdateTool()
        out = await tool.execute(artifact_id="x", agent_id="a")
        assert "not available" in out.lower()


class TestArtifactPatchTool:
    async def test_patch_replace(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc1", "document", "original")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="replace", content="replaced", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["record"]["content"] == "replaced"
        assert data["record"]["version"] == 2

    async def test_patch_append(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc2", "document", "hello")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="append", content=" world", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["record"]["content"] == "hello world"

    async def test_patch_prepend(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc3", "document", "world")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="prepend", content="hello ", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["record"]["content"] == "hello world"

    async def test_patch_section_replace(self, tools, manager):
        content = "header\n<!-- section:body -->\nold\n<!-- end:body -->\nfooter"
        result = manager.create_artifact("agent_1", "doc4", "document", content)
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="section_replace",
            content="new",
            section="body",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "new" in data["record"]["content"]
        assert "footer" in data["record"]["content"]

    async def test_patch_section_replace_new_section(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc5", "document", "base")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="section_replace",
            content="added",
            section="notes",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "added" in data["record"]["content"]

    async def test_patch_section_replace_no_section(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc6", "document", "base")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="section_replace",
            content="x",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_patch_invalid_op(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc7", "document", "base")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="delete", content="x", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_patch_missing_artifact(self, tools):
        out = await tools["patch"].execute(
            artifact_id="nope", operation="replace", content="x"
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_patch_no_artifact_id(self, tools):
        out = await tools["patch"].execute(operation="replace", content="x")
        assert "required" in out.lower()

    async def test_patch_no_operation(self, tools):
        out = await tools["patch"].execute(artifact_id="x", content="x")
        assert "required" in out.lower()

    async def test_patch_no_manager(self):
        tool = ArtifactPatchTool()
        out = await tool.execute(artifact_id="x", operation="replace", content="x")
        assert "not available" in out.lower()


class TestArtifactLoadTool:
    async def test_load_full(self, tools, manager):
        result = manager.create_artifact(
            "agent_1", "doc1", "document", "full content here"
        )
        aid = result["artifact_id"]
        out = await tools["load"].execute(artifact_id=aid)
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["content"] == "full content here"
        assert data["version"] == 1

    async def test_load_preview(self, tools, manager):
        long_content = "x" * 1000
        result = manager.create_artifact("agent_1", "doc2", "document", long_content)
        aid = result["artifact_id"]
        out = await tools["load"].execute(artifact_id=aid, preview_only=True)
        data = json.loads(out)
        assert len(data["content"]) == 500

    async def test_load_section(self, tools, manager):
        content = (
            "header\n<!-- section:body -->\nsection content\n<!-- end:body -->\nfooter"
        )
        result = manager.create_artifact("agent_1", "doc3", "document", content)
        aid = result["artifact_id"]
        out = await tools["load"].execute(artifact_id=aid, section="body")
        data = json.loads(out)
        assert "section content" in data["content"]

    async def test_load_max_tokens(self, tools, manager):
        long_content = "a" * 4000
        result = manager.create_artifact("agent_1", "doc4", "document", long_content)
        aid = result["artifact_id"]
        out = await tools["load"].execute(artifact_id=aid, max_tokens=10)
        data = json.loads(out)
        assert len(data["content"]) <= 40

    async def test_load_missing_section(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc5", "document", "no sections")
        aid = result["artifact_id"]
        out = await tools["load"].execute(artifact_id=aid, section="missing")
        data = json.loads(out)
        assert data["content"] == ""

    async def test_load_missing_artifact(self, tools):
        out = await tools["load"].execute(artifact_id="nope")
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_load_no_id(self, tools):
        out = await tools["load"].execute()
        assert "required" in out.lower()

    async def test_load_no_manager(self):
        tool = ArtifactLoadTool()
        out = await tool.execute(artifact_id="x")
        assert "not available" in out.lower()


class TestArtifactContextBudgetTool:
    async def test_budget_empty(self, tools):
        out = await tools["budget"].execute()
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["total_tokens"] == 0
        assert data["artifact_count"] == 0

    async def test_budget_with_artifacts(self, tools, manager):
        manager.create_artifact("agent_1", "d1", "document", "x" * 100)
        manager.create_artifact("agent_1", "d2", "code", "y" * 200)
        out = await tools["budget"].execute(agent_id="agent_1")
        data = json.loads(out)
        assert data["artifact_count"] == 2
        assert data["total_tokens"] > 0
        assert "document" in data["by_type"]
        assert "code" in data["by_type"]

    async def test_budget_no_manager(self):
        tool = ArtifactContextBudgetTool()
        out = await tool.execute()
        assert "not available" in out.lower()


class TestArtifactManagerNewMethods:
    async def test_patch_artifact_replace(self, manager):
        result = manager.create_artifact("agent_1", "p1", "document", "old")
        aid = result["artifact_id"]
        patched = manager.patch_artifact(aid, "replace", "new", agent_id="agent_1")
        assert patched["status"] == "ok"
        assert patched["record"]["content"] == "new"

    async def test_patch_artifact_append(self, manager):
        result = manager.create_artifact("agent_1", "p2", "document", "hello")
        aid = result["artifact_id"]
        patched = manager.patch_artifact(aid, "append", " world", agent_id="agent_1")
        assert patched["record"]["content"] == "hello world"

    async def test_load_artifact_preview(self, manager):
        result = manager.create_artifact("agent_1", "l1", "document", "x" * 1000)
        aid = result["artifact_id"]
        loaded = manager.load_artifact(aid, preview_only=True)
        assert loaded["status"] == "ok"
        assert len(loaded["content"]) == 500

    async def test_load_artifact_section(self, manager):
        content = "h\n<!-- section:main -->\nbody\n<!-- end:main -->\nf"
        result = manager.create_artifact("agent_1", "l2", "document", content)
        aid = result["artifact_id"]
        loaded = manager.load_artifact(aid, section="main")
        assert "body" in loaded["content"]

    async def test_get_context_budget(self, manager):
        manager.create_artifact("agent_1", "b1", "document", "x" * 100)
        budget = manager.get_context_budget("agent_1")
        assert budget["status"] == "ok"
        assert budget["artifact_count"] == 1
        assert budget["total_tokens"] > 0


class TestAS1ArtifactGetSourceWithLoad:
    async def test_get_source_with_preview(self, tools, manager):
        long_content = "x" * 1000
        result = manager.create_artifact("agent_1", "src1", "document", long_content)
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid, preview_only=True)
        data = json.loads(out)
        assert data["status"] == "ok"
        assert len(data["content"]) == 500
        assert "sections" in data
        assert "summary" in data
        assert "token_count" in data

    async def test_get_source_with_section(self, tools, manager):
        content = "header\n<!-- section:body -->\nbody text\n<!-- end:body -->\nfooter"
        result = manager.create_artifact("agent_1", "src2", "document", content)
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid, section="body")
        data = json.loads(out)
        assert "body text" in data["content"]
        assert "sections" in data
        assert "body" in data["sections"]

    async def test_get_source_returns_sections_and_summary(self, tools, manager):
        content = "My Doc\n<!-- section:intro -->\nintro\n<!-- end:intro -->\n<!-- section:body -->\nbody\n<!-- end:body -->"
        result = manager.create_artifact("agent_1", "src3", "document", content)
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid)
        data = json.loads(out)
        assert "intro" in data["sections"]
        assert "body" in data["sections"]
        assert data["summary"]


class TestAS2ArtifactCreateAutoTrigger:
    async def test_auto_trigger_creates_when_above_threshold(self, tools, manager):
        long_content = "\n".join([f"line {i}" for i in range(35)])
        out = await tools["create"].execute(
            agent_id="agent_1",
            name="auto_doc",
            content=long_content,
            auto_trigger=True,
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["artifact_id"]

    async def test_auto_trigger_skips_when_below_threshold(self, tools, manager):
        short_content = "just a short text"
        out = await tools["create"].execute(
            agent_id="agent_1",
            name="skip_doc",
            content=short_content,
            auto_trigger=True,
        )
        data = json.loads(out)
        assert data["status"] == "skipped"

    async def test_auto_trigger_skips_by_lines(self, tools, manager):
        content = "\n".join(["short"] * 10)
        out = await tools["create"].execute(
            agent_id="agent_1",
            name="skip_lines",
            content=content,
            auto_trigger=True,
        )
        data = json.loads(out)
        assert data["status"] == "skipped"

    async def test_auto_trigger_by_chars(self, tools, manager):
        content = "x" * 2000
        out = await tools["create"].execute(
            agent_id="agent_1",
            name="char_trigger",
            content=content,
            auto_trigger=True,
        )
        data = json.loads(out)
        assert data["status"] == "ok"

    async def test_normal_create_ignores_threshold(self, tools, manager):
        short_content = "short"
        out = await tools["create"].execute(
            agent_id="agent_1",
            name="normal_doc",
            content=short_content,
        )
        data = json.loads(out)
        assert data["status"] == "ok"


class TestAS3ArtifactUpdateWithPatch:
    async def test_update_with_replace_section(self, tools, manager):
        content = "header\n<!-- section:body -->\nold\n<!-- end:body -->\nfooter"
        result = manager.create_artifact("agent_1", "u1", "document", content)
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content="new",
            operation="replace_section",
            anchor="body",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "new" in data["record"]["content"]
        assert "footer" in data["record"]["content"]
        assert "old" not in data["record"]["content"]

    async def test_update_with_append(self, tools, manager):
        result = manager.create_artifact("agent_1", "u2", "document", "hello")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content=" world",
            operation="append",
        )
        data = json.loads(out)
        assert data["record"]["content"] == "hello world"

    async def test_update_with_prepend(self, tools, manager):
        result = manager.create_artifact("agent_1", "u3", "document", "world")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content="hello ",
            operation="prepend",
        )
        data = json.loads(out)
        assert data["record"]["content"] == "hello world"

    async def test_update_with_delete_section(self, tools, manager):
        content = "header\n<!-- section:body -->\nto delete\n<!-- end:body -->\nfooter"
        result = manager.create_artifact("agent_1", "u4", "document", content)
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            operation="delete_section",
            anchor="body",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "to delete" not in data["record"]["content"]
        assert "footer" in data["record"]["content"]

    async def test_update_delete_section_missing_anchor(self, tools, manager):
        result = manager.create_artifact("agent_1", "u5", "document", "no sections")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            operation="delete_section",
            anchor="missing",
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_update_replace_section_missing_anchor(self, tools, manager):
        result = manager.create_artifact("agent_1", "u6", "document", "no sections")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content="x",
            operation="replace_section",
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_update_invalid_operation(self, tools, manager):
        result = manager.create_artifact("agent_1", "u7", "document", "base")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content="x",
            operation="invalid_op",
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_update_full_still_works(self, tools, manager):
        result = manager.create_artifact("agent_1", "u8", "document", "original")
        aid = result["artifact_id"]
        out = await tools["update"].execute(
            artifact_id=aid,
            agent_id="agent_1",
            content="replaced",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert data["record"]["content"] == "replaced"


class TestAS5ArtifactListAllPaginated:
    async def test_pagination_basic(self, tools, manager):
        for i in range(5):
            manager.create_artifact("agent_1", f"p{i}", "document", f"content {i}")
        out = await tools["list_all"].execute(agent_id="agent_1", page=1, limit=2)
        data = json.loads(out)
        assert data["status"] == "ok"
        assert len(data["artifacts"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["total_pages"] == 3

    async def test_pagination_page2(self, tools, manager):
        for i in range(5):
            manager.create_artifact("agent_1", f"pp{i}", "document", f"content {i}")
        out = await tools["list_all"].execute(agent_id="agent_1", page=2, limit=2)
        data = json.loads(out)
        assert len(data["artifacts"]) == 2
        assert data["page"] == 2

    async def test_pagination_filter_by_type(self, tools, manager):
        manager.create_artifact("agent_1", "d1", "document", "doc")
        manager.create_artifact("agent_1", "c1", "code", "code")
        out = await tools["list_all"].execute(agent_id="agent_1", artifact_type="code")
        data = json.loads(out)
        assert data["total"] == 1
        assert data["artifacts"][0]["artifact_type"] == "code"

    async def test_pagination_empty(self, tools):
        out = await tools["list_all"].execute()
        data = json.loads(out)
        assert data["total"] == 0
        assert data["artifacts"] == []


class TestAS6BudgetAwareContext:
    async def test_full_mode_under_70pct(self, manager):
        manager.create_artifact("agent_1", "small", "document", "short")
        result = manager.get_active_artifacts_context_budget_aware(
            "agent_1",
            context_window=32768,
        )
        assert result["mode"] == "full"
        assert "[Active Artifacts (full)]" in result["context_text"]
        assert "short" in result["context_text"]

    async def test_preview_mode_70_90pct(self, manager):
        big_content = "x" * 24000
        manager.create_artifact("agent_1", "big", "document", big_content)
        result = manager.get_active_artifacts_context_budget_aware(
            "agent_1",
            context_window=8000,
        )
        assert result["mode"] == "preview"
        assert "preview" in result["context_text"]

    async def test_blocked_mode_over_90pct(self, manager):
        huge_content = "y" * 32000
        manager.create_artifact("agent_1", "huge", "document", huge_content)
        result = manager.get_active_artifacts_context_budget_aware(
            "agent_1",
            context_window=8000,
        )
        assert result["mode"] == "blocked"
        assert "BLOCKED" in result["context_text"]

    async def test_no_artifacts(self, manager):
        result = manager.get_active_artifacts_context_budget_aware(
            "agent_1",
            context_window=32768,
        )
        assert result["mode"] == "none"
        assert result["context_text"] == ""

    async def test_sections_in_preview(self, manager):
        content = "doc\n<!-- section:intro -->\nintro\n<!-- end:intro -->\n<!-- section:body -->\nbody\n<!-- end:body -->"
        manager.create_artifact("agent_1", "sec_doc", "document", content)
        result = manager.get_active_artifacts_context_budget_aware(
            "agent_1",
            context_window=100,
        )
        if result["mode"] in ("preview", "blocked"):
            assert (
                "sections" in result["context_text"]
                or "BLOCKED" in result["context_text"]
            )


class TestAS6ArtifactGetSourceToolParams:
    async def test_get_source_preview_only(self, tools, manager):
        long_content = "a" * 2000
        result = manager.create_artifact("agent_1", "gsp", "document", long_content)
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid, preview_only=True)
        data = json.loads(out)
        assert len(data["content"]) == 500

    async def test_get_source_section(self, tools, manager):
        content = "h\n<!-- section:data -->\nthe data\n<!-- end:data -->\nf"
        result = manager.create_artifact("agent_1", "gss", "document", content)
        aid = result["artifact_id"]
        out = await tools["get_source"].execute(artifact_id=aid, section="data")
        data = json.loads(out)
        assert "the data" in data["content"]


class TestPatchArtifactDeleteSection:
    async def test_delete_section(self, tools, manager):
        content = "h\n<!-- section:del -->\nremove me\n<!-- end:del -->\nf"
        result = manager.create_artifact("agent_1", "ds1", "document", content)
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="delete_section",
            section="del",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "remove me" not in data["record"]["content"]
        assert "f" in data["record"]["content"]

    async def test_delete_section_missing(self, tools, manager):
        result = manager.create_artifact("agent_1", "ds2", "document", "plain")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="delete_section",
            section="missing",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "error"

    async def test_replace_section_alias(self, tools, manager):
        content = "h\n<!-- section:main -->\nold\n<!-- end:main -->\nf"
        result = manager.create_artifact("agent_1", "rsa", "document", content)
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid,
            operation="replace_section",
            content="new",
            section="main",
            agent_id="agent_1",
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "new" in data["record"]["content"]
        assert "old" not in data["record"]["content"]


class TestArtifactRecordSections:
    def test_sections_extraction(self, manager):
        content = "h\n<!-- section:intro -->\ni\n<!-- end:intro -->\n<!-- section:body -->\nb\n<!-- end:body -->"
        result = manager.create_artifact("agent_1", "sec", "document", content)
        aid = result["artifact_id"]
        rec = manager._artifacts[aid]
        assert "intro" in rec.sections()
        assert "body" in rec.sections()

    def test_auto_summary(self, manager):
        result = manager.create_artifact(
            "agent_1", "sum", "document", "My Document Title\ncontent"
        )
        aid = result["artifact_id"]
        rec = manager._artifacts[aid]
        assert "My Document Title" in rec.auto_summary()

    def test_auto_summary_explicit(self, manager):
        result = manager.create_artifact("agent_1", "esum", "document", "content")
        aid = result["artifact_id"]
        rec = manager._artifacts[aid]
        rec.summary = "Custom summary"
        assert rec.auto_summary() == "Custom summary"
