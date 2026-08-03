"""Tests for artifact FC tools — 5 BaseTool subclasses wrapping ArtifactManager.

Importers: pytest (test runner).
Affected API: ArtifactGetSourceTool, ArtifactCreateTool, ArtifactUpdateTool,
             ArtifactCreateSnapshotTool, ArtifactListAllTool.
Data schemas: ArtifactManager, ArtifactRecord (from artifact_tools).
User instruction: issue #60.
"""

import json
import tempfile

import pytest

from agent_runtime.artifact_tools import ArtifactManager
from tools.artifact_fc_tools import (
    ArtifactCreateSnapshotTool,
    ArtifactCreateTool,
    ArtifactGetSourceTool,
    ArtifactListAllTool,
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
