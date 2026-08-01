from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONNECTORS_DIR = "connectors"
CONNECTORS_INDEX = "connectors_index.json"

VALID_TYPES = ("oauth2", "api_key", "webhook")
VALID_STATUSES = ("connected", "expired", "disconnected", "error")


@dataclass
class ConnectorConfig:
    id: str = ""
    name: str = ""
    type: str = "api_key"
    auth_config: dict[str, Any] = field(default_factory=dict)
    status: str = "disconnected"
    created_at: float = 0.0
    updated_at: float = 0.0
    last_tested_at: float | None = None
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "auth_config": {k: "***" if k in ("api_key", "secret", "client_secret", "password", "token") else v
                            for k, v in self.auth_config.items()},
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_tested_at": self.last_tested_at,
            "error_message": self.error_message,
        }

    def _get_full_config(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "auth_config": self.auth_config,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_tested_at": self.last_tested_at,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorConfig:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            type=data.get("type", "api_key"),
            auth_config=data.get("auth_config", {}),
            status=data.get("status", "disconnected"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            last_tested_at=data.get("last_tested_at"),
            error_message=data.get("error_message", ""),
        )


class ConnectorManager:
    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self._connectors: dict[str, ConnectorConfig] = {}
        self._load_index()

    @property
    def index_path(self) -> Path:
        return self.base_path / CONNECTORS_INDEX

    def _load_index(self) -> None:
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    cfg = ConnectorConfig.from_dict(entry)
                    self._connectors[cfg.id] = cfg
                logger.info("Loaded %d connectors", len(self._connectors))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load connectors index: %s", exc)
                self._connectors = {}

    def _persist_index(self) -> None:
        self.base_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_path, 0o700)
        except OSError as exc:
            logger.warning("Could not set connectors dir perms: %s", exc)
        data = [c._get_full_config() for c in self._connectors.values()]
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        try:
            os.chmod(self.index_path, 0o600)
        except OSError as exc:
            logger.warning("Could not set connectors index perms: %s", exc)
        logger.debug("Persisted connectors index: %d entries", len(data))

    def list_connectors(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._connectors.values()]

    def get(self, connector_id: str) -> dict[str, Any] | None:
        cfg = self._connectors.get(connector_id)
        if cfg is None:
            return None
        return cfg.to_dict()

    def create(self, name: str, conn_type: str, auth_config: dict[str, Any]) -> dict[str, Any]:
        import uuid
        if conn_type not in VALID_TYPES:
            return {"status": "error", "message": f"Invalid connector type: {conn_type}. Valid: {VALID_TYPES}"}
        conn_id = uuid.uuid4().hex[:12]
        now = time.time()
        cfg = ConnectorConfig(
            id=conn_id,
            name=name,
            type=conn_type,
            auth_config=auth_config,
            status="disconnected",
            created_at=now,
            updated_at=now,
        )
        self._connectors[conn_id] = cfg
        self._persist_index()
        logger.info("connector.create: id=%s name=%s type=%s", conn_id, name, conn_type)
        return {"connector_id": conn_id, "connector": cfg.to_dict()}

    def update(self, connector_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        cfg = self._connectors.get(connector_id)
        if cfg is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        for key in ("name", "type", "status", "error_message"):
            if key in updates:
                setattr(cfg, key, updates[key])
        if "auth_config" in updates:
            cfg.auth_config = updates["auth_config"]
        cfg.updated_at = time.time()
        self._persist_index()
        logger.info("connector.update: id=%s keys=%s", connector_id, list(updates.keys()))
        return {"updated": True, "connector": cfg.to_dict()}

    def delete(self, connector_id: str) -> dict[str, Any]:
        removed = self._connectors.pop(connector_id, None)
        if removed is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        self._persist_index()
        logger.info("connector.delete: id=%s", connector_id)
        return {"deleted": True}

    def connect(self, connector_id: str) -> dict[str, Any]:
        cfg = self._connectors.get(connector_id)
        if cfg is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        test_result = self._test_auth(cfg)
        if test_result:
            cfg.status = "connected"
            cfg.error_message = ""
        else:
            cfg.status = "error"
            cfg.error_message = "Authentication test failed"
        cfg.last_tested_at = time.time()
        cfg.updated_at = time.time()
        self._persist_index()
        logger.info("connector.connect: id=%s status=%s", connector_id, cfg.status)
        return {"connector_id": connector_id, "status": cfg.status, "error_message": cfg.error_message}

    def disconnect(self, connector_id: str) -> dict[str, Any]:
        cfg = self._connectors.get(connector_id)
        if cfg is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        cfg.status = "disconnected"
        cfg.updated_at = time.time()
        self._persist_index()
        logger.info("connector.disconnect: id=%s", connector_id)
        return {"connector_id": connector_id, "status": "disconnected"}

    def test(self, connector_id: str) -> dict[str, Any]:
        cfg = self._connectors.get(connector_id)
        if cfg is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        success = self._test_auth(cfg)
        cfg.last_tested_at = time.time()
        self._persist_index()
        logger.info("connector.test: id=%s success=%s", connector_id, success)
        return {"connector_id": connector_id, "test_passed": success, "status": cfg.status}

    def _test_auth(self, cfg: ConnectorConfig) -> bool:
        if cfg.type == "api_key":
            api_key = cfg.auth_config.get("api_key", "")
            return bool(api_key.strip())
        elif cfg.type == "oauth2":
            has_token = bool(cfg.auth_config.get("access_token", ""))
            return has_token
        elif cfg.type == "webhook":
            has_url = bool(cfg.auth_config.get("url", ""))
            return has_url
        return False
