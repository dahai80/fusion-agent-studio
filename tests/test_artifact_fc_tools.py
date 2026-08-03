"""Tests for artifact FC tools — 8 BaseTool subclasses wrapping ArtifactManager.

Importers: pytest (test runner).
Affected API: ArtifactGetSourceTool, ArtifactCreateTool, ArtifactUpdateTool,
             ArtifactCreateSnapshotTool, ArtifactListAllTool,
             ArtifactPatchTool, ArtifactLoadTool, ArtifactContextBudgetTool.
Data schemas: ArtifactManager, ArtifactRecord (from artifact_tools).
User instruction: issue #61.
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
        mgr.set_policy("agent_1", type("P", (), {
            "creation_triggers": ["create"],
            "update_triggers": ["update"],
        })())
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
        result = manager.create_artifact("agent_1", "snap_doc", "document", "snap content")
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
            artifact_id=aid, operation="section_replace",
            content="new", section="body", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "new" in data["record"]["content"]
        assert "footer" in data["record"]["content"]

    async def test_patch_section_replace_new_section(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc5", "document", "base")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="section_replace",
            content="added", section="notes", agent_id="agent_1"
        )
        data = json.loads(out)
        assert data["status"] == "ok"
        assert "added" in data["record"]["content"]

    async def test_patch_section_replace_no_section(self, tools, manager):
        result = manager.create_artifact("agent_1", "doc6", "document", "base")
        aid = result["artifact_id"]
        out = await tools["patch"].execute(
            artifact_id=aid, operation="section_replace", content="x", agent_id="agent_1"
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
        result = manager.create_artifact("agent_1", "doc1", "document", "full content here")
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
        content = "header\n<!-- section:body -->\nsection content\n<!-- end:body -->\nfooter"
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
