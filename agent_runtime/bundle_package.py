from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

BUNDLE_HOME = Path.home() / ".fusion-agent-studio" / "bundles"
MANIFEST_NAME = "manifest.json"
FACET_KEYS = (
    "agent",
    "agents",
    "graph",
    "skills",
    "hooks",
    "connectors",
    "mcp_servers",
)
CREDENTIAL_KEYS = ("api_key", "secret", "client_secret", "password", "token")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", (name or "").strip().lower()).strip("-")
    return cleaned or f"bundle-{int(time.time())}"


@dataclass
class BundleManifest:
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    agent: dict = field(default_factory=dict)
    agents: list[dict] = field(default_factory=list)
    graph: dict = field(default_factory=dict)
    skills: list[dict] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    connectors: list[dict] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    root: Path | None = None

    @classmethod
    def from_dict(cls, data: dict, root: Path | None = None) -> BundleManifest:
        return cls(
            name=str(data.get("name", "") or ""),
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            agent=data.get("agent") or {},
            agents=list(data.get("agents") or []),
            graph=data.get("graph") or {},
            skills=list(data.get("skills") or []),
            hooks=list(data.get("hooks") or []),
            connectors=list(data.get("connectors") or []),
            mcp_servers=list(data.get("mcp_servers") or data.get("mcpServers") or []),
            root=root,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "agent": self.agent,
            "agents": self.agents,
            "graph_id": self.graph.get("id", ""),
            "skills": [s.get("name", "") for s in self.skills],
            "hooks": len(self.hooks),
            "connectors": [c.get("id", "") for c in self.connectors],
            "mcp_servers": len(self.mcp_servers),
        }

    @property
    def agent_entries(self) -> list[dict]:
        entries = []
        if self.agent:
            entries.append(self.agent)
        entries.extend(self.agents)
        return entries

    @property
    def is_bundle(self) -> bool:
        return any(
            [
                self.agent_entries,
                self.graph,
                self.skills,
                self.hooks,
                self.connectors,
                self.mcp_servers,
            ]
        )


def load_bundle_manifest(path: str | Path) -> BundleManifest | None:
    base = Path(path)
    candidates = (
        [base] if base.name == MANIFEST_NAME and base.is_file() else [base / MANIFEST_NAME]
    )
    candidates.extend([base / ".fusion-agent" / MANIFEST_NAME])
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("bundle manifest unreadable path=%s: %s", candidate, e)
            continue
        if isinstance(data, dict):
            return BundleManifest.from_dict(data, root=candidate.parent)
    return None


