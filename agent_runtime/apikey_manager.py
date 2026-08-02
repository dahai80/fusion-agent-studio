from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APIKEYS_DIR = "apikeys"
APIKEYS_INDEX = "apikeys_index.json"
_PBKDF2_ITERS = 100_000


@dataclass
class ApiKeyConfig:
    id: str = ""
    name: str = ""
    key_prefix: str = ""
    key_hash: str = ""
    permissions: list[str] = field(default_factory=list)
    allowed_agent_ids: list[str] = field(default_factory=list)
    ip_whitelist: list[str] = field(default_factory=list)
    created_at: float = 0.0
    expires_at: float | None = None
    last_used_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "permissions": self.permissions,
            "allowed_agent_ids": self.allowed_agent_ids,
            "ip_whitelist": self.ip_whitelist,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKeyConfig:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            key_prefix=data.get("key_prefix", ""),
            key_hash=data.get("key_hash", ""),
            permissions=data.get("permissions", []),
            allowed_agent_ids=data.get("allowed_agent_ids", []),
            ip_whitelist=data.get("ip_whitelist", []),
            created_at=data.get("created_at", 0.0),
            expires_at=data.get("expires_at"),
            last_used_at=data.get("last_used_at"),
        )


def _hash_key(key: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", key.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt}${derived.hex()}"


def _verify_key(key: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters_s, salt, hash_hex = stored.split("$")
            derived = hashlib.pbkdf2_hmac(
                "sha256", key.encode(), salt.encode(), int(iters_s)
            )
            return secrets.compare_digest(derived.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    return secrets.compare_digest(hashlib.sha256(key.encode()).hexdigest(), stored)


class ApiKeyManager:
    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self._keys: dict[str, ApiKeyConfig] = {}
        self._load_index()

    @property
    def index_path(self) -> Path:
        return self.base_path / APIKEYS_INDEX

    def _load_index(self) -> None:
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    cfg = ApiKeyConfig.from_dict(entry)
                    self._keys[cfg.id] = cfg
                logger.info("Loaded %d API keys", len(self._keys))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load API keys index: %s", exc)
                self._keys = {}

    def _persist_index(self) -> None:
        self.base_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.base_path, 0o700)
        except OSError as exc:
            logger.warning("Could not set apikeys dir perms: %s", exc)
        data = []
        for k in self._keys.values():
            d = k.to_dict()
            d["key_hash"] = k.key_hash
            data.append(d)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        try:
            os.chmod(self.index_path, 0o600)
        except OSError as exc:
            logger.warning("Could not set apikeys index perms: %s", exc)
        logger.debug("Persisted API keys index: %d entries", len(data))

    def create(
        self,
        name: str,
        permissions: list[str] | None = None,
        allowed_agent_ids: list[str] | None = None,
        ip_whitelist: list[str] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        import uuid

        key_id = uuid.uuid4().hex[:12]
        raw_key = f"fk-{secrets.token_hex(24)}"
        key_prefix = raw_key[:10]
        key_hash = _hash_key(raw_key)
        now = time.time()
        cfg = ApiKeyConfig(
            id=key_id,
            name=name,
            key_prefix=key_prefix,
            key_hash=key_hash,
            permissions=permissions or ["agent:execute"],
            allowed_agent_ids=allowed_agent_ids or [],
            ip_whitelist=ip_whitelist or [],
            created_at=now,
            expires_at=expires_at,
        )
        self._keys[key_id] = cfg
        self._persist_index()
        logger.info("apikey.create: id=%s name=%s", key_id, name)
        return {
            "key_id": key_id,
            "key_secret": raw_key,
            "key_prefix": key_prefix,
            "permissions": cfg.permissions,
        }

    def list_keys(self) -> list[dict[str, Any]]:
        return [k.to_dict() for k in self._keys.values()]

    def revoke(self, key_id: str) -> dict[str, Any]:
        removed = self._keys.pop(key_id, None)
        if removed is None:
            return {"status": "error", "message": f"API key not found: {key_id}"}
        self._persist_index()
        logger.info("apikey.revoke: id=%s", key_id)
        return {"revoked": True}

    def rotate(self, key_id: str) -> dict[str, Any]:
        cfg = self._keys.get(key_id)
        if cfg is None:
            return {"status": "error", "message": f"API key not found: {key_id}"}
        raw_key = f"fk-{secrets.token_hex(24)}"
        cfg.key_prefix = raw_key[:10]
        cfg.key_hash = _hash_key(raw_key)
        self._persist_index()
        logger.info("apikey.rotate: id=%s", key_id)
        return {"key_id": key_id, "key_secret": raw_key, "key_prefix": cfg.key_prefix}

    def update(self, key_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        cfg = self._keys.get(key_id)
        if cfg is None:
            return {"status": "error", "message": f"API key not found: {key_id}"}
        for key in (
            "name",
            "permissions",
            "allowed_agent_ids",
            "ip_whitelist",
            "expires_at",
        ):
            if key in updates:
                setattr(cfg, key, updates[key])
        self._persist_index()
        logger.info("apikey.update: id=%s keys=%s", key_id, list(updates.keys()))
        return {"updated": True, "key": cfg.to_dict()}

    def validate(
        self, raw_key: str, agent_id: str | None = None, client_ip: str | None = None
    ) -> dict[str, Any]:
        for cfg in self._keys.values():
            if _verify_key(raw_key, cfg.key_hash):
                if cfg.expires_at and time.time() > cfg.expires_at:
                    return {"valid": False, "reason": "key_expired"}
                if (
                    cfg.allowed_agent_ids
                    and agent_id
                    and agent_id not in cfg.allowed_agent_ids
                ):
                    return {"valid": False, "reason": "agent_not_allowed"}
                if cfg.ip_whitelist and client_ip and client_ip not in cfg.ip_whitelist:
                    return {"valid": False, "reason": "ip_not_allowed"}
                cfg.last_used_at = time.time()
                self._persist_index()
                return {"valid": True, "key_id": cfg.id, "permissions": cfg.permissions}
        return {"valid": False, "reason": "key_not_found"}
