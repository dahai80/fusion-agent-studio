import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import time

logger = logging.getLogger(__name__)

try:
    from agent_runtime.data_ingestion import DocumentReader, ETLPipeline
    _HAS_DATA_INGESTION = True
except ImportError:
    _HAS_DATA_INGESTION = False
    logger.warning("agent_runtime.data_ingestion not available; file ingestion will be skipped")

try:
    from agent_runtime.knowledge_engine import KnowledgeEngine
    _HAS_KNOWLEDGE_ENGINE = True
except ImportError:
    _HAS_KNOWLEDGE_ENGINE = False
    logger.warning("agent_runtime.knowledge_engine not available; knowledge ingestion will be skipped")


@dataclass
class KnowledgeBase:
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    scope: str = "default"
    file_count: int = 0
    total_size: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    bound_agents: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "scope": self.scope,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "bound_agents": list(self.bound_agents),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeBase":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            scope=data.get("scope", "default"),
            file_count=data.get("file_count", 0),
            total_size=data.get("total_size", 0),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            bound_agents=data.get("bound_agents", []),
        )


@dataclass
class KBFileInfo:
    file_id: str = ""
    filename: str = ""
    size: int = 0
    content_type: str = ""
    kb_id: str = ""
    entry_count: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "size": self.size,
            "content_type": self.content_type,
            "kb_id": self.kb_id,
            "entry_count": self.entry_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KBFileInfo":
        return cls(
            file_id=data.get("file_id", ""),
            filename=data.get("filename", ""),
            size=data.get("size", 0),
            content_type=data.get("content_type", ""),
            kb_id=data.get("kb_id", ""),
            entry_count=data.get("entry_count", 0),
            created_at=data.get("created_at", 0.0),
        )


