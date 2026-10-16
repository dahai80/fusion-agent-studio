"""N16 persona agent.md stack (#355): layered persona merge with conflict report.

Layer precedence (low -> high): global < tenant < team < agent < run.
Each layer is an agent.md-style markdown file; merge is section-level —
a higher layer's "## Section" body overrides the lower layer's same-named
section, and every override is reported as an explicit conflict record.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PERSONA_ROOT = Path.home() / ".fusion-agent-studio" / "persona"

_LAYER_RE = re.compile(r"^(global|tenant|team)$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _layer_path(layer: str, name: str = "") -> Path:
    if layer == "global":
        return PERSONA_ROOT / "agent.md"
    if layer == "tenant":
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
        return PERSONA_ROOT / "tenants" / safe / "agent.md"
    if layer == "team":
        safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64]
        return PERSONA_ROOT / "teams" / safe / "agent.md"
    raise ValueError(f"unknown persona layer: {layer}")


def load_layer(layer: str, name: str = "") -> str:
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    p = _layer_path(layer, name)
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("persona layer read failed layer=%s: %s", layer, e)
    return ""


def save_layer(layer: str, content: str, name: str = "") -> Path:
    if not _LAYER_RE.match(layer):
        raise ValueError(f"unknown persona layer: {layer}")
    if layer != "global" and not name:
        raise ValueError(f"{layer} layer requires name")
    p = _layer_path(layer, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.info("persona layer saved layer=%s name=%s path=%s", layer, name, p)
    return p


def parse_sections(md: str) -> dict[str, str]:
    if not md:
        return {}
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(md))
    if not matches:
        return {"": md.strip()}
    preamble = md[: matches[0].start()].strip()
    if preamble:
        out[""] = preamble
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[header] = md[start:end].strip()
    return out


def merge_persona_layers(layers: list[tuple[str, str]]) -> dict:
    merged: dict[str, str] = {}
    conflicts: list[dict] = []
    applied: list[str] = []
    order: list[str] = []
    for layer_name, text in layers:
        if not text or not text.strip():
            continue
        applied.append(layer_name)
        sections = parse_sections(text)
        for header, body in sections.items():
            if header in merged and merged[header].strip() != body.strip():
                conflicts.append(
                    {"section": header or "(preamble)", "winner": layer_name,
                     "loser": "lower layer"}
                )
            elif header in merged:
                conflicts.append(
                    {"section": header or "(preamble)", "winner": layer_name,
                     "loser": "lower layer (identical)"}
                )
            if header not in order:
                order.append(header)
            merged[header] = body
    parts = []
    for header in order:
        if header == "":
            parts.append(merged[header])
        else:
            parts.append(f"## {header}\n\n{merged[header]}")
    persona = "\n\n".join(parts).strip()
    return {"persona": persona, "layers_applied": applied, "conflicts": conflicts}


def resolve_persona(
    agent_id: str = "",
    tenant_id: str = "",
    team_id: str = "",
    run_override: str = "",
    fallback: str = "",
    agents_root: Path | None = None,
) -> dict:
    stack: list[tuple[str, str]] = [("global", load_layer("global"))]
    if tenant_id:
        stack.append((f"tenant:{tenant_id}", load_layer("tenant", tenant_id)))
    if team_id:
        stack.append((f"team:{team_id}", load_layer("team", team_id)))
    agent_md = _load_agent_soul(agent_id, agents_root=agents_root)
    if agent_md:
        stack.append((f"agent:{agent_id}", agent_md))
    if run_override:
        stack.append(("run", run_override))
    result = merge_persona_layers(stack)
    if not result["persona"]:
        result["persona"] = fallback
    return result


def _load_agent_soul(agent_id: str, agents_root: Path) -> str:
    if not agent_id:
        return ""
    try:
        from .agent_package import AGENTS_ROOT, AgentPackage

        base = agents_root or AGENTS_ROOT
        pkg = AgentPackage(base / agent_id)
        if pkg.exists:
            return pkg.load_soul() or ""
    except Exception as e:
        logger.warning("persona agent soul load failed agent=%s: %s", agent_id, e)
    return ""
