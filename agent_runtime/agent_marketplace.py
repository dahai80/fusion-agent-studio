"""Agent marketplace — import/export, template registry, search/filter.

Provides .fusion-agent package serialization, marketplace index,
template search with filtering, and one-click install.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MARKETPLACE_DIR = Path.home() / ".fusion-agent-studio" / "marketplace"
AGENT_EXT = ".fusion-agent"


@dataclass
class MarketEntry:
    id: str = ""
    name: str = ""
    author: str = ""
    description: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    graph_data: dict[str, Any] = field(default_factory=dict)
    rating: float = 0.0
    downloads: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "version": self.version,
            "graph_data": self.graph_data,
            "rating": self.rating,
            "downloads": self.downloads,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketEntry:
        return cls(
            id=data["id"],
            name=data["name"],
            author=data.get("author", ""),
            description=data.get("description", ""),
            category=data.get("category", ""),
            tags=data.get("tags", []),
            version=data.get("version", "1.0.0"),
            graph_data=data.get("graph_data", {}),
            rating=data.get("rating", 0.0),
            downloads=data.get("downloads", 0),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )


class AgentMarketplace:
    """Agent template marketplace with search, install, and export."""

    def __init__(self, store_dir: Path | str | None = None):
        self.store_dir = Path(store_dir) if store_dir else MARKETPLACE_DIR
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, MarketEntry] = {}
        self._load_index()

    def _index_path(self) -> Path:
        return self.store_dir / "index.json"

    def _load_index(self):
        idx_path = self._index_path()
        if idx_path.exists():
            with open(idx_path) as f:
                data = json.load(f)
            for entry_data in data:
                entry = MarketEntry.from_dict(entry_data)
                self._index[entry.id] = entry
            logger.info("Loaded %d marketplace entries", len(self._index))
        else:
            logger.info("No marketplace index found, starting fresh")

    def _save_index(self):
        with open(self._index_path(), "w") as f:
            json.dump([e.to_dict() for e in self._index.values()], f, indent=2)
        logger.debug("Saved marketplace index: %d entries", len(self._index))

    def publish(self, entry: MarketEntry) -> str:
        if not entry.id:
            entry.id = str(uuid.uuid4())[:8]
        now = time.time()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        self._index[entry.id] = entry
        self._save_index()
        logger.info("Published agent: %s (%s)", entry.name, entry.id)
        return entry.id

    def unpublish(self, entry_id: str) -> bool:
        if entry_id in self._index:
            del self._index[entry_id]
            self._save_index()
            logger.info("Unpublished agent: %s", entry_id)
            return True
        return False

    def get(self, entry_id: str) -> MarketEntry | None:
        return self._index.get(entry_id)

    def search(
        self,
        query: str = "",
        category: str = "",
        tags: list[str] | None = None,
        sort_by: str = "name",
        limit: int = 50,
    ) -> list[MarketEntry]:
        results = list(self._index.values())

        if query:
            q = query.lower()
            results = [e for e in results if q in e.name.lower() or q in e.description.lower()]

        if category:
            results = [e for e in results if e.category == category]

        if tags:
            results = [e for e in results if any(t in e.tags for t in tags)]

        sort_key = {
            "name": lambda e: e.name,
            "rating": lambda e: -e.rating,
            "downloads": lambda e: -e.downloads,
            "updated": lambda e: -e.updated_at,
        }.get(sort_by, lambda e: e.name)

        results.sort(key=sort_key)
        return results[:limit]

    def list_categories(self) -> list[str]:
        return sorted({e.category for e in self._index.values() if e.category})

    def export_agent(self, entry_id: str, output_dir: Path | str) -> Path | None:
        entry = self._index.get(entry_id)
        if not entry:
            logger.error("Entry not found: %s", entry_id)
            return None

        agent_dir = Path(output_dir) / f"{entry.name}{AGENT_EXT}"
        agent_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "id": entry.id,
            "name": entry.name,
            "author": entry.author,
            "description": entry.description,
            "category": entry.category,
            "tags": entry.tags,
            "version": entry.version,
        }
        with open(agent_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)

        with open(agent_dir / "graph.json", "w") as f:
            json.dump(entry.graph_data, f, indent=2)

        logger.info("Exported agent: %s -> %s", entry.name, agent_dir)
        return agent_dir

    def import_agent(self, agent_dir: Path | str) -> MarketEntry | None:
        agent_dir = Path(agent_dir)
        manifest_path = agent_dir / "manifest.json"
        graph_path = agent_dir / "graph.json"

        if not manifest_path.exists():
            logger.error("No manifest.json in %s", agent_dir)
            return None

        with open(manifest_path) as f:
            manifest = json.load(f)

        graph_data = {}
        if graph_path.exists():
            with open(graph_path) as f:
                graph_data = json.load(f)

        entry = MarketEntry(
            id=manifest.get("id", str(uuid.uuid4())[:8]),
            name=manifest.get("name", agent_dir.name),
            author=manifest.get("author", ""),
            description=manifest.get("description", ""),
            category=manifest.get("category", ""),
            tags=manifest.get("tags", []),
            version=manifest.get("version", "1.0.0"),
            graph_data=graph_data,
        )

        self.publish(entry)
        logger.info("Imported agent: %s from %s", entry.name, agent_dir)
        return entry

    def install(self, entry_id: str, target_dir: Path | str | None = None) -> Path | None:
        result = self.export_agent(entry_id, target_dir or self.store_dir / "installed")
        if result:
            entry = self._index.get(entry_id)
            if entry:
                entry.downloads += 1
                self._save_index()
        return result
