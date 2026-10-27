"""Agent marketplace — import/export, template registry, search/filter.

Provides .fusion-agent package serialization, marketplace index,
template search with filtering, and one-click install.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
    # N17 #356: air-gapped integrity — sha256 over canonical graph_data,
    # stamped at publish, verified at install (tampered package refuses).
    sha256: str = ""
    # #481: tenant scoping + visibility. private entries are only visible to
    # the owning tenant; public entries are visible to every tenant.
    tenant: str = "default"
    visibility: str = "public"

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
            "sha256": self.sha256,
            "tenant": self.tenant,
            "visibility": self.visibility,
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
            sha256=data.get("sha256", ""),
            tenant=data.get("tenant", "default"),
            visibility=data.get("visibility", "public"),
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

    def _audit_path(self) -> Path:
        return self.store_dir / "audit.jsonl"

    # ── #481: audit trail ──────────────────────────────────────────────

    def _audit(self, event: str, entry_id: str, tenant: str, detail: str = "") -> None:
        record = {
            "ts": time.time(),
            "event": event,
            "entry_id": entry_id,
            "tenant": tenant,
        }
        if detail:
            record["detail"] = detail
        try:
            with open(self._audit_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info("marketplace audit: %s entry=%s tenant=%s%s",
                        event, entry_id, tenant, f" ({detail})" if detail else "")
        except OSError as exc:
            logger.error("marketplace audit write failed: %s", exc)

    def audit_records(self, tenant: str | None = None, limit: int = 100) -> list[dict]:
        """Tail of the audit log. tenant=None returns all records (admin
        view); otherwise only that tenant's records."""
        path = self._audit_path()
        if not path.exists():
            return []
        records: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if tenant is None or rec.get("tenant") == tenant:
                        records.append(rec)
        except OSError as exc:
            logger.error("marketplace audit read failed: %s", exc)
            return []
        return records[-limit:]

    # ── #481: tenant visibility ────────────────────────────────────────

    @staticmethod
    def _visible(entry: MarketEntry, tenant: str) -> bool:
        if entry.visibility != "private":
            return True
        return entry.tenant == tenant

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

    @staticmethod
    def compute_signature(graph_data: dict[str, Any]) -> str:
        payload = json.dumps(graph_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def publish(self, entry: MarketEntry, tenant: str = "default") -> str:
        if not entry.tenant or entry.tenant == "default":
            entry.tenant = tenant
        if not entry.sha256:
            entry.sha256 = self.compute_signature(entry.graph_data)
        if not entry.id:
            entry.id = str(uuid.uuid4())[:8]
        now = time.time()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        self._index[entry.id] = entry
        self._save_index()
        self._audit("publish", entry.id, entry.tenant, f"visibility={entry.visibility}")
        logger.info("Published agent: %s (%s)", entry.name, entry.id)
        return entry.id

    def unpublish(self, entry_id: str, tenant: str = "default") -> bool:
        entry = self._index.get(entry_id)
        if entry is None:
            return False
        if not self._visible(entry, tenant):
            self._audit("unpublish_refused", entry_id, tenant, "not visible")
            return False
        del self._index[entry_id]
        self._save_index()
        self._audit("unpublish", entry_id, entry.tenant)
        logger.info("Unpublished agent: %s", entry_id)
        return True

    def get(self, entry_id: str, tenant: str = "default") -> MarketEntry | None:
        entry = self._index.get(entry_id)
        if entry is None or not self._visible(entry, tenant):
            return None
        return entry

    def search(
        self,
        query: str = "",
        category: str = "",
        tags: list[str] | None = None,
        sort_by: str = "name",
        limit: int = 50,
        tenant: str = "default",
    ) -> list[MarketEntry]:
        results = [e for e in self._index.values() if self._visible(e, tenant)]

        if query:
            q = query.lower()
            results = [
                e for e in results if q in e.name.lower() or q in e.description.lower()
            ]

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

    def export_agent(
        self, entry_id: str, output_dir: Path | str, tenant: str = "default"
    ) -> Path | None:
        entry = self._index.get(entry_id)
        if not entry or not self._visible(entry, tenant):
            logger.error("Entry not found (or not visible): %s", entry_id)
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

    def install(
        self, entry_id: str, target_dir: Path | str | None = None, tenant: str = "default"
    ) -> Path | None:
        entry = self._index.get(entry_id)
        if entry is None or not self._visible(entry, tenant):
            logger.error("Entry not found (or not visible to tenant %s): %s", tenant, entry_id)
            self._audit("install_refused", entry_id, tenant, "not visible")
            return None
        # N17 #356: verify air-gapped integrity before install.
        expected = self.compute_signature(entry.graph_data)
        if entry.sha256 and entry.sha256 != expected:
            logger.error(
                "install refused: signature mismatch entry=%s expected=%s got=%s",
                entry_id, expected, entry.sha256,
            )
            self._audit("install_refused", entry_id, tenant, "signature mismatch")
            return None
        result = self.export_agent(
            entry_id, target_dir or self.store_dir / "installed", tenant=tenant
        )
        if result:
            entry.downloads += 1
            self._save_index()
            self._audit("install", entry_id, tenant, f"path={result}")
        return result
