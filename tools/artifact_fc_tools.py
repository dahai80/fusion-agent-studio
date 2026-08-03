"""Artifact FC tools — 8 BaseTool subclasses wrapping ArtifactManager.

Importers: tools/__init__.py (create_default_registry),
           agent_runtime/runtime.py (tool execution),
           agent_runtime/dispatchers/agent.py (RPC dispatch).
Affected API: artifact_get_source, artifact_create, artifact_update,
             artifact_create_snapshot, artifact_list_all,
             artifact_patch, artifact_load, artifact_context_budget.
Data schemas: ArtifactRecord, ArtifactManager (from artifact_tools).
User instruction: issue #61 — patch_artifact / load_artifact / context_budget.
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


class ArtifactPatchTool(BaseTool):
    name = "artifact_patch"
    description = "Incrementally patch an artifact's content. Supports 4 operations: replace, append, prepend, section_replace."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to patch.",
        },
        "operation": {
            "type": "string",
            "description": "Patch operation: replace, append, prepend, section_replace.",
        },
        "content": {
            "type": "string",
            "description": "Content to apply in the patch.",
        },
        "section": {
            "type": "string",
            "description": "Section name for section_replace operation (optional).",
        },
        "agent_id": {
            "type": "string",
            "description": "Agent ID performing the patch (for policy check).",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        operation = kwargs.get("operation", "")
        content = kwargs.get("content", "")
        section = kwargs.get("section", "")
        agent_id = kwargs.get("agent_id", "")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not operation:
            return "Error: operation is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        result = self._manager.patch_artifact(
            artifact_id=artifact_id,
            operation=operation,
            content=content,
            section=section,
            agent_id=agent_id,
        )
        logger.info("artifact_patch: %s op=%s status=%s", artifact_id, operation, result.get("status"))
        return json.dumps(result, ensure_ascii=False)


class ArtifactLoadTool(BaseTool):
    name = "artifact_load"
    description = "Load artifact content with optional preview_only or section-based loading for efficient context usage."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to load.",
        },
        "preview_only": {
            "type": "boolean",
            "description": "If true, return only first 500 chars. Default: false.",
        },
        "section": {
            "type": "string",
            "description": "Load only a specific section by name (optional).",
        },
        "max_tokens": {
            "type": "integer",
            "description": "Max tokens to return (0 = unlimited). Default: 0.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        preview_only = kwargs.get("preview_only", False)
        section = kwargs.get("section", "")
        max_tokens = kwargs.get("max_tokens", 0)
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        import json
        result = self._manager.load_artifact(
            artifact_id=artifact_id,
            preview_only=preview_only,
            section=section,
            max_tokens=max_tokens,
        )
        logger.info("artifact_load: %s preview=%s section=%s", artifact_id, preview_only, section or "all")
        return json.dumps(result, ensure_ascii=False)


class ArtifactContextBudgetTool(BaseTool):
    name = "artifact_context_budget"
    description = "Get context budget info: total token usage across artifacts, breakdown by type."
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
        result = self._manager.get_context_budget(agent_id=agent_id)
        logger.info("artifact_context_budget: agent=%s tokens=%d", agent_id, result.get("total_tokens", 0))
        return json.dumps(result, ensure_ascii=False)
