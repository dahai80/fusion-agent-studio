"""Anthropic-style YAML -> FAS AgentDefinition compiler (#437 / E1, §4).

Port of f-ecommerce agent_platform/yaml_compiler.py — the customer-facing
authoring contract is the say-what-not-how YAML; FAS consumes flat
AgentDefinition JSON. This module compiles one to the other with honest,
line-numbered errors and NEVER grants authority from the YAML: permissions
are derived from the publisher's role at publish time (door + publish
registry), not from the document.

Generalization vs the f-ecommerce original:
- model.id: base | tuned — `tuned` resolves to a registered fine-tuned
  model (trainer publish, E4) at run time; vendor model strings rejected.
- tool endpoints point at THIS studio's single audited door
  (/v1/agent-platform/call), not a remote console proxy.

Compile rules (§4-A):
  name, description  -> passthrough (trimmed)
  model.effort       -> temperature tier: low 0.2 / medium 0.5 / high 0.7
                         (FAS AgentModelConfig has no effort field — we map,
                         not extend; honest on a 4B model)
  model.id           -> base | tuned (kept as-is for the runtime to
                         resolve)
  tools[]            -> must be capability ids in the spine registry;
                         each becomes an AgentToolConfig pointing at the
                         single audited door
  system             -> system_prompt passthrough
  metadata.template  -> lineage marker for seeded agents
"""

from __future__ import annotations

from typing import Any, Dict, List

import yaml

from .capabilities import get_capability

_EFFORT_TEMPERATURE = {"low": 0.2, "medium": 0.5, "high": 0.7}
_MODEL_IDS = ("base", "tuned")
_DOOR_PATH = "/v1/agent-platform/call"


class YamlCompileError(ValueError):
    """Raised with a customer-presentable message (no stack traces)."""


def compile_yaml(text: str) -> Dict[str, Any]:
    """Compile authoring YAML to (definition, warnings). Raises
    YamlCompileError with a `line N:` prefix on any customer mistake."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark else None
        raise YamlCompileError(
            f"line {line}: invalid YAML: {e.problem or str(e)[:120]}"
            if line else f"invalid YAML: {str(e)[:160]}") from e
    if not isinstance(doc, dict):
        raise YamlCompileError("document must be a YAML mapping "
                               "(name:, model:, system:, tools:…)")
    return _compile_doc(doc)


def compile_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Compile an already-parsed mapping (form tab round-trips here)."""
    return _compile_doc(doc)


