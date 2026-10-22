"""Publish registry (#437 / E1, §5.3/§9): agent_id -> publisher record
written at publish time from the authenticated caller. The dispatch door
resolves authority from THIS record, never from a client-asserted author
field. JSON file store — one record per line region (simple dict)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PublishRegistry:
    def __init__(self, path: str = ""):
        if not path:
            path = str(Path.home() / ".fusion-agent-studio" / "publish_registry.json")
        self.path = Path(path)

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("publish registry unreadable: %s", e)
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def set(self, agent_id: str, publisher: str, version: Any = None) -> dict:
        data = self._load()
        rec = {"publisher": str(publisher or ""),
               "version": version,
               "published_at": time.time()}
        data[agent_id] = rec
        self._save(data)
        return rec

    def get(self, agent_id: str) -> Dict[str, Any]:
        return self._load().get(agent_id) or {}

    def remove(self, agent_id: str) -> bool:
        data = self._load()
        if agent_id in data:
            del data[agent_id]
            self._save(data)
            return True
        return False