class KnowledgeBaseManager:
    def __init__(self, base_path=None):
        if base_path is None:
            self.base_path = Path.home() / ".fusion-agent-studio" / "knowledge_bases"
        else:
            self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._index_path = self.base_path / "index.json"
        logger.info("KnowledgeBaseManager initialized at %s", self.base_path)

    def _load_index(self) -> list[dict]:
        if not self._index_path.exists():
            return []
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load knowledge base index: %s", e)
            return []

    def _save_index(self, entries: list[dict]):
        try:
            with open(self._index_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Failed to save knowledge base index: %s", e)

    def _load_files_index(self, kb_id: str) -> list[dict]:
        files_index_path = self.base_path / kb_id / "files_index.json"
        if not files_index_path.exists():
            return []
        try:
            with open(files_index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load files index for kb %s: %s", kb_id, e)
            return []

    def _save_files_index(self, kb_id: str, entries: list[dict]):
        kb_dir = self.base_path / kb_id
        kb_dir.mkdir(parents=True, exist_ok=True)
        files_index_path = kb_dir / "files_index.json"
        try:
            with open(files_index_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error("Failed to save files index for kb %s: %s", kb_id, e)

    def create_kb(self, name: str, description: str = "", tags: list = None, scope: str = "default") -> KnowledgeBase:
        now = time()
        kb = KnowledgeBase(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            tags=tags or [],
            scope=scope,
            created_at=now,
            updated_at=now,
        )
        entries = self._load_index()
        entries.append(kb.to_dict())
        self._save_index(entries)
        kb_dir = self.base_path / kb.id
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "files").mkdir(parents=True, exist_ok=True)
        logger.info("Created knowledge base %s (%s)", kb.name, kb.id)
        return kb

    def get_kb(self, kb_id: str) -> KnowledgeBase | None:
        entries = self._load_index()
        for entry in entries:
            if entry.get("id") == kb_id:
                return KnowledgeBase.from_dict(entry)
        logger.warning("Knowledge base %s not found", kb_id)
        return None

    def update_kb(self, kb_id: str, updates: dict) -> KnowledgeBase | None:
        entries = self._load_index()
        for i, entry in enumerate(entries):
            if entry.get("id") == kb_id:
                for key, value in updates.items():
                    if key in ("id", "created_at"):
                        continue
                    entry[key] = value
                entry["updated_at"] = time()
                entries[i] = entry
                self._save_index(entries)
                logger.info("Updated knowledge base %s with keys %s", kb_id, list(updates.keys()))
                return KnowledgeBase.from_dict(entry)
        logger.warning("Knowledge base %s not found for update", kb_id)
        return None

    def delete_kb(self, kb_id: str) -> bool:
        entries = self._load_index()
        new_entries = [e for e in entries if e.get("id") != kb_id]
        if len(new_entries) == len(entries):
            logger.warning("Knowledge base %s not found for deletion", kb_id)
            return False
        self._save_index(new_entries)
        kb_dir = self.base_path / kb_id
        if kb_dir.exists():
            try:
                shutil.rmtree(kb_dir)
            except OSError as e:
                logger.error("Failed to remove kb directory %s: %s", kb_dir, e)
        logger.info("Deleted knowledge base %s", kb_id)
        return True

    def list_kbs(self, page: int = 1, limit: int = 20, keyword: str = "", scope: str = "") -> dict:
        entries = self._load_index()
        filtered = entries
        if keyword:
            keyword_lower = keyword.lower()
            filtered = [
                e for e in filtered
                if keyword_lower in e.get("name", "").lower()
                or keyword_lower in e.get("description", "").lower()
                or keyword_lower in " ".join(e.get("tags", [])).lower()
            ]
        if scope:
            filtered = [e for e in filtered if e.get("scope") == scope]
        total = len(filtered)
        start = (page - 1) * limit
        end = start + limit
        page_entries = filtered[start:end]
        logger.info("Listed knowledge bases: total=%d, page=%d, limit=%d", total, page, limit)
        return {
            "data": [KnowledgeBase.from_dict(e) for e in page_entries],
            "total": total,
            "page": page,
            "limit": limit,
        }

    def add_file(self, kb_id: str, file_path: str, content_type: str = "") -> KBFileInfo:
        kb = self.get_kb(kb_id)
        if kb is None:
            raise ValueError(f"Knowledge base {kb_id} not found")

        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        filename = src.name
        file_size = src.stat().st_size
        now = time()
        file_id = str(uuid.uuid4())

        dest_dir = self.base_path / kb_id / "files"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        shutil.copy2(str(src), str(dest))
        logger.info("Copied file %s to %s", src, dest)

        file_info = KBFileInfo(
            file_id=file_id,
            filename=filename,
            size=file_size,
            content_type=content_type,
            kb_id=kb_id,
            created_at=now,
        )

        self._ingest_file(kb_id, file_path=str(dest), file_info=file_info)

        files_entries = self._load_files_index(kb_id)
        files_entries.append(file_info.to_dict())
        self._save_files_index(kb_id, files_entries)

        self.update_kb(kb_id, {
            "file_count": kb.file_count + 1,
            "total_size": kb.total_size + file_size,
        })

        logger.info("Added file %s (%s) to kb %s", filename, file_id, kb_id)
        return file_info

    def _ingest_file(self, kb_id: str, file_path: str, file_info: KBFileInfo):
        if not _HAS_DATA_INGESTION or not _HAS_KNOWLEDGE_ENGINE:
            logger.warning("Skipping ingestion for %s: required modules not available", file_path)
            return

        try:
            reader = DocumentReader()
            documents = reader.read_file(file_path)
            if not documents:
                logger.warning("No documents extracted from %s", file_path)
                return

            pipeline = ETLPipeline()
            chunks = pipeline.chunk_documents(documents)
            if not chunks:
                logger.warning("No chunks produced from %s", file_path)
                return

            engine = KnowledgeEngine()
            entry_ids = engine.ingest(chunks, scope=kb_id)
            file_info.entry_count = len(entry_ids)
            logger.info("Ingested %d entries from %s into kb %s", len(entry_ids), file_path, kb_id)
        except Exception as e:
            logger.error("Failed to ingest file %s into kb %s: %s", file_path, kb_id, e)

    def list_files(self, kb_id: str) -> list[KBFileInfo]:
        entries = self._load_files_index(kb_id)
        logger.info("Listed %d files for kb %s", len(entries), kb_id)
        return [KBFileInfo.from_dict(e) for e in entries]

    def delete_file(self, kb_id: str, file_id: str) -> bool:
        files_entries = self._load_files_index(kb_id)
        target = None
        new_entries = []
        for e in files_entries:
            if e.get("file_id") == file_id:
                target = e
            else:
                new_entries.append(e)

        if target is None:
            logger.warning("File %s not found in kb %s", file_id, kb_id)
            return False

        file_path = self.base_path / kb_id / "files" / target.get("filename", "")
        if file_path.exists():
            try:
                file_path.unlink()
            except OSError as e:
                logger.error("Failed to delete file %s: %s", file_path, e)

        self._save_files_index(kb_id, new_entries)

        kb = self.get_kb(kb_id)
        if kb:
            self.update_kb(kb_id, {
                "file_count": max(0, kb.file_count - 1),
                "total_size": max(0, kb.total_size - target.get("size", 0)),
            })

        logger.info("Deleted file %s from kb %s", file_id, kb_id)
        return True

    def reparse_file(self, kb_id: str, file_id: str) -> KBFileInfo | None:
        files_entries = self._load_files_index(kb_id)
        target_entry = None
        for e in files_entries:
            if e.get("file_id") == file_id:
                target_entry = e
                break

        if target_entry is None:
            logger.warning("File %s not found in kb %s for reparse", file_id, kb_id)
            return None

        file_info = KBFileInfo.from_dict(target_entry)
        file_path = self.base_path / kb_id / "files" / file_info.filename
        if not file_path.exists():
            logger.error("File %s does not exist on disk for reparse", file_path)
            return None

        file_info.entry_count = 0
        self._ingest_file(kb_id, file_path=str(file_path), file_info=file_info)

        updated_entries = []
        for e in files_entries:
            if e.get("file_id") == file_id:
                updated_entries.append(file_info.to_dict())
            else:
                updated_entries.append(e)
        self._save_files_index(kb_id, updated_entries)

        logger.info("Reparsed file %s in kb %s, entry_count=%d", file_id, kb_id, file_info.entry_count)
        return file_info

    def bind_agent(self, kb_id: str, agent_id: str) -> bool:
        kb = self.get_kb(kb_id)
        if kb is None:
            logger.warning("Knowledge base %s not found for bind_agent", kb_id)
            return False
        if agent_id in kb.bound_agents:
            logger.info("Agent %s already bound to kb %s", agent_id, kb_id)
            return True
        kb.bound_agents.append(agent_id)
        self.update_kb(kb_id, {"bound_agents": kb.bound_agents})
        logger.info("Bound agent %s to kb %s", agent_id, kb_id)
        return True

    def unbind_agent(self, kb_id: str, agent_id: str) -> bool:
        kb = self.get_kb(kb_id)
        if kb is None:
            logger.warning("Knowledge base %s not found for unbind_agent", kb_id)
            return False
        if agent_id not in kb.bound_agents:
            logger.info("Agent %s not bound to kb %s", agent_id, kb_id)
            return True
        kb.bound_agents.remove(agent_id)
        self.update_kb(kb_id, {"bound_agents": kb.bound_agents})
        logger.info("Unbound agent %s from kb %s", agent_id, kb_id)
        return True
