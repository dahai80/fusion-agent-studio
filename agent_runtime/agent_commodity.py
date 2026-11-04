"""Agent commodity standard (#570) — capability/model separation.

An agent definition is a commercial commodity: capability fields (prompt
pack, tool allowlist, KB binding, version, eval set reference, docs pack)
are the product; the model binding is a separate replaceable field. One
commodity ships dual SKUs (local-model edition / Pro cloud edition) that
differ ONLY in model_bindings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

COMMODITY_SCHEMA_VERSION = "1.0.0"
DOC_FIVE_PACK = ("readme", "quickstart", "capability_spec", "eval_report", "changelog")
DOC_FIVE_PACK_CN = {
    "readme": "README",
    "quickstart": "Quickstart",
    "capability_spec": "Capability spec",
    "eval_report": "Eval report",
    "changelog": "Changelog",
}
SKU_LOCAL = "local"
SKU_PRO = "pro"


@dataclass
class ModelBinding:
    sku: str = SKU_LOCAL
    provider: str = "mlx"
    model_name: str = ""
    context_window: int = 32768
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "provider": self.provider,
            "model_name": self.model_name,
            "context_window": self.context_window,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelBinding:
        return cls(
            sku=str(data.get("sku", SKU_LOCAL) or SKU_LOCAL),
            provider=str(data.get("provider", "mlx") or "mlx"),
            model_name=str(data.get("model_name", "") or ""),
            context_window=int(data.get("context_window", 32768) or 32768),
            note=str(data.get("note", "") or ""),
        )


@dataclass
class AgentCommodity:
    agent_id: str = ""
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    schema_version: str = COMMODITY_SCHEMA_VERSION
    system_prompt: str = ""
    tool_allowlist: list[str] = field(default_factory=list)
    kb_binding: dict[str, Any] = field(default_factory=dict)
    eval_set_ref: str = ""
    docs: dict[str, str] = field(default_factory=dict)
    model_bindings: dict[str, ModelBinding] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    author: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capability": {
                "system_prompt": self.system_prompt,
                "tool_allowlist": list(self.tool_allowlist),
                "kb_binding": dict(self.kb_binding),
                "eval_set_ref": self.eval_set_ref,
                "docs": dict(self.docs),
            },
            "model_bindings": {k: v.to_dict() for k, v in self.model_bindings.items()},
            "tags": list(self.tags),
            "author": self.author,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentCommodity:
        cap = data.get("capability") or {}
        flat = not cap
        src = cap if isinstance(cap, dict) and cap else data
        bindings_raw = data.get("model_bindings") or {}
        bindings = {
            str(k): ModelBinding.from_dict(v if isinstance(v, dict) else {})
            for k, v in bindings_raw.items()
        }
        docs_raw = src.get("docs") or {}
        return cls(
            schema_version=str(data.get("schema_version", COMMODITY_SCHEMA_VERSION)),
            agent_id=str(data.get("agent_id", "") or ""),
            name=str(data.get("name", "") or ""),
            version=str(data.get("version", "0.1.0") or "0.1.0"),
            description=str(data.get("description", "") or ""),
            system_prompt=str(src.get("system_prompt", "") or ""),
            tool_allowlist=[str(t) for t in (src.get("tool_allowlist") or [])],
            kb_binding=dict(src.get("kb_binding") or {}),
            eval_set_ref=str(src.get("eval_set_ref", "") or ""),
            docs={str(k): str(v) for k, v in docs_raw.items()},
            model_bindings=bindings,
            tags=[str(t) for t in (data.get("tags") or [])],
            author=str(data.get("author", "") or ""),
        )

    def skus(self) -> list[str]:
        return sorted(self.model_bindings)

    def with_model_binding(self, sku: str, binding: ModelBinding) -> "AgentCommodity":
        clone = AgentCommodity.from_dict(self.to_dict())
        clone.model_bindings[sku] = binding
        return clone

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("name is required")
        if not self.version:
            errors.append("version is required")
        if not self.system_prompt:
            errors.append("capability.system_prompt (prompt pack) is required")
        if not self.tool_allowlist:
            errors.append("capability.tool_allowlist is required")
        if not self.eval_set_ref:
            errors.append("capability.eval_set_ref is required")
        if not self.model_bindings:
            errors.append("model_bindings: at least one SKU binding is required")
        errors.extend(validate_docs_pack(self.docs))
        return errors

    def capability_fingerprint(self) -> str:
        import hashlib
        import json

        cap = self.to_dict()["capability"]
        blob = json.dumps(cap, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def validate_docs_pack(docs: dict[str, str]) -> list[str]:
    missing = [key for key in DOC_FIVE_PACK if not str(docs.get(key, "") or "").strip()]
    return [f"docs five-pack missing item: {k} ({DOC_FIVE_PACK_CN[k]})" for k in missing]


def docs_pack_complete(docs: dict[str, str]) -> bool:
    return not validate_docs_pack(docs)


def build_commodity_from_reviewer_pack(pack_key: str) -> AgentCommodity | None:
    from agent_runtime.reviewer_packs import get_pack

    pack = get_pack(pack_key)
    if pack is None:
        return None
    agent_id = f"reviewer-{pack.key}"
    docs = {
        "readme": f"docs/agents/{agent_id}/README.md",
        "quickstart": f"docs/agents/{agent_id}/QUICKSTART.md",
        "capability_spec": f"docs/agents/{agent_id}/CAPABILITY_SPEC.md",
        "eval_report": f"docs/agents/{agent_id}/EVAL_REPORT.md",
        "changelog": f"docs/agents/{agent_id}/CHANGELOG.md",
    }
    return AgentCommodity(
        agent_id=agent_id,
        name=f"{pack.name} Agent",
        version="1.0.0",
        description=pack.description,
        system_prompt=pack.system_prompt,
        tool_allowlist=["artifact_get_source", "artifact_list_all"],
        kb_binding={"enable_rag": False, "kb_id": "", "top_k": 0},
        eval_set_ref=f"eval-sets/{agent_id}.json",
        docs=docs,
        model_bindings={},
        tags=["reviewer", "agent-commodity", pack.key],
    )


def all_reviewer_commodities() -> list[AgentCommodity]:
    from agent_runtime.reviewer_packs import REVIEWER_PACKS

    out = []
    for key in REVIEWER_PACKS:
        c = build_commodity_from_reviewer_pack(key)
        if c is not None:
            out.append(c)
    return out


def eval_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Eval Report — {report.get('agent_id', '')} v{report.get('version', '')}",
        "",
        f"- Model binding: `{report.get('model_name', '')}` ({report.get('sku', '')})",
        f"- Eval set: `{report.get('eval_set_ref', '')}`",
        f"- Pass rate: **{report.get('pass_rate', 0)}%** "
        f"({report.get('passed', 0)}/{report.get('total', 0)} cases)",
        "",
        "| case | expect | got | pass |",
        "|---|---|---|---|",
    ]
    for case in report.get("cases", []):
        expect = case.get("expect", "")
        got = case.get("got", "")
        ok = "yes" if case.get("passed") else "NO"
        lines.append(f"| {case.get('case_id', '')} | {expect} | {got} | {ok} |")
    limitations = report.get("known_limitations") or []
    if limitations:
        lines += ["", "## Known limitations"]
        lines += [f"- {lim}" for lim in limitations]
    lines.append("")
    return "\n".join(lines)
