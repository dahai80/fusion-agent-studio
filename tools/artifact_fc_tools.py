"""Artifact FC tools — 5 BaseTool subclasses wrapping ArtifactManager.

Importers: tools/__init__.py (create_default_registry),
           agent_runtime/runtime.py (tool execution),
           agent_runtime/dispatchers/agent.py (RPC dispatch).
Affected API: artifact_get_source, artifact_create, artifact_update,
             artifact_create_snapshot, artifact_list_all tool schemas.
Data schemas: ArtifactRecord, ArtifactManager (from artifact_tools).
User instruction: issue #60 — implement artifact tools + context injection.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.base import BaseTool

logger = logging.getLogger(__name__)


class ArtifactGetSourceTool(BaseTool):
    name = "artifact_get_source"
    description = "Load an artifact's content by ID. Returns the full content, version, and metadata."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to load.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        result = self._manager.get_artifact(artifact_id)
        if result.get("status") == "error":
            return result.get("message", "Artifact not found")
        import json
        record = result["record"]
        logger.info("artifact_get_source: loaded %s v%d", artifact_id, record.get("version", 1))
        return json.dumps(record, ensure_ascii=False)


class ArtifactCreateTool(BaseTool):
    name = "artifact_create"
    description = "Create a new artifact with name, type, and content. Returns the artifact ID and record."
    parameters = {
        "agent_id": {
            "type": "string",
            "description": "Owner agent ID.",
        },
        "name": {
            "type": "string",
            "description": "Artifact name.",
        },
        "artifact_type": {
            "type": "string",
            "description": "Artifact type: document, code, data, image, config, report. Default: document.",
        },
        "content": {
            "type": "string",
            "description": "Artifact content text.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        agent_id = kwargs.get("agent_id", "")
        name = kwargs.get("name", "")
        content = kwargs.get("content", "")
        artifact_type = kwargs.get("artifact_type", "document")
        if not name:
            return "Error: name is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        result = self._manager.create_artifact(
            agent_id=agent_id,
            name=name,
            artifact_type=artifact_type,
            content=content,
        )
        logger.info("artifact_create: %s status=%s", name, result.get("status"))
        return json.dumps(result, ensure_ascii=False)


class ArtifactUpdateTool(BaseTool):
    name = "artifact_update"
    description = "Update an existing artifact's content or metadata. Increments version."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to update.",
        },
        "agent_id": {
            "type": "string",
            "description": "Agent ID performing the update (for policy check).",
        },
        "content": {
            "type": "string",
            "description": "New content text (optional, omit to keep existing).",
        },
        "metadata": {
            "type": "object",
            "description": "Metadata fields to merge (optional).",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        agent_id = kwargs.get("agent_id", "")
        content = kwargs.get("content")
        metadata = kwargs.get("metadata")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        result = self._manager.update_artifact(
            artifact_id=artifact_id,
            agent_id=agent_id,
            content=content,
            metadata=metadata,
        )
        logger.info("artifact_update: %s status=%s", artifact_id, result.get("status"))
        return json.dumps(result, ensure_ascii=False)


class ArtifactCreateSnapshotTool(BaseTool):
    name = "artifact_create_snapshot"
    description = "Create a named snapshot of an artifact at its current version."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to snapshot.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        load_result = self._manager.get_artifact(artifact_id)
        if load_result.get("status") == "error":
            return load_result.get("message", "Artifact not found")
        record = load_result["record"]
        export_result = self._manager.export_artifact(artifact_id)
        logger.info(
            "artifact_create_snapshot: %s v%d export=%s",
            artifact_id, record.get("version", 1), export_result.get("status"),
        )
        snapshot_info = {
            "artifact_id": artifact_id,
            "version": record.get("version", 1),
            "name": record.get("name", ""),
            "export_path": export_result.get("path", ""),
            "snapshot_status": export_result.get("status", "unknown"),
        }
        return json.dumps(snapshot_info, ensure_ascii=False)


class ArtifactListAllTool(BaseTool):
    name = "artifact_list_all"
    description = "List all artifacts, optionally filtered by owner agent ID."
    parameters = {
        "agent_id": {
            "type": "string",
            "description": "Filter by owner agent ID (optional, empty for all).",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        agent_id = kwargs.get("agent_id", "")
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        artifacts = self._manager.list_artifacts(agent_id=agent_id)
        logger.info("artifact_list_all: returned %d artifacts", len(artifacts))
        return json.dumps({"artifacts": artifacts, "total": len(artifacts)}, ensure_ascii=False)
