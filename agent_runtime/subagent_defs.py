from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

MEMORY_SCOPES = ("", "user", "project", "local")
EFFORT_LEVELS = ("", "low", "medium", "high", "xhigh", "max")

DEFAULT_USER_AGENTS_DIR = str(Path.home() / ".fusion-agent-studio" / "agents")


@dataclass
class AgentDefinition:
    name: str
    description: str = ""
    prompt: str = ""
    tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str = ""
    effort: str = ""
    memory: str = ""
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    max_turns: int = 8
    background: bool = False
    permission_mode: str = ""
    auto_compact_window: int = 0
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "tools": list(self.tools),
            "disallowedTools": list(self.disallowed_tools),
            "model": self.model,
            "effort": self.effort,
            "memory": self.memory,
            "mcpServers": list(self.mcp_servers),
            "skills": list(self.skills),
            "maxTurns": self.max_turns,
            "background": self.background,
            "permissionMode": self.permission_mode,
            "autoCompactWindow": self.auto_compact_window,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentDefinition:
        def pick(*keys, default=None):
            for key in keys:
                if key in data and data[key] is not None:
                    return data[key]
            return default

        def as_list(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return [str(v) for v in value]

        try:
            max_turns = int(pick("maxTurns", "max_turns", default=8))
        except (TypeError, ValueError):
            logger.warning("agent definition maxTurns invalid, defaulting to 8")
            max_turns = 8
        try:
            window = int(pick("autoCompactWindow", "auto_compact_window", default=0))
        except (TypeError, ValueError):
            logger.warning("agent definition autoCompactWindow invalid, defaulting to 0")
            window = 0
        memory = str(pick("memory", default="") or "")
        if memory not in MEMORY_SCOPES:
            logger.warning("agent definition memory scope %r unknown, disabling", memory)
            memory = ""
        effort = str(pick("effort", default="") or "")
        if effort and effort not in EFFORT_LEVELS:
            logger.warning("agent definition effort %r unknown, disabling", effort)
            effort = ""
        return cls(
            name=str(pick("name", default="") or ""),
            description=str(pick("description", default="") or ""),
            prompt=str(pick("prompt", "system_prompt", "systemPrompt", default="") or ""),
            tools=as_list(pick("tools", "allowedTools", "allowed_tools")),
            disallowed_tools=as_list(pick("disallowedTools", "disallowed_tools")),
            model=str(pick("model", default="") or ""),
            effort=effort,
            memory=memory,
            mcp_servers=as_list(pick("mcpServers", "mcp_servers")),
            skills=as_list(pick("skills")),
            max_turns=max_turns,
            background=bool(pick("background", default=False)),
            permission_mode=str(pick("permissionMode", "permission_mode", default="") or ""),
            auto_compact_window=window,
            source=str(pick("source", default="") or ""),
        )

    def to_spec(self, task: str = ""):
        from .subagent import SubAgentSpec

        return SubAgentSpec(
            name=self.name,
            task=task,
            model=self.model,
            tools=list(self.tools),
            system_prompt=self.prompt,
            max_turns=self.max_turns,
            effort=self.effort,
            memory_scope=self.memory,
            auto_compact_window=self.auto_compact_window,
            background=self.background,
            disallowed_tools=list(self.disallowed_tools),
            mcp_servers=list(self.mcp_servers),
            skills=list(self.skills),
            permission_mode=self.permission_mode,
        )


def parse_agent_md(content: str, fallback_name: str = "") -> AgentDefinition:
    name = fallback_name
    meta: dict = {}
    body = content
    m = _FRONTMATTER_RE.match(content)
    if m:
        body = content[m.end():]
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            logger.warning("agent definition frontmatter parse failed name=%s: %s", fallback_name, e)
            loaded = {}
        if isinstance(loaded, dict):
            meta = loaded
    if meta.get("name"):
        name = str(meta["name"])
    meta = dict(meta)
    meta["name"] = name
    definition = AgentDefinition.from_dict(meta)
    definition.prompt = body.strip() or definition.prompt
    return definition


class AgentDefinitionRegistry:
    def __init__(self):
        self._defs: dict[str, AgentDefinition] = {}
        logger.info("agent definition registry init")

    def register(self, definition: AgentDefinition) -> AgentDefinition:
        if not definition.name:
            raise ValueError("AgentDefinition.name is required")
        self._defs[definition.name] = definition
        logger.info(
            "agent definition registered name=%s source=%s tools=%d deny=%d memory=%s",
            definition.name,
            definition.source or "programmatic",
            len(definition.tools),
            len(definition.disallowed_tools),
            definition.memory or "-",
        )
        return definition

    def get(self, name: str) -> AgentDefinition | None:
        return self._defs.get(name)

    def list(self) -> list[dict]:
        return [self._defs[k].to_dict() for k in sorted(self._defs)]

    def unregister(self, name: str) -> bool:
        return self._defs.pop(name, None) is not None

    def clear(self) -> None:
        self._defs.clear()

    def load_dir(self, directory: str | Path, source: str = "") -> int:
        base = Path(directory)
        if not base.is_dir():
            return 0
        loaded = 0
        for path in sorted(base.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("agent definition read failed path=%s: %s", path, e)
                continue
            definition = parse_agent_md(text, fallback_name=path.stem)
            if not definition.name:
                logger.warning("agent definition without name skipped path=%s", path)
                continue
            definition.source = source or str(path)
            self.register(definition)
            loaded += 1
        logger.info(
            "agent definitions loaded dir=%s source=%s count=%d", base, source or "-", loaded
        )
        return loaded

    def discover(self, project_dir: str = "", extra_dirs: list[str] | None = None) -> int:
        dirs: list[tuple[str, str]] = []
        project = project_dir or os.getcwd()
        dirs.append((str(Path(project) / ".claude" / "agents"), "claude-compat"))
        dirs.append((DEFAULT_USER_AGENTS_DIR, "user"))
        dirs.append((str(Path(project) / ".fusion-agent-studio" / "agents"), "project"))
        for d in extra_dirs or []:
            dirs.append((d, "extra"))
        total = 0
        for directory, source in dirs:
            total += self.load_dir(directory, source=source)
        logger.info("agent definition discovery done count=%d dirs=%d", total, len(dirs))
        return total


_registry_singleton: AgentDefinitionRegistry | None = None


def get_definition_registry() -> AgentDefinitionRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = AgentDefinitionRegistry()
        _registry_singleton.discover()
    return _registry_singleton
