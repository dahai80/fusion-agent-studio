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
# the skills.invoke RPC which runs the same ToolFunnel admission chain
# before touching the skills store (#476).
GOVERNANCE_CHAIN = ("hooks", "permission", "budget")

X_FUSION_PREFIX = "x-fusion-"


def read_extensions(fields) -> dict:
    # N17 #390: read x-fusion-* extension fields from an MCP tool schema or
    # header map. Missing/unknown fields degrade to an empty dict.
    out: dict = {}
    if not fields:
        return out
    items = fields.items() if hasattr(fields, "items") else ()
    for key, value in items:
        lowered = str(key).lower()
        if lowered.startswith(X_FUSION_PREFIX):
            out[lowered[len(X_FUSION_PREFIX):]] = value
    return out


def write_extensions(extensions: dict | None) -> dict:
    # N17 #390: publish fusion extensions without forking the wire format.
    return {
        f"{X_FUSION_PREFIX}{key}": value
        for key, value in (extensions or {}).items()
        if value is not None
    }


def merge_headers(headers: dict | None, extensions: dict | None) -> dict:
    # Explicit x-fusion-* headers win over derived extensions.
    merged = write_extensions(extensions)
    merged.update(headers or {})
    return merged


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

    def ensure(self, kind: str, name: str, origin: str = "",
               readonly: bool = False, extensions: dict | None = None) -> ProtocolArtifact:
        # N17 #390: execution entry points call ensure() so every runnable
        # artifact is registered on the governance plane before it executes.
        art = self.get(kind, name)
        if art is None:
            return self.register(
                kind=kind, name=name, origin=origin,
                readonly=readonly, extensions=extensions or {},
            )
        if extensions:
            art.extensions.update(extensions)
        if origin and not art.origin:
            art.origin = origin
        return art

    def require(self, kind: str, name: str) -> ProtocolArtifact:
        # Strict mode (FUSION_PROTOCOL_STRICT=1) refuses unregistered
        # artifacts; default mode auto-registers at the owning layer.
        import os

        strict = os.environ.get("FUSION_PROTOCOL_STRICT", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        art = self.get(kind, name)
        if art is None and strict:
            raise ValueError(f"protocol artifact not registered: {kind}/{name}")
        if art is None:
            art = self.ensure(kind, name)
        return art

    def check_chain(self, hooks=None, permissions=None, budget=None) -> dict:
        # N17 #390: report which governance stages are actually armed.
        present = {
            "hooks": hooks is not None,
            "permission": permissions is not None,
            "budget": budget is not None,
        }
        return {
            "chain": list(GOVERNANCE_CHAIN),
            "armed": present,
            "missing": [name for name in GOVERNANCE_CHAIN if not present[name]],
        }

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