# #509: renamed from _resolve_credential — acceptance grep bans
# `hand-rolled _resolve-key helpers` handwritten resolvers (config Phase 2.2).
def _credential_value(key: str, value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        return "", f"{key} must be a reference string"
    if value.startswith("env:"):
        env_name = value[4:].strip()
        resolved = os.environ.get(env_name, "")
        if not resolved:
            return "", f"{key}: env {env_name} is not set"
        return resolved, ""
    if value.startswith("keychain:"):
        item = value[len("keychain:"):].strip()
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", item],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return "", f"{key}: keychain lookup failed: {e}"
        if out.returncode != 0 or not out.stdout.strip():
            return "", f"{key}: keychain item {item} not found"
        return out.stdout.strip(), ""
    return "", (
        f"{key}: inline credentials are not allowed; use env:NAME or keychain:item"
    )


class BundleInstaller:
    def __init__(
        self,
        home_dir: str | Path = "",
        skills_store=None,
        hook_engine=None,
        connector_manager=None,
        store=None,
        agents_dir: str | Path = "",
        mcp_register: Callable[[dict], Awaitable[Any]] | None = None,
        mcp_unregister: Callable[[str], Awaitable[Any]] | None = None,
    ):
        home = Path(home_dir) if home_dir else Path.home() / ".fusion-agent-studio"
        self.home_dir = home
        self.registry_dir = home / "bundles"
        self.agents_dir = Path(agents_dir) if agents_dir else home / "agents"
        self._skills_store = skills_store
        self._hook_engine = hook_engine
        self._connectors = connector_manager
        self._store = store
        self._mcp_register = mcp_register
        self._mcp_unregister = mcp_unregister
        self._manifest_root: Path | None = None
        logger.info("BundleInstaller ready home=%s", self.home_dir)

    def _skills(self):
        if self._skills_store is None:
            from .skills_store import SkillsStore

            self._skills_store = SkillsStore.with_project_dirs()
        return self._skills_store

    def _hooks(self):
        if self._hook_engine is None:
            from .hooks import HookEngine

            self._hook_engine = HookEngine()
        return self._hook_engine

    def _connector_mgr(self):
        if self._connectors is None:
            from .connectors import ConnectorManager

            self._connectors = ConnectorManager(self.home_dir)
        return self._connectors

    def _registry_path(self, name: str) -> Path:
        return self.registry_dir / f"{_slug(name)}.json"

    def list(self) -> list[dict]:
        rows = []
        if not self.registry_dir.exists():
            return rows
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                logger.warning("bundle registry unreadable path=%s: %s", path, e)
        return rows

    def get(self, name: str) -> dict | None:
        path = self._registry_path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_registry(self, record: dict) -> None:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path(record["name"]).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _install_agent(self, spec: dict, registered: dict, errors: list[str]) -> None:
        from .agent_package import AgentManifest, AgentPackage

        name = str(spec.get("name", "") or "")
        manifest = AgentManifest.from_dict({k: v for k, v in spec.items() if k != "soul"})
        manifest.name = manifest.name or name
        agent_id = _slug(manifest.name or name)
        if not agent_id:
            errors.append("agent facet requires a name")
            return
        agent_dir = self.agents_dir / agent_id
        pkg = AgentPackage(agent_dir)
        pkg.init(manifest=manifest, soul=str(spec.get("soul", "") or ""))
        index_path = self.agents_dir / "index.json"
        index: dict = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                index = {}
        index[agent_id] = {**manifest.to_dict(), "id": agent_id}
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, indent=4, ensure_ascii=False), encoding="utf-8")
        registered["agents"].append(agent_id)
        logger.info("bundle agent installed id=%s path=%s", agent_id, agent_dir)

    def _install_skill(self, spec: dict, registered: dict, errors: list[str]) -> None:
        name = str(spec.get("name", "") or "")
        if not name:
            errors.append("skill facet requires a name")
            return
        content = str(spec.get("content", "") or "")
        if not content and spec.get("path") and self._manifest_root is not None:
            skill_path = self._manifest_root / str(spec["path"])
            try:
                content = skill_path.read_text(encoding="utf-8")
            except OSError as e:
                errors.append(f"skill {name}: cannot read {skill_path}: {e}")
                return
        if not content:
            errors.append(f"skill {name}: content or path required")
            return
        try:
            self._skills().install_skill(name, content)
        except (ValueError, OSError) as e:
            errors.append(f"skill {name}: {e}")
            return
        registered["skills"].append(name)

    def _install_hook(self, spec: dict, registered: dict, errors: list[str]) -> None:
        from .hooks import HookConfig

        try:
            hook = HookConfig.from_dict(spec)
        except (KeyError, TypeError) as e:
            errors.append(f"hook invalid: {e}")
            return
        self._hooks().register(hook)
        registered["hooks"].append(hook.to_dict())

    def _install_connector(self, spec: dict, registered: dict, errors: list[str]) -> None:
        connector_id = str(spec.get("id", "") or "")
        if not connector_id:
            errors.append("connector facet requires an id")
            return
        auth_config = dict(spec.get("auth_config") or {})
        for key in CREDENTIAL_KEYS:
            if key not in auth_config:
                continue
            resolved, problem = _credential_value(key, auth_config[key])
            if problem:
                errors.append(f"connector {connector_id}: {problem}")
                return
            auth_config[key] = resolved
        mgr = self._connector_mgr()
        existing = mgr.get(connector_id)
        if existing is not None:
            mgr.update(connector_id, {"name": spec.get("name", ""), "auth_config": auth_config})
        else:
            created = mgr.create(
                str(spec.get("name", connector_id)),
                str(spec.get("type", "api_key")),
                auth_config,
                connector_id=connector_id,
            )
            if isinstance(created, dict) and created.get("connector_id"):
                connector_id = str(created["connector_id"])
            elif isinstance(created, dict) and created.get("status") == "error":
                errors.append(f"connector {connector_id}: {created.get('message')}")
                return
        registered["connectors"].append(connector_id)

    def _install_graph(self, spec: dict, registered: dict, errors: list[str]) -> None:
        if self._store is None:
            errors.append("graph facet requires a graph store")
            return
        from .graph import AgentGraph

        try:
            graph = AgentGraph.from_dict(spec)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"graph invalid: {e}")
            return
        self._store.save_graph(graph)
        registered["graphs"].append(graph.id)
        logger.info("bundle graph installed id=%s nodes=%d", graph.id, len(graph.nodes))

    async def _install_mcp(self, spec: dict, registered: dict, errors: list[str]) -> None:
        if self._mcp_register is None:
            errors.append("mcp facet requires an MCP registrar")
            return
        try:
            result = await self._mcp_register(dict(spec))
        except Exception as e:
            errors.append(f"mcp server: {e}")
            return
        if isinstance(result, dict) and (result.get("error") or result.get("status") == "error"):
            errors.append(f"mcp server: {result.get('error') or result.get('message')}")
            return
        key = str(spec.get("server_url") or spec.get("sse_url") or spec.get("post_url") or "")
        registered["mcp_servers"].append(key or str(spec.get("stdio_cmd", "")))

    async def install(self, path: str | Path) -> dict:
        manifest = load_bundle_manifest(path)
        if manifest is None:
            return {"ok": False, "error": f"bundle manifest not found under {path}"}
        if not manifest.name:
            manifest.name = Path(path).resolve().name
        if not manifest.is_bundle:
            return {"ok": False, "error": "manifest has no bundle facets"}
        self._manifest_root = manifest.root
        registered: dict[str, list] = {
            "agents": [],
            "graphs": [],
            "skills": [],
            "hooks": [],
            "connectors": [],
            "mcp_servers": [],
        }
        errors: list[str] = []
        for spec in manifest.agent_entries:
            self._install_agent(spec, registered, errors)
        for spec in manifest.skills:
            self._install_skill(spec, registered, errors)
        for spec in manifest.hooks:
            self._install_hook(spec, registered, errors)
        for spec in manifest.connectors:
            self._install_connector(spec, registered, errors)
        if manifest.graph:
            self._install_graph(manifest.graph, registered, errors)
        for spec in manifest.mcp_servers:
            await self._install_mcp(spec, registered, errors)
        if errors:
            logger.warning(
                "bundle install failed name=%s errors=%s, rolling back",
                manifest.name,
                errors,
            )
            rollback = await self.uninstall_facets(registered)
            return {
                "ok": False,
                "name": manifest.name,
                "errors": errors,
                "rolled_back": rollback,
            }
        record = {
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "path": str(Path(path).resolve()),
            "installed_at": time.time(),
            "facets": registered,
        }
        self._write_registry(record)
        logger.info(
            "bundle installed name=%s agents=%d graphs=%d skills=%d hooks=%d connectors=%d mcp=%d",
            manifest.name,
            len(registered["agents"]),
            len(registered["graphs"]),
            len(registered["skills"]),
            len(registered["hooks"]),
            len(registered["connectors"]),
            len(registered["mcp_servers"]),
        )
        return {"ok": True, "name": manifest.name, "facets": registered, "errors": []}

    async def uninstall_facets(self, registered: dict, name: str = "") -> dict:
        removed: dict[str, list] = {k: [] for k in registered}
        errors: list[str] = []
        for server in registered.get("mcp_servers", []):
            if not server:
                continue
            try:
                if self._mcp_unregister is not None:
                    await self._mcp_unregister(str(server))
                removed["mcp_servers"].append(server)
            except Exception as e:
                errors.append(f"mcp {server}: {e}")
        for connector_id in registered.get("connectors", []):
            try:
                self._connector_mgr().delete(connector_id)
                removed["connectors"].append(connector_id)
            except Exception as e:
                errors.append(f"connector {connector_id}: {e}")
        hooks = self._hooks()
        for hook_dict in registered.get("hooks", []):
            try:
                bucket = hooks._hooks.get(str(hook_dict.get("event", "")), [])
                before = len(bucket)
                hooks._hooks[str(hook_dict.get("event", ""))] = [
                    h for h in bucket if h.to_dict() != hook_dict
                ]
                if len(hooks._hooks[str(hook_dict.get("event", ""))]) != before:
                    removed["hooks"].append(hook_dict)
            except Exception as e:
                errors.append(f"hook {hook_dict}: {e}")
        for skill_name in registered.get("skills", []):
            try:
                if self._skills().uninstall_skill(skill_name):
                    removed["skills"].append(skill_name)
            except Exception as e:
                errors.append(f"skill {skill_name}: {e}")
        for graph_id in registered.get("graphs", []):
            try:
                if self._store is not None and self._store.delete_graph(graph_id):
                    removed["graphs"].append(graph_id)
            except Exception as e:
                errors.append(f"graph {graph_id}: {e}")
        for agent_id in registered.get("agents", []):
            try:
                shutil.rmtree(self.agents_dir / agent_id, ignore_errors=True)
                index_path = self.agents_dir / "index.json"
                if index_path.exists():
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    index.pop(agent_id, None)
                    index_path.write_text(
                        json.dumps(index, indent=4, ensure_ascii=False), encoding="utf-8"
                    )
                removed["agents"].append(agent_id)
            except Exception as e:
                errors.append(f"agent {agent_id}: {e}")
        logger.info(
            "bundle facets removed name=%s counts=%s errors=%d",
            name or "-",
            {k: len(v) for k, v in removed.items()},
            len(errors),
        )
        return {"removed": removed, "errors": errors}

    async def uninstall(self, name: str) -> dict:
        record = self.get(name)
        if record is None:
            return {"ok": False, "error": f"bundle not installed: {name}"}
        result = await self.uninstall_facets(record.get("facets") or {}, name=name)
        if result["errors"]:
            return {"ok": False, "name": name, **result}
        self._registry_path(name).unlink(missing_ok=True)
        logger.info("bundle uninstalled name=%s", name)
        return {"ok": True, "name": name, **result}
