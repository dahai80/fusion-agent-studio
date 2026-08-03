"""Artifact FC tools and policy — Issues #32, #33, #34.

Importers: daemon_server.py (RPC dispatch via _get_artifact_manager),
           api_server.py (REST /agents/{id}/artifacts).
Affected API: artifact.create/update/search/export/list/delete/context RPC methods.
Data schemas: ArtifactRecord, ArtifactManager, ArtifactPolicyConfig (from agent_definition).
User instruction: "后续功能也要马上启动落地实施" — implement remaining open issues.
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

        if content is not None:
            rec.content = content
        if metadata is not None:
            rec.metadata.update(metadata)
        rec.updated_at = time.time()
        rec.version += 1
        self._persist()
        logger.info("Updated artifact %s (v%d)", artifact_id, rec.version)
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
