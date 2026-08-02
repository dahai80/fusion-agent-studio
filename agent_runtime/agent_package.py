"""Agent identity system — .fusion-agent package format.

A .fusion-agent package defines an agent's identity, personality, memory,
knowledge, and skills. It is the portable unit of agent configuration.

Structure:
    .fusion-agent/
    ├── manifest.json    — agent metadata, model config, capabilities
    ├── soul.md          — agent personality, instructions, system prompt
    ├── memory.md        — persistent memories (auto-summarized)
    ├── knowledge/
    │   ├── index.db     — sqlite-vec + FTS5 knowledge base
    │   └── sources.json — external import data source config (Notion/Git etc)
    ├── skills/          — DAG pipeline config, component skill definitions
    │   └── *.json
    └── workspace/       — OpenDevin-style code workspace snapshot
        └── .git/
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FUSION_AGENT_DIR = ".fusion-agent"
MANIFEST_FILE = "manifest.json"
SOUL_FILE = "soul.md"
MEMORY_FILE = "memory.md"
AGENTS_FILE = "agents.md"
KNOWLEDGE_DIR = "knowledge"
SKILLS_DIR = "skills"
WORKSPACE_DIR = "workspace"
SOURCES_FILE = "sources.json"


@dataclass
class AgentManifest:
    """Agent manifest — the core identity and configuration."""

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    model: str = ""
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    tools: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    safety_level: str = "L1"
    tags: list[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""
    status: str = "draft"
    version_int: int = 1
    published_at: float | None = None
    knowledge_base_ids: list[str] = field(default_factory=list)
    visibility: str = "private"
    rag_strategy: str = "hybrid"
    web_search_enabled: bool = False
    deep_research_enabled: bool = False
    connector_ids: list[str] = field(default_factory=list)
    style: str = ""
    top_p: float = 1.0
    context_window: int = 32768
    rate_limit_qps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tools": self.tools,
            "capabilities": self.capabilities,
            "safety_level": self.safety_level,
            "tags": self.tags,
            "author": self.author,
            "created_at": self.created_at,
            "status": self.status,
            "version_int": self.version_int,
            "published_at": self.published_at,
            "knowledge_base_ids": self.knowledge_base_ids,
            "visibility": self.visibility,
            "rag_strategy": self.rag_strategy,
            "web_search_enabled": self.web_search_enabled,
            "deep_research_enabled": self.deep_research_enabled,
            "connector_ids": self.connector_ids,
            "style": self.style,
            "top_p": self.top_p,
            "context_window": self.context_window,
            "rate_limit_qps": self.rate_limit_qps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentManifest:
        return cls(
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            model=data.get("model", ""),
            system_prompt=data.get("system_prompt", ""),
            temperature=data.get("temperature", 0.7),
            max_tokens=data.get("max_tokens", 4096),
            tools=data.get("tools", []),
            capabilities=data.get("capabilities", []),
            safety_level=data.get("safety_level", "L1"),
            tags=data.get("tags", []),
            author=data.get("author", ""),
            created_at=data.get("created_at", ""),
            status=data.get("status", "draft"),
            version_int=data.get("version_int", 1),
            published_at=data.get("published_at"),
            knowledge_base_ids=data.get("knowledge_base_ids", []),
            visibility=data.get("visibility", "private"),
            rag_strategy=data.get("rag_strategy", "hybrid"),
            web_search_enabled=data.get("web_search_enabled", False),
            deep_research_enabled=data.get("deep_research_enabled", False),
            connector_ids=data.get("connector_ids", []),
            style=data.get("style", ""),
            top_p=data.get("top_p", 1.0),
            context_window=data.get("context_window", 32768),
            rate_limit_qps=data.get("rate_limit_qps", 0),
        )


class AgentPackage:
    """Read and write .fusion-agent packages on disk.

    Usage:
        pkg = AgentPackage("/path/to/agent-dir")
        manifest = pkg.load_manifest()
        soul = pkg.load_soul()
        pkg.save_manifest(manifest)
    """

    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self.pkg_path = self.base_path / FUSION_AGENT_DIR

    @property
    def exists(self) -> bool:
        return self.pkg_path.is_dir()

    @property
    def manifest_path(self) -> Path:
        return self.pkg_path / MANIFEST_FILE

    @property
    def soul_path(self) -> Path:
        return self.pkg_path / SOUL_FILE

    @property
    def memory_path(self) -> Path:
        return self.pkg_path / MEMORY_FILE

    @property
    def agents_path(self) -> Path:
        return self.pkg_path / AGENTS_FILE

    @property
    def knowledge_path(self) -> Path:
        return self.pkg_path / KNOWLEDGE_DIR

    @property
    def skills_path(self) -> Path:
        return self.pkg_path / SKILLS_DIR

    @property
    def workspace_path(self) -> Path:
        return self.pkg_path / WORKSPACE_DIR

    @property
    def sources_path(self) -> Path:
        return self.pkg_path / KNOWLEDGE_DIR / SOURCES_FILE

    def init(
        self,
        manifest: AgentManifest | None = None,
        soul: str = "",
        memory: str = "",
        agents_md: str = "",
    ) -> None:
        """Initialize a new .fusion-agent package directory."""
        self.pkg_path.mkdir(parents=True, exist_ok=True)
        (self.pkg_path / KNOWLEDGE_DIR).mkdir(exist_ok=True)
        (self.pkg_path / SKILLS_DIR).mkdir(exist_ok=True)
        (self.pkg_path / WORKSPACE_DIR).mkdir(exist_ok=True)

        if manifest:
            self.save_manifest(manifest)
        elif not self.manifest_path.exists():
            self.save_manifest(AgentManifest())

        if soul:
            self.save_soul(soul)
        elif not self.soul_path.exists():
            self.save_soul("# Agent Soul\n\nDefine your agent's personality here.\n")

        if memory:
            self.save_memory(memory)
        elif not self.memory_path.exists():
            self.save_memory("")

        if agents_md:
            self.save_agents(agents_md)

        logger.info("Initialized .fusion-agent package at %s", self.pkg_path)

    def load_manifest(self) -> AgentManifest:
        """Load manifest.json from the package."""
        if not self.manifest_path.exists():
            logger.warning("manifest.json not found at %s", self.manifest_path)
            return AgentManifest()
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return AgentManifest.from_dict(data)

    def save_manifest(self, manifest: AgentManifest) -> None:
        """Save manifest.json to the package."""
        self.pkg_path.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=4, ensure_ascii=False)
        logger.info("Saved manifest.json for %s", manifest.name or "unnamed")

    def load_soul(self) -> str:
        """Load soul.md from the package."""
        if not self.soul_path.exists():
            return ""
        return self.soul_path.read_text(encoding="utf-8")

    def save_soul(self, content: str) -> None:
        """Save soul.md to the package."""
        self.pkg_path.mkdir(parents=True, exist_ok=True)
        self.soul_path.write_text(content, encoding="utf-8")
        logger.info("Saved soul.md")

    def load_memory(self) -> str:
        """Load memory.md from the package."""
        if not self.memory_path.exists():
            return ""
        return self.memory_path.read_text(encoding="utf-8")

    def save_memory(self, content: str) -> None:
        """Save memory.md to the package."""
        self.pkg_path.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(content, encoding="utf-8")

    def append_memory(self, entry: str) -> None:
        """Append a memory entry to memory.md."""
        existing = self.load_memory()
        updated = f"{existing}\n{entry}" if existing else entry
        self.save_memory(updated)

    def load_agents(self) -> str:
        """Load agents.md from the package."""
        if not self.agents_path.exists():
            return ""
        return self.agents_path.read_text(encoding="utf-8")

    def save_agents(self, content: str) -> None:
        """Save agents.md to the package."""
        self.pkg_path.mkdir(parents=True, exist_ok=True)
        self.agents_path.write_text(content, encoding="utf-8")
        logger.info("Saved agents.md")

    def list_skills(self) -> list[str]:
        """List skill names in the package."""
        if not self.skills_path.exists():
            return []
        return [f.stem for f in self.skills_path.glob("*.json") if f.is_file()]

    def load_skill(self, name: str) -> dict[str, Any]:
        """Load a skill definition by name."""
        skill_path = self.skills_path / f"{name}.json"
        if not skill_path.exists():
            return {}
        with open(skill_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_skill(self, name: str, skill_def: dict[str, Any]) -> None:
        """Save a skill definition."""
        self.skills_path.mkdir(parents=True, exist_ok=True)
        skill_path = self.skills_path / f"{name}.json"
        with open(skill_path, "w", encoding="utf-8") as f:
            json.dump(skill_def, f, indent=4, ensure_ascii=False)
        logger.info("Saved skill: %s", name)

    def delete_skill(self, name: str) -> bool:
        """Delete a skill definition."""
        skill_path = self.skills_path / f"{name}.json"
        if skill_path.exists():
            skill_path.unlink()
            logger.info("Deleted skill: %s", name)
            return True
        return False

    def get_system_prompt(self) -> str:
        """Resolve the effective system prompt — soul.md takes precedence over manifest.system_prompt."""
        soul = self.load_soul().strip()
        if soul:
            return soul
        manifest = self.load_manifest()
        return manifest.system_prompt

    def to_graph_config(self) -> dict[str, Any]:
        """Convert package to a graph-ready configuration dict."""
        manifest = self.load_manifest()
        return {
            "name": manifest.name,
            "model": manifest.model,
            "system_prompt": self.get_system_prompt(),
            "temperature": manifest.temperature,
            "max_tokens": manifest.max_tokens,
            "tools": manifest.tools,
            "capabilities": manifest.capabilities,
            "safety_level": manifest.safety_level,
        }

    # -- workspace snapshot methods --

    def snapshot_workspace(
        self, source_dir: str | Path, exclude: list[str] | None = None
    ) -> dict:
        source_dir = Path(source_dir)
        if not source_dir.is_dir():
            logger.error(
                "snapshot_workspace: source dir does not exist: %s", source_dir
            )
            return {"files": [], "total_size": 0, "timestamp": ""}

        ws_dest = self.workspace_path
        if ws_dest.exists():
            shutil.rmtree(ws_dest)

        exclude = set(exclude) if exclude else set()
        exclude.add(".git")

        def _should_skip(rel_path: Path) -> bool:
            if rel_path.name in exclude:
                return True
            return any(part in exclude for part in rel_path.parts)

        for item in source_dir.rglob("*"):
            rel = item.relative_to(source_dir)
            if _should_skip(rel):
                continue
            dest = ws_dest / rel
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)

        git_dir = source_dir / ".git"
        if git_dir.is_dir():
            git_dest = ws_dest / ".git"
            git_dest.mkdir(exist_ok=True)
            for ref_file in (git_dir / "refs").rglob("*"):
                if ref_file.is_file():
                    rel = ref_file.relative_to(git_dir)
                    dest = git_dest / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ref_file, dest)
            head_file = git_dir / "HEAD"
            if head_file.exists():
                shutil.copy2(head_file, git_dest / "HEAD")

        files_info = []
        total_size = 0
        for f in ws_dest.rglob("*"):
            if f.is_file():
                size = f.stat().st_size
                total_size += size
                files_info.append(
                    {
                        "path": str(f.relative_to(ws_dest)),
                        "size": size,
                        "modified": datetime.fromtimestamp(
                            f.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )

        result = {
            "files": files_info,
            "total_size": total_size,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        logger.info(
            "snapshot_workspace: %d files, %d bytes from %s",
            len(files_info),
            total_size,
            source_dir,
        )
        return result

    def restore_workspace(self, target_dir: str | Path) -> int:
        ws_src = self.workspace_path
        if not ws_src.is_dir():
            logger.warning(
                "restore_workspace: no workspace snapshot found at %s", ws_src
            )
            return 0

        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ws_src, target_dir, dirs_exist_ok=True)

        count = sum(1 for _ in target_dir.rglob("*") if _.is_file())
        logger.info("restore_workspace: restored %d files to %s", count, target_dir)
        return count

    def list_workspace_files(self) -> list[dict]:
        ws_dir = self.workspace_path
        if not ws_dir.is_dir():
            return []

        result = []
        for f in ws_dir.rglob("*"):
            if f.is_file():
                result.append(
                    {
                        "path": str(f.relative_to(ws_dir)),
                        "size": f.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            f.stat().st_mtime, tz=timezone.utc
                        ).isoformat(),
                    }
                )
        return result

    # -- sources.json methods --

    def load_sources(self) -> list[dict]:
        if not self.sources_path.exists():
            return []
        try:
            with open(self.sources_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("load_sources: failed to read %s: %s", self.sources_path, exc)
            return []

    def save_sources(self, sources: list[dict]) -> None:
        self.knowledge_path.mkdir(parents=True, exist_ok=True)
        with open(self.sources_path, "w", encoding="utf-8") as f:
            json.dump(sources, f, indent=4, ensure_ascii=False)
        logger.info(
            "save_sources: wrote %d sources to %s", len(sources), self.sources_path
        )

    def add_source(self, source_type: str, config: dict) -> dict:
        sources = self.load_sources()
        import uuid

        entry = {
            "id": uuid.uuid4().hex[:12],
            "type": source_type,
            "config": config,
            "last_sync": None,
        }
        sources.append(entry)
        self.save_sources(sources)
        logger.info("add_source: added %s source id=%s", source_type, entry["id"])
        return entry

    def remove_source(self, source_type: str, source_id: str) -> bool:
        sources = self.load_sources()
        original_len = len(sources)
        sources = [
            s
            for s in sources
            if not (s.get("type") == source_type and s.get("id") == source_id)
        ]
        if len(sources) == original_len:
            logger.warning(
                "remove_source: no %s source with id=%s found", source_type, source_id
            )
            return False
        self.save_sources(sources)
        logger.info("remove_source: removed %s source id=%s", source_type, source_id)
        return True

    # -- DAG skill methods --

    def load_skill_dag(self, name: str) -> dict:
        skill_path = self.skills_path / f"{name}.json"
        if not skill_path.exists():
            logger.warning("load_skill_dag: skill %s not found", name)
            return {}
        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "nodes" in data or "edges" in data:
                return data
            return {
                "name": name,
                "nodes": data.get("nodes", []),
                "edges": data.get("edges", []),
                "config": data.get("config", {}),
            }
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("load_skill_dag: failed to read %s: %s", skill_path, exc)
            return {}

    def save_skill_dag(self, name: str, dag_config: dict) -> None:
        self.skills_path.mkdir(parents=True, exist_ok=True)
        skill_path = self.skills_path / f"{name}.json"
        dag_config.setdefault("name", name)
        with open(skill_path, "w", encoding="utf-8") as f:
            json.dump(dag_config, f, indent=4, ensure_ascii=False)
        logger.info("save_skill_dag: saved DAG skill %s", name)

    def export_skill_graph(self, name: str) -> str:
        dag = self.load_skill_dag(name)
        if not dag:
            logger.warning("export_skill_graph: skill %s is empty", name)
            return "{}"
        return json.dumps(dag, indent=4, ensure_ascii=False)

    def import_skill_graph(self, name: str, graph_json: str) -> None:
        try:
            dag_config = json.loads(graph_json)
        except json.JSONDecodeError as exc:
            logger.error("import_skill_graph: invalid JSON for skill %s: %s", name, exc)
            return
        self.save_skill_dag(name, dag_config)

    @property
    def agent_id(self) -> str:
        return self.base_path.name

    def fork(self, new_name: str | None = None) -> AgentPackage:
        import uuid as _uuid

        agents_root = self.base_path.parent
        new_id = new_name or f"{self.agent_id}-copy-{_uuid.uuid4().hex[:6]}"
        new_path = agents_root / new_id
        if new_path.exists():
            new_id = f"{self.agent_id}-copy-{_uuid.uuid4().hex[:8]}"
            new_path = agents_root / new_id
        shutil.copytree(self.base_path, new_path)
        new_pkg = AgentPackage(new_path)
        manifest = new_pkg.load_manifest()
        manifest.name = new_name or f"{manifest.name} (copy)"
        manifest.status = "draft"
        manifest.created_at = datetime.now(tz=timezone.utc).isoformat()
        manifest.published_at = None
        manifest.version_int = 1
        new_pkg.save_manifest(manifest)
        logger.info("Forked agent %s -> %s", self.agent_id, new_id)
        return new_pkg

    def destroy(self) -> None:
        if self.pkg_path.exists():
            shutil.rmtree(self.pkg_path)
            logger.info("Destroyed .fusion-agent package at %s", self.pkg_path)