def _compile_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []

    name = str(doc.get("name") or "").strip()
    if not name:
        errors.append("name: is required")
    system = str(doc.get("system") or "").strip()
    if not system:
        errors.append("system: is required — say what the agent is and "
                      "how it should behave")

    description = str(doc.get("description") or "").strip()

    # model -> temperature tier + runtime model id
    model = doc.get("model") or {}
    if not isinstance(model, dict):
        errors.append("model: must be a mapping (id:, effort:)")
        model = {}
    model_id = str(model.get("id") or "base").strip()
    if model_id not in _MODEL_IDS:
        errors.append(f"model.id: '{model_id}' is not one of "
                      f"{', '.join(_MODEL_IDS)}")
    effort = str(model.get("effort") or "low").strip().lower()
    if effort not in _EFFORT_TEMPERATURE:
        errors.append(f"model.effort: '{effort}' is not one of "
                      f"{', '.join(_EFFORT_TEMPERATURE)}")

    # tools -> capability ids -> AgentToolConfig list
    tools_in = doc.get("tools") or []
    if not isinstance(tools_in, list):
        errors.append("tools: must be a list of capability ids")
        tools_in = []
    tool_defs: List[Dict[str, Any]] = []
    seen: set = set()
    for i, entry in enumerate(tools_in):
        if isinstance(entry, str):
            cap_id, params = entry.strip(), {}
        elif isinstance(entry, dict) and entry.get("id"):
            cap_id = str(entry["id"]).strip()
            params = entry.get("config") or {}
            if not isinstance(params, dict):
                params = {}
        else:
            errors.append(f"tools[{i}]: each tool is a capability id "
                          f"string, or a mapping with id/config keys")
            continue
        if cap_id in seen:
            warnings.append(f"tools: '{cap_id}' listed twice — deduped")
            continue
        seen.add(cap_id)
        cap = get_capability(cap_id)
        if cap is None:
            errors.append(f"tools[{i}]: unknown capability '{cap_id}' — "
                          f"pick ids from GET /v1/agent-platform/capabilities")
            continue
        if cap.get("status") != "active":
            errors.append(f"tools[{i}]: capability '{cap_id}' is disabled")
            continue
        tool_defs.append({
            "name": cap_id,
            "type": "http",
            "description": cap.get("description") or cap.get("title"),
            "endpoint": _DOOR_PATH,
            "config": {
                "capability_id": cap_id,
                "mutation": bool(cap.get("mutation")),
                "params": params if isinstance(params, dict) else {},
                # Full param schema embedded AT COMPILE TIME (local
                # registry): FAS registers the tool with zero network
                # dependency — the manifest is the contract (§5.5).
                "cap_params": (cap.get("params") or {}).get("properties")
                              or {},
                "cap_mutation": bool(cap.get("mutation")),
                "cap_title": cap.get("title") or cap_id,
            },
        })

    # metadata.template passthrough (lineage for seeded agents)
    meta = doc.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    template = str(meta.get("template") or "").strip() or None

    if errors:
        raise YamlCompileError(" | ".join(errors))

    definition: Dict[str, Any] = {
        "name": name,
        "description": description,
        "system_prompt": system,
        "model": {
            "provider": "fusion-mlx",
            "name": model_id,  # resolved at run time (base/tuned)
            "temperature": _EFFORT_TEMPERATURE[effort],
            "max_tokens": 2048,
        },
        "tools": tool_defs,
        "permissions": {
            # Never authority (§9): the real gate is the door's
            # publisher-role ∩ write-class check. These flags only
            # describe intent for the FAS UI.
            "read": True,
            "write": any(t["config"]["mutation"] for t in tool_defs),
        },
        "status": "draft",
    }
    if template:
        definition["metadata"] = {"template": template}
    # model_slot preserves the author's base|tuned choice across the FAS
    # round-trip (FAS stores the RESOLVED concrete model id; the slot is
    # ours). decompile() prefers this over reverse-matching ids.
    definition.setdefault("metadata", {})["model_slot"] = model_id
    return {"definition": definition, "warnings": warnings}


def decompile(definition: Dict[str, Any]) -> str:
    """Round-trip an existing FAS definition back to authoring YAML
    (editor Form<->YAML one-contract guarantee)."""
    model = definition.get("model") or {}
    if isinstance(model, str):
        # FAS test-created agents store a bare model-id string, not the
        # {name, temperature, max_tokens} dict our compiler produces.
        model = {"name": model}
    temp = float(model.get("temperature") or 0.2)
    effort = min(_EFFORT_TEMPERATURE.items(),
                 key=lambda kv: abs(kv[1] - temp))[0]
    meta = definition.get("metadata") or {}
    model_id = meta.get("model_slot") or model.get("name") or "base"
    if model_id not in _MODEL_IDS:
        model_id = "base"
    tools = [t.get("name") if isinstance(t, dict) else str(t)
             for t in (definition.get("tools") or [])
             if (t.get("name") if isinstance(t, dict) else str(t))]
    doc: Dict[str, Any] = {
        "name": definition.get("name") or "",
        "description": definition.get("description") or "",
        "model": {"id": model_id, "effort": effort},
        "system": definition.get("system_prompt") or "",
        "tools": tools,
    }
    if meta.get("template"):
        doc["metadata"] = {"template": meta["template"]}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                          width=100)
