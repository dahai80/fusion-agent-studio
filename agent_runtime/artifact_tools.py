"""Artifact tools and policy — Issues #32, #33, #34, #62.

Importers: daemon_server.py (RPC dispatch via _get_artifact_manager),
           api_server.py (REST /agents/{id}/artifacts).
Affected API: artifact.create/update/search/export/list/delete/context/load/patch RPC methods.
Data schemas: ArtifactRecord, ArtifactManager, ArtifactPolicyConfig (from agent_definition).
AS-1: artifact_get_source with preview_only + section
AS-2: auto-trigger on output > 30 lines / 1500 chars
AS-3: artifact_update with patch operations (replace_section, append, prepend, delete_section)
AS-5: pagination for list_all
AS-6: budget-aware context injection
AS-7: proactive context compaction
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.agent_definition import ArtifactPolicyConfig

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = os.path.expanduser("~/.fusion-agent-studio/artifacts")


@dataclass
class ArtifactRecord:
    artifact_id: str = ""
    name: str = ""
    artifact_type: str = "document"
    owner_agent_id: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 1
    summary: str = ""
    tags: list[str] = field(default_factory=list)

    def sections(self) -> list[str]:
        sections = []
        idx = 0
        while True:
            start_marker = "<!-- section:"
            idx = self.content.find(start_marker, idx)
            if idx == -1:
                break
            name_start = idx + len(start_marker)
            name_end = self.content.find(" -->", name_start)
            if name_end == -1:
                break
            sections.append(self.content[name_start:name_end])
            idx = name_end
        return sections

    def auto_summary(self) -> str:
        if self.summary:
            return self.summary
        content = self.content.strip()
        if not content:
            return ""
        first_line = content.split("\n")[0][:120]
        return first_line

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "artifact_type": self.artifact_type,
            "owner_agent_id": self.owner_agent_id,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "summary": self.auto_summary(),
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactRecord:
        return cls(
            artifact_id=data.get("artifact_id", ""),
            name=data.get("name", ""),
            artifact_type=data.get("artifact_type", "document"),
            owner_agent_id=data.get("owner_agent_id", ""),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            version=data.get("version", 1),
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
        )


VALID_ARTIFACT_TYPES = {"document", "code", "data", "image", "config", "report"}


class ArtifactManager:
    """Manage artifact CRUD with policy enforcement and context injection."""

    def __init__(self, artifacts_dir: str = ARTIFACTS_DIR):
        self._dir = artifacts_dir
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._policies: dict[str, ArtifactPolicyConfig] = {}
        os.makedirs(self._dir, exist_ok=True)
        self._load()

    def _load(self):
        index_path = os.path.join(self._dir, "index.json")
        if os.path.exists(index_path):
            try:
                with open(index_path) as f:
                    data = json.load(f)
                for item in data.get("artifacts", []):
                    rec = ArtifactRecord.from_dict(item)
                    self._artifacts[rec.artifact_id] = rec
                logger.info(
                    "Loaded %d artifacts from %s", len(self._artifacts), index_path
                )
            except (ValueError, TypeError, OSError, RuntimeError) as e:
                logger.error("Failed to load artifacts index: %s", e)

    def _persist(self):
        index_path = os.path.join(self._dir, "index.json")
        try:
            data = {"artifacts": [a.to_dict() for a in self._artifacts.values()]}
            with open(index_path, "w") as f:
                json.dump(data, f, indent=2)
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("Failed to persist artifacts index: %s", e)

    def set_policy(self, agent_id: str, policy: ArtifactPolicyConfig):
        self._policies[agent_id] = policy
        logger.info("Set artifact policy for agent %s", agent_id)

    def get_policy(self, agent_id: str) -> ArtifactPolicyConfig | None:
        return self._policies.get(agent_id)

    def create_artifact(
        self,
        agent_id: str,
        name: str,
        artifact_type: str = "document",
        content: str = "",
        metadata: dict | None = None,
        auto_trigger: bool = False,
    ) -> dict[str, Any]:
        policy = self._policies.get(agent_id)
        if policy and "create" not in policy.creation_triggers:
            return {
                "status": "error",
                "message": f"Agent {agent_id} not allowed to create artifacts",
            }

        if artifact_type not in VALID_ARTIFACT_TYPES:
            return {
                "status": "error",
                "message": f"Invalid artifact_type '{artifact_type}'",
            }

        if auto_trigger:
            threshold_lines = 30
            threshold_chars = 1500
            if policy:
                threshold_lines = getattr(policy, "auto_create_threshold_lines", 30)
                threshold_chars = getattr(policy, "auto_create_threshold_chars", 1500)
            line_count = content.count("\n") + 1
            if line_count < threshold_lines and len(content) < threshold_chars:
                return {
                    "status": "skipped",
                    "message": f"Content below auto-trigger threshold ({line_count} lines / {len(content)} chars)",
                }
            logger.info(
                "Auto-trigger activated: %d lines / %d chars (thresholds: %d / %d)",
                line_count, len(content), threshold_lines, threshold_chars,
            )

        record = ArtifactRecord(
            artifact_id=uuid.uuid4().hex[:12],
            name=name,
            artifact_type=artifact_type,
            owner_agent_id=agent_id,
            content=content,
            metadata=metadata or {},
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._artifacts[record.artifact_id] = record
        self._persist()
        logger.info(
            "Created artifact %s (%s) for agent %s", record.artifact_id, name, agent_id
        )
        return {
            "status": "ok",
            "artifact_id": record.artifact_id,
            "record": record.to_dict(),
        }

    def update_artifact(
        self,
        artifact_id: str,
        agent_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        operation: str = "",
        anchor: str = "",
    ) -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}

        policy = self._policies.get(agent_id)
        if policy and "update" not in policy.update_triggers:
            return {
                "status": "error",
                "message": f"Agent {agent_id} not allowed to update artifacts",
            }

        if operation:
            valid_ops = {"replace_section", "append", "prepend", "delete_section"}
            if operation not in valid_ops:
                return {
                    "status": "error",
                    "message": f"Invalid patch operation '{operation}', must be one of {valid_ops}",
                }
            if content is None and operation != "delete_section":
                return {
                    "status": "error",
                    "message": f"content is required for operation '{operation}'",
                }
            if operation == "replace_section":
                if not anchor:
                    return {
                        "status": "error",
                        "message": "anchor is required for replace_section operation",
                    }
                marker_start = f"<!-- section:{anchor} -->"
                marker_end = f"<!-- end:{anchor} -->"
                if marker_start in rec.content and marker_end in rec.content:
                    before = rec.content[: rec.content.index(marker_start)]
                    after = rec.content[rec.content.index(marker_end) + len(marker_end) :]
                    rec.content = f"{before}{marker_start}\n{content}\n{marker_end}{after}"
                else:
                    rec.content += f"\n{marker_start}\n{content}\n{marker_end}"
            elif operation == "append":
                rec.content += content
            elif operation == "prepend":
                rec.content = content + rec.content
            elif operation == "delete_section":
                if not anchor:
                    return {
                        "status": "error",
                        "message": "anchor is required for delete_section operation",
                    }
                marker_start = f"<!-- section:{anchor} -->"
                marker_end = f"<!-- end:{anchor} -->"
                if marker_start in rec.content and marker_end in rec.content:
                    before = rec.content[: rec.content.index(marker_start)]
                    after = rec.content[rec.content.index(marker_end) + len(marker_end) :]
                    rec.content = f"{before}{after}"
                else:
                    return {
                        "status": "error",
                        "message": f"Section '{anchor}' not found in artifact",
                    }
        else:
            if content is not None:
                rec.content = content
            if metadata is not None:
                rec.metadata.update(metadata)

        rec.updated_at = time.time()
        rec.version += 1
        self._persist()
        logger.info("Updated artifact %s op=%s (v%d)", artifact_id, operation or "full", rec.version)
        return {"status": "ok", "record": rec.to_dict()}

    def search_artifacts(
        self, query: str = "", artifact_type: str = "", owner_agent_id: str = ""
    ) -> list[dict[str, Any]]:
        results = []
        for rec in self._artifacts.values():
            if artifact_type and rec.artifact_type != artifact_type:
                continue
            if owner_agent_id and rec.owner_agent_id != owner_agent_id:
                continue
            if (
                query
                and query.lower() not in rec.name.lower()
                and query.lower() not in rec.content.lower()
            ):
                continue
            results.append(rec.to_dict())
        logger.info("Artifact search returned %d results", len(results))
        return results

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}
        return {"status": "ok", "record": rec.to_dict()}

    def list_artifacts(self, agent_id: str = "") -> list[dict[str, Any]]:
        if agent_id:
            return [
                a.to_dict()
                for a in self._artifacts.values()
                if a.owner_agent_id == agent_id
            ]
        return [a.to_dict() for a in self._artifacts.values()]

    def list_artifacts_paginated(
        self,
        agent_id: str = "",
        page: int = 1,
        limit: int = 20,
        artifact_type: str = "",
    ) -> dict[str, Any]:
        artifacts = list(self._artifacts.values())
        if agent_id:
            artifacts = [a for a in artifacts if a.owner_agent_id == agent_id]
        if artifact_type:
            artifacts = [a for a in artifacts if a.artifact_type == artifact_type]

        total = len(artifacts)
        artifacts.sort(key=lambda a: a.updated_at, reverse=True)
        start = (page - 1) * limit
        end = start + limit
        page_items = artifacts[start:end]

        logger.info(
            "list_artifacts_paginated: agent=%s page=%d limit=%d total=%d returned=%d",
            agent_id, page, limit, total, len(page_items),
        )
        return {
            "status": "ok",
            "artifacts": [a.to_dict() for a in page_items],
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if limit > 0 else 0,
        }

    def delete_artifact(self, artifact_id: str, agent_id: str = "") -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}
        if agent_id and rec.owner_agent_id != agent_id:
            return {"status": "error", "message": "Only owner can delete artifact"}
        del self._artifacts[artifact_id]
        self._persist()
        logger.info("Deleted artifact %s", artifact_id)
        return {"status": "ok"}

    def export_artifact(self, artifact_id: str) -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}
        export_path = os.path.join(self._dir, f"{artifact_id}.json")
        try:
            with open(export_path, "w") as f:
                json.dump(rec.to_dict(), f, indent=2)
            logger.info("Exported artifact %s to %s", artifact_id, export_path)
            return {"status": "ok", "path": export_path}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("Failed to export artifact %s: %s", artifact_id, e)
            return {"status": "error", "message": str(e)}

    def get_active_artifacts_context(self, agent_id: str, limit: int = 5) -> str:
        agent_artifacts = [
            a for a in self._artifacts.values() if a.owner_agent_id == agent_id
        ]
        agent_artifacts.sort(key=lambda a: a.updated_at, reverse=True)
        recent = agent_artifacts[:limit]
        if not recent:
            return ""
        lines = ["[Active Artifacts]"]
        for a in recent:
            lines.append(
                f"- {a.name} ({a.artifact_type}, v{a.version}): {a.content[:200]}"
            )
        return "\n".join(lines)

    def get_active_artifacts_context_budget_aware(
        self,
        agent_id: str,
        context_window: int = 32768,
        limit: int = 5,
    ) -> dict[str, Any]:
        agent_artifacts = [
            a for a in self._artifacts.values() if a.owner_agent_id == agent_id
        ]
        agent_artifacts.sort(key=lambda a: a.updated_at, reverse=True)
        recent = agent_artifacts[:limit]

        if not recent:
            return {"context_text": "", "mode": "none", "artifact_count": 0}

        artifact_tokens = sum(len(a.content) // 4 for a in recent)
        utilization = artifact_tokens / context_window if context_window > 0 else 0.0

        if utilization < 0.7:
            mode = "full"
            lines = ["[Active Artifacts (full)]"]
            for a in recent:
                lines.append(f"## {a.name} (id={a.artifact_id}, type={a.artifact_type}, v{a.version})")
                lines.append(a.content)
                lines.append("")
            context_text = "\n".join(lines)
        elif utilization < 0.9:
            mode = "preview"
            lines = ["[Active Artifacts (preview — budget at {:.0%})]".format(utilization)]
            for a in recent:
                sections = a.sections()
                lines.append(
                    f"- {a.name} (id={a.artifact_id}, type={a.artifact_type}, v{a.version}, "
                    f"sections={sections}, summary={a.auto_summary()})"
                )
            context_text = "\n".join(lines)
        else:
            mode = "blocked"
            lines = ["[Artifact Context BLOCKED — budget at {:.0%}, use artifact_load to fetch specific content]".format(utilization)]
            for a in recent:
                lines.append(
                    f"- {a.name} (id={a.artifact_id}, type={a.artifact_type}) [BLOCKED]"
                )
            context_text = "\n".join(lines)

        logger.info(
            "budget_aware_context: agent=%s mode=%s utilization=%.2f artifact_tokens=%d context_window=%d",
            agent_id, mode, utilization, artifact_tokens, context_window,
        )
        return {
            "context_text": context_text,
            "mode": mode,
            "utilization": utilization,
            "artifact_count": len(recent),
            "artifact_tokens": artifact_tokens,
        }

    def patch_artifact(
        self,
        artifact_id: str,
        operation: str,
        content: str = "",
        section: str = "",
        agent_id: str = "",
    ) -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}

        valid_ops = {"replace", "append", "prepend", "section_replace"}
        if operation not in valid_ops:
            return {
                "status": "error",
                "message": f"Invalid patch operation '{operation}', must be one of {valid_ops}",
            }

        policy = self._policies.get(agent_id)
        if policy and "update" not in policy.update_triggers:
            return {
                "status": "error",
                "message": f"Agent {agent_id} not allowed to patch artifacts",
            }

        if operation == "replace":
            rec.content = content
        elif operation == "append":
            rec.content += content
        elif operation == "prepend":
            rec.content = content + rec.content
        elif operation == "section_replace":
            if not section:
                return {
                    "status": "error",
                    "message": "section is required for section_replace operation",
                }
            marker_start = f"<!-- section:{section} -->"
            marker_end = f"<!-- end:{section} -->"
            if marker_start in rec.content and marker_end in rec.content:
                before = rec.content[: rec.content.index(marker_start)]
                after = rec.content[rec.content.index(marker_end) + len(marker_end) :]
                rec.content = f"{before}{marker_start}\n{content}\n{marker_end}{after}"
            else:
                rec.content += f"\n{marker_start}\n{content}\n{marker_end}"

        rec.updated_at = time.time()
        rec.version += 1
        self._persist()
        logger.info("Patched artifact %s op=%s v%d", artifact_id, operation, rec.version)
        return {"status": "ok", "record": rec.to_dict()}

    def load_artifact(
        self,
        artifact_id: str,
        preview_only: bool = False,
        section: str = "",
        max_tokens: int = 0,
    ) -> dict[str, Any]:
        rec = self._artifacts.get(artifact_id)
        if not rec:
            return {"status": "error", "message": f"Artifact {artifact_id} not found"}

        content = rec.content

        if section:
            marker_start = f"<!-- section:{section} -->"
            marker_end = f"<!-- end:{section} -->"
            if marker_start in content and marker_end in content:
                start_idx = content.index(marker_start) + len(marker_start)
                end_idx = content.index(marker_end)
                content = content[start_idx:end_idx].strip()
            else:
                content = ""

        if preview_only:
            content = content[:500]

        if max_tokens > 0:
            max_chars = max_tokens * 4
            content = content[:max_chars]

        token_count = len(content) // 4
        logger.info(
            "Loaded artifact %s preview=%s section=%s tokens~=%d",
            artifact_id, preview_only, section or "all", token_count,
        )
        return {
            "status": "ok",
            "artifact_id": artifact_id,
            "name": rec.name,
            "version": rec.version,
            "content": content,
            "token_count": token_count,
        }

    def get_context_budget(self, agent_id: str = "") -> dict[str, Any]:
        agent_artifacts = [
            a for a in self._artifacts.values() if a.owner_agent_id == agent_id
        ] if agent_id else list(self._artifacts.values())

        total_tokens = sum(len(a.content) // 4 for a in agent_artifacts)
        artifact_count = len(agent_artifacts)
        by_type: dict[str, int] = {}
        for a in agent_artifacts:
            by_type[a.artifact_type] = by_type.get(a.artifact_type, 0) + len(a.content) // 4

        logger.info(
            "Context budget: agent=%s artifacts=%d tokens~=%d",
            agent_id, artifact_count, total_tokens,
        )
        return {
            "status": "ok",
            "agent_id": agent_id,
            "artifact_count": artifact_count,
            "total_tokens": total_tokens,
            "by_type": by_type,
        }
