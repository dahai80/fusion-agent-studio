"""Agent Definition JSON Schema v1 — standardized cross-project contract.

Provides schema definition, validation, and serialization for agent definitions.
Used by REST API (#29), cowork IPC (#36), and context injection (#37).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_URI = "https://fusion.dev/agent-definition-v1.json"
SCHEMA_VERSION = "1.0.0"


@dataclass
class AgentModelConfig:
    provider: str = "mlx"
    model_name: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    context_window: int = 32768

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "context_window": self.context_window,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentModelConfig:
        return cls(
            provider=data.get("provider", "mlx"),
            model_name=data.get("model_name", ""),
            temperature=data.get("temperature", 0.7),
            max_tokens=data.get("max_tokens", 4096),
            top_p=data.get("top_p", 1.0),
            context_window=data.get("context_window", 32768),
        )


@dataclass
class AgentToolConfig:
    name: str = ""
    type: str = "function"
    description: str = ""
    endpoint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "endpoint": self.endpoint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentToolConfig:
        return cls(
            name=data.get("name", ""),
            type=data.get("type", "function"),
            description=data.get("description", ""),
            endpoint=data.get("endpoint", ""),
        )


@dataclass
class AgentKnowledgeConfig:
    enable_rag: bool = False
    kb_id: str = ""
    top_k: int = 5
    strategy: str = "hybrid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "enable_rag": self.enable_rag,
            "kb_id": self.kb_id,
            "top_k": self.top_k,
            "strategy": self.strategy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentKnowledgeConfig:
        return cls(
            enable_rag=data.get("enable_rag", False),
            kb_id=data.get("kb_id", ""),
            top_k=data.get("top_k", 5),
            strategy=data.get("strategy", "hybrid"),
        )


@dataclass
class AgentOrchestrationConfig:
    chain_next: str = ""
    parallel_group: str = ""
    timeout_seconds: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_next": self.chain_next,
            "parallel_group": self.parallel_group,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentOrchestrationConfig:
        return cls(
            chain_next=data.get("chain_next", ""),
            parallel_group=data.get("parallel_group", ""),
            timeout_seconds=data.get("timeout_seconds", 300),
        )


@dataclass
class AgentMetadataConfig:
    author: str = ""
    tags: list[str] = field(default_factory=list)
    published_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "tags": self.tags,
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentMetadataConfig:
        return cls(
            author=data.get("author", ""),
            tags=data.get("tags", []),
            published_at=data.get("published_at"),
        )


@dataclass
class ContextInjectionConfig:
    mode: str = "full"
    recent_n: int = 50
    enable_rag: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "recent_n": self.recent_n,
            "enable_rag": self.enable_rag,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextInjectionConfig:
        return cls(
            mode=data.get("mode", "full"),
            recent_n=data.get("recent_n", 50),
            enable_rag=data.get("enable_rag", False),
        )


@dataclass
class ArtifactPolicyConfig:
    can_create: bool = True
    can_update: bool = True
    trigger_strategy: str = "auto"
    auto_create_threshold_lines: int = 20
    ownership_type: str = "project"
    default_folder: str = ""
    default_tags: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=lambda: [
        "artifact_get_source", "artifact_update",
        "artifact_create", "artifact_create_snapshot", "artifact_list_all",
    ])

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_create": self.can_create,
            "can_update": self.can_update,
            "trigger_strategy": self.trigger_strategy,
            "auto_create_threshold_lines": self.auto_create_threshold_lines,
            "ownership_type": self.ownership_type,
            "default_folder": self.default_folder,
            "default_tags": self.default_tags,
            "allowed_tools": self.allowed_tools,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactPolicyConfig:
        return cls(
            can_create=data.get("can_create", True),
            can_update=data.get("can_update", True),
            trigger_strategy=data.get("trigger_strategy", "auto"),
            auto_create_threshold_lines=data.get("auto_create_threshold_lines", 20),
            ownership_type=data.get("ownership_type", "project"),
            default_folder=data.get("default_folder", ""),
            default_tags=data.get("default_tags", []),
            allowed_tools=data.get("allowed_tools", [
                "artifact_get_source", "artifact_update",
                "artifact_create", "artifact_create_snapshot", "artifact_list_all",
            ]),
        )


@dataclass
class AgentDefinition:
    schema_ref: str = SCHEMA_URI
    schema_version: str = SCHEMA_VERSION
    agent_id: str = ""
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    system_prompt: str = ""
    model: AgentModelConfig = field(default_factory=AgentModelConfig)
    tools: list[AgentToolConfig] = field(default_factory=list)
    knowledge: AgentKnowledgeConfig = field(default_factory=AgentKnowledgeConfig)
    orchestration: AgentOrchestrationConfig = field(default_factory=AgentOrchestrationConfig)
    metadata: AgentMetadataConfig = field(default_factory=AgentMetadataConfig)
    context_injection: ContextInjectionConfig = field(default_factory=ContextInjectionConfig)
    artifact_policy: ArtifactPolicyConfig = field(default_factory=ArtifactPolicyConfig)
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": self.schema_ref,
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model.to_dict(),
            "tools": [t.to_dict() for t in self.tools],
            "knowledge": self.knowledge.to_dict(),
            "orchestration": self.orchestration.to_dict(),
            "metadata": self.metadata.to_dict(),
            "context_injection": self.context_injection.to_dict(),
            "artifact_policy": self.artifact_policy.to_dict(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentDefinition:
        return cls(
            schema_ref=data.get("$schema", SCHEMA_URI),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            agent_id=data.get("agent_id", ""),
            name=data.get("name", ""),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            system_prompt=data.get("system_prompt", ""),
            model=AgentModelConfig.from_dict(data.get("model", {})),
            tools=[AgentToolConfig.from_dict(t) for t in data.get("tools", [])],
            knowledge=AgentKnowledgeConfig.from_dict(data.get("knowledge", {})),
            orchestration=AgentOrchestrationConfig.from_dict(data.get("orchestration", {})),
            metadata=AgentMetadataConfig.from_dict(data.get("metadata", {})),
            context_injection=ContextInjectionConfig.from_dict(data.get("context_injection", {})),
            artifact_policy=ArtifactPolicyConfig.from_dict(data.get("artifact_policy", {})),
            status=data.get("status", "draft"),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("name is required")
        if not self.system_prompt:
            errors.append("system_prompt is required")
        if self.context_injection.mode not in ("full", "recent_n", "rag"):
            errors.append(f"invalid context_injection.mode: {self.context_injection.mode}")
        if self.artifact_policy.trigger_strategy not in ("auto", "fence", "always", "never"):
            errors.append(f"invalid artifact_policy.trigger_strategy: {self.artifact_policy.trigger_strategy}")
        if self.status not in ("draft", "published", "archived"):
            errors.append(f"invalid status: {self.status}")
        return errors

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> AgentDefinition:
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_manifest(cls, manifest_dict: dict[str, Any], agent_id: str = "") -> AgentDefinition:
        model_cfg = AgentModelConfig(
            model_name=manifest_dict.get("model", ""),
            temperature=manifest_dict.get("temperature", 0.7),
            max_tokens=manifest_dict.get("max_tokens", 4096),
            top_p=manifest_dict.get("top_p", 1.0),
            context_window=manifest_dict.get("context_window", 32768),
        )
        knowledge_cfg = AgentKnowledgeConfig(
            enable_rag=bool(manifest_dict.get("knowledge_base_ids", [])),
            kb_id=",".join(manifest_dict.get("knowledge_base_ids", [])),
            strategy=manifest_dict.get("rag_strategy", "hybrid"),
        )
        tools_cfg = [AgentToolConfig(name=t) for t in manifest_dict.get("tools", [])]
        meta_cfg = AgentMetadataConfig(
            author=manifest_dict.get("author", ""),
            tags=manifest_dict.get("tags", []),
            published_at=manifest_dict.get("published_at"),
        )
        orch_cfg = AgentOrchestrationConfig()
        ctx_cfg = ContextInjectionConfig()
        art_cfg = ArtifactPolicyConfig()
        return cls(
            agent_id=agent_id,
            name=manifest_dict.get("name", ""),
            version=manifest_dict.get("version", "0.1.0"),
            description=manifest_dict.get("description", ""),
            system_prompt=manifest_dict.get("system_prompt", ""),
            model=model_cfg,
            tools=tools_cfg,
            knowledge=knowledge_cfg,
            orchestration=orch_cfg,
            metadata=meta_cfg,
            context_injection=ctx_cfg,
            artifact_policy=art_cfg,
            status=manifest_dict.get("status", "draft"),
        )
