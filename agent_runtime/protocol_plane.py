"""N17 protocol plane (#356): MCP / Skills / Plugin three-layer model.

Relationship model:
    MCP    = connection layer  (real-time tool/context protocol)
    Skills = knowledge layer   (SKILL.md directory format)
    Plugin = distribution container (packages the above + config)

Every protocol artifact lands in the ToolRegistry / skills store / plugin
manager, so all execution flows through the same funnels
(hooks -> permission -> budget). No bypass path exists.

Zero-fork commitment: industry packages install unmodified; fusion
enhancements ride x-fusion-* extension fields only.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PROTOCOL_KINDS = ("mcp", "skill", "plugin")

# Governance surface shared by all three kinds. Tool-bearing artifacts
# (MCP tools, plugin tools) execute via ToolFunnel; skills execute via
# skill.execute which is itself funnel/permission-gated at the runtime.
GOVERNANCE_CHAIN = ("hooks", "permission", "budget")


@dataclass
class ProtocolArtifact:
    kind: str
    name: str
    origin: str = ""
    readonly: bool = False
    registered_at: float = 0.0
    extensions: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in PROTOCOL_KINDS:
            raise ValueError(f"unknown protocol kind: {self.kind}")
        if not self.registered_at:
            self.registered_at = time.time()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "origin": self.origin,
            "readonly": self.readonly,
            "registered_at": self.registered_at,
            "extensions": dict(self.extensions),
        }


class ProtocolPlane:
    """Registry + governance routing for the three protocol layers."""

    def __init__(self):
        self._artifacts: dict[tuple[str, str], ProtocolArtifact] = {}

    def register(self, kind: str, name: str, origin: str = "", readonly: bool = False,
                 extensions: dict | None = None) -> ProtocolArtifact:
        art = ProtocolArtifact(
            kind=kind, name=name, origin=origin, readonly=readonly,
            extensions=extensions or {},
        )
        self._artifacts[(kind, name)] = art
        logger.info("protocol artifact registered kind=%s name=%s origin=%s", kind, name, origin)
        return art

    def unregister(self, kind: str, name: str) -> bool:
        return self._artifacts.pop((kind, name), None) is not None

    def get(self, kind: str, name: str) -> ProtocolArtifact | None:
        return self._artifacts.get((kind, name))

    def list(self, kind: str = "") -> list[dict]:
        rows = [a.to_dict() for a in self._artifacts.values()]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        return sorted(rows, key=lambda r: (r["kind"], r["name"]))

    def governance_route(self, kind: str, name: str) -> dict:
        art = self.get(kind, name)
        return {
            "kind": kind,
            "name": name,
            "registered": art is not None,
            "chain": list(GOVERNANCE_CHAIN),
            "origin": art.origin if art else "",
            "extensions": dict(art.extensions) if art else {},
        }
