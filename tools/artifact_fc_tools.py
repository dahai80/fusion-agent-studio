"""Artifact FC tools — 8 BaseTool subclasses wrapping ArtifactManager.

Importers: tools/__init__.py (create_default_registry),
           agent_runtime/runtime.py (tool execution),
           agent_runtime/dispatchers/agent.py (RPC dispatch).
Affected API: artifact_get_source, artifact_create, artifact_update,
             artifact_create_snapshot, artifact_list_all,
             artifact_patch, artifact_load, artifact_context_budget.
Data schemas: ArtifactRecord, ArtifactManager (from artifact_tools).
Issue #62 — AS-1~8: load with preview/section, auto-trigger, patch ops,
             pagination, budget-aware context, compaction, system prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tools.base import BaseTool

logger = logging.getLogger(__name__)


class ArtifactGetSourceTool(BaseTool):
    name = "artifact_get_source"
    description = "Load an artifact's content by ID. Supports preview_only and section-based loading for efficient context usage. Returns content, sections list, summary, and token count."
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
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        preview_only = kwargs.get("preview_only", False)
        section = kwargs.get("section", "")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        result = self._manager.load_artifact(
            artifact_id=artifact_id,
            preview_only=preview_only,
            section=section,
        )
        if result.get("status") == "error":
            return result.get("message", "Artifact not found")
        logger.info(
            "artifact_get_source: loaded %s preview=%s section=%s tokens=%d",
            artifact_id,
            preview_only,
            section or "all",
            result.get("token_count", 0),
        )
        return json.dumps(result, ensure_ascii=False)


class ArtifactCreateTool(BaseTool):
    name = "artifact_create"
    description = "Create a new artifact with name, type, and content. Supports auto-trigger: when auto_trigger=true, only creates if content exceeds 30 lines / 1500 chars."
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
        "auto_trigger": {
            "type": "boolean",
            "description": "If true, only create when content exceeds threshold (30 lines / 1500 chars). Default: false.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        agent_id = kwargs.get("agent_id", "")
        name = kwargs.get("name", "")
        content = kwargs.get("content", "")
        artifact_type = kwargs.get("artifact_type", "document")
        auto_trigger = kwargs.get("auto_trigger", False)
        if not name:
            return "Error: name is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        result = self._manager.create_artifact(
            agent_id=agent_id,
            name=name,
            artifact_type=artifact_type,
            content=content,
            auto_trigger=auto_trigger,
        )
        logger.info(
            "artifact_create: %s status=%s auto_trigger=%s",
            name,
            result.get("status"),
            auto_trigger,
        )
        return json.dumps(result, ensure_ascii=False)


class ArtifactUpdateTool(BaseTool):
    name = "artifact_update"
    description = "Update an existing artifact. Supports full content replacement, metadata merge, or incremental patch operations (replace_section, append, prepend, delete_section). Increments version."
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
            "description": "Content text. For full update: new content. For patch ops: content to apply.",
        },
        "metadata": {
            "type": "object",
            "description": "Metadata fields to merge (optional, only for full update).",
        },
        "operation": {
            "type": "string",
            "description": "Patch operation: replace_section, append, prepend, delete_section. Empty for full content update.",
        },
        "anchor": {
            "type": "string",
            "description": "Section anchor name for replace_section or delete_section operations.",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        artifact_id = kwargs.get("artifact_id", "")
        agent_id = kwargs.get("agent_id", "")
        content = kwargs.get("content")
        metadata = kwargs.get("metadata")
        operation = kwargs.get("operation", "")
        anchor = kwargs.get("anchor", "")
        if not artifact_id:
            return "Error: artifact_id is required"
        if not self._manager:
            return "Error: ArtifactManager not available"
        result = self._manager.update_artifact(
            artifact_id=artifact_id,
            agent_id=agent_id,
            content=content,
            metadata=metadata,
            operation=operation,
            anchor=anchor,
        )
        logger.info(
            "artifact_update: %s op=%s status=%s",
            artifact_id,
            operation or "full",
            result.get("status"),
        )
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
        load_result = self._manager.get_artifact(artifact_id)
        if load_result.get("status") == "error":
            return load_result.get("message", "Artifact not found")
        record = load_result["record"]
        export_result = self._manager.export_artifact(artifact_id)
        logger.info(
            "artifact_create_snapshot: %s v%d export=%s",
            artifact_id,
            record.get("version", 1),
            export_result.get("status"),
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
    description = "List all artifacts with pagination support. Filter by owner agent ID and artifact type."
    parameters = {
        "agent_id": {
            "type": "string",
            "description": "Filter by owner agent ID (optional, empty for all).",
        },
        "page": {
            "type": "integer",
            "description": "Page number (1-based). Default: 1.",
        },
        "limit": {
            "type": "integer",
            "description": "Items per page. Default: 20.",
        },
        "artifact_type": {
            "type": "string",
            "description": "Filter by artifact type (optional).",
        },
    }

    def __init__(self, artifact_manager: Any | None = None):
        self._manager = artifact_manager

    async def execute(self, **kwargs) -> str:
        agent_id = kwargs.get("agent_id", "")
        page = kwargs.get("page", 1)
        limit = kwargs.get("limit", 20)
        artifact_type = kwargs.get("artifact_type", "")
        if not self._manager:
            return "Error: ArtifactManager not available"
        result = self._manager.list_artifacts_paginated(
            agent_id=agent_id,
            page=page,
            limit=limit,
            artifact_type=artifact_type,
        )
        logger.info(
            "artifact_list_all: page=%d limit=%d total=%d",
            result.get("page", 1),
            result.get("limit", 20),
            result.get("total", 0),
        )
        return json.dumps(result, ensure_ascii=False)


class ArtifactPatchTool(BaseTool):
    name = "artifact_patch"
    description = "Incrementally patch an artifact's content. Supports 6 operations: replace, append, prepend, section_replace, replace_section, delete_section."
    parameters = {
        "artifact_id": {
            "type": "string",
            "description": "The artifact ID to patch.",
        },
        "operation": {
            "type": "string",
            "description": "Patch operation: replace, append, prepend, section_replace, replace_section, delete_section.",
        },
        "content": {
            "type": "string",
            "description": "Content to apply in the patch (not needed for delete_section).",
        },
        "section": {
            "type": "string",
            "description": "Section name for section_replace/replace_section/delete_section operations (optional).",
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
        result = self._manager.patch_artifact(
            artifact_id=artifact_id,
            operation=operation,
            content=content,
            section=section,
            agent_id=agent_id,
        )
        logger.info(
            "artifact_patch: %s op=%s status=%s",
            artifact_id,
            operation,
            result.get("status"),
        )
        return json.dumps(result, ensure_ascii=False)


class ArtifactLoadTool(BaseTool):
    name = "artifact_load"
    description = "Load artifact content with optional preview_only or section-based loading for efficient context usage. Returns content, sections list, summary, and token count."
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
        result = self._manager.load_artifact(
            artifact_id=artifact_id,
            preview_only=preview_only,
            section=section,
            max_tokens=max_tokens,
        )
        logger.info(
            "artifact_load: %s preview=%s section=%s",
            artifact_id,
            preview_only,
            section or "all",
        )
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
        result = self._manager.get_context_budget(agent_id=agent_id)
        logger.info(
            "artifact_context_budget: agent=%s tokens=%d",
            agent_id,
            result.get("total_tokens", 0),
        )
        return json.dumps(result, ensure_ascii=False)
