import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VersionRecord:
    version_id: str
    agent_id: str
    snapshot_data: dict
    created_at: float
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "agent_id": self.agent_id,
            "snapshot_data": self.snapshot_data,
            "created_at": self.created_at,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VersionRecord":
        return cls(
            version_id=data["version_id"],
            agent_id=data["agent_id"],
            snapshot_data=data["snapshot_data"],
            created_at=data["created_at"],
            label=data.get("label", ""),
        )


class AgentVersionStore:
    def __init__(self, base_path: Optional[Path] = None):
        self.base_path = base_path or Path.home() / ".fusion-agent-studio" / "versions"
        logger.info("AgentVersionStore initialized with base_path=%s", self.base_path)

    def _agent_dir(self, agent_id: str) -> Path:
        return self.base_path / agent_id

    def _versions_file(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "versions.json"

    def _load_versions(self, agent_id: str) -> list[VersionRecord]:
        versions_file = self._versions_file(agent_id)
        if not versions_file.exists():
            logger.debug("No versions file found for agent_id=%s", agent_id)
            return []
        try:
            raw = json.loads(versions_file.read_text(encoding="utf-8"))
            records = [VersionRecord.from_dict(item) for item in raw]
            logger.debug("Loaded %d versions for agent_id=%s", len(records), agent_id)
            return records
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Failed to load versions for agent_id=%s: %s", agent_id, exc)
            return []

    def _save_versions(self, agent_id: str, records: list[VersionRecord]) -> None:
        agent_dir = self._agent_dir(agent_id)
        agent_dir.mkdir(parents=True, exist_ok=True)
        versions_file = self._versions_file(agent_id)
        raw = [record.to_dict() for record in records]
        versions_file.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug("Saved %d versions for agent_id=%s", len(records), agent_id)

    def save_snapshot(
        self, agent_id: str, snapshot_data: dict, label: str = ""
    ) -> VersionRecord:
        version_id = uuid.uuid4().hex
        created_at = time.time()
        record = VersionRecord(
            version_id=version_id,
            agent_id=agent_id,
            snapshot_data=snapshot_data,
            created_at=created_at,
            label=label,
        )
        records = self._load_versions(agent_id)
        records.append(record)
        self._save_versions(agent_id, records)
        logger.info(
            "Saved snapshot version_id=%s for agent_id=%s label=%s",
            version_id,
            agent_id,
            label,
        )
        return record

    def list_versions(self, agent_id: str) -> list[VersionRecord]:
        records = self._load_versions(agent_id)
        logger.info("Listed %d versions for agent_id=%s", len(records), agent_id)
        return records

    def get_version(self, agent_id: str, version_id: str) -> Optional[VersionRecord]:
        records = self._load_versions(agent_id)
        for record in records:
            if record.version_id == version_id:
                logger.info("Found version_id=%s for agent_id=%s", version_id, agent_id)
                return record
        logger.warning("Version_id=%s not found for agent_id=%s", version_id, agent_id)
        return None

    def restore_version(self, agent_id: str, version_id: str) -> Optional[dict]:
        record = self.get_version(agent_id, version_id)
        if record is None:
            logger.warning(
                "Cannot restore version_id=%s for agent_id=%s: not found",
                version_id,
                agent_id,
            )
            return None
        logger.info("Restored version_id=%s for agent_id=%s", version_id, agent_id)
        return record.snapshot_data

    def delete_version(self, agent_id: str, version_id: str) -> bool:
        records = self._load_versions(agent_id)
        original_len = len(records)
        records = [r for r in records if r.version_id != version_id]
        if len(records) == original_len:
            logger.warning(
                "Cannot delete version_id=%s for agent_id=%s: not found",
                version_id,
                agent_id,
            )
            return False
        self._save_versions(agent_id, records)
        logger.info("Deleted version_id=%s for agent_id=%s", version_id, agent_id)
        return True
