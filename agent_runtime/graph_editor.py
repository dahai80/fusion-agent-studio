"""Graph editor backend — validation, auto-layout, serialization for DAG visual editor.

Provides graph validation (cycle detection, orphan nodes, type checking),
auto-layout (topological sort + layered positioning), and enhanced CRUD.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.graph import AgentGraph

logger = logging.getLogger(__name__)

NODE_TYPES = {"start", "llm", "tool", "condition", "loop", "end", "error_handler"}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "start": {"llm", "tool", "condition", "end"},
    "llm": {"llm", "tool", "condition", "end", "error_handler"},
    "tool": {"llm", "tool", "condition", "end", "error_handler"},
    "condition": {"llm", "tool", "condition", "loop", "end"},
    "loop": {"llm", "tool", "condition", "end"},
    "error_handler": {"llm", "tool", "end"},
    "end": set(),
}


@dataclass
class ValidationIssue:
    severity: str  # "error", "warning", "info"
    node_id: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "node_id": self.node_id,
            "message": self.message,
        }


@dataclass
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class NodePosition:
    node_id: str
    x: float
    y: float
    width: float = 200.0
    height: float = 80.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


def validate_graph(graph: AgentGraph) -> ValidationResult:
    """Validate a graph for structural correctness."""
    issues: list[ValidationIssue] = []
    nodes = dict(graph.nodes)
    node_ids = set(nodes.keys())

    start_nodes = [nid for nid, n in nodes.items() if n.type == "start"]
    if len(start_nodes) == 0:
        issues.append(ValidationIssue("error", "", "No start node found"))
    elif len(start_nodes) > 1:
        issues.append(ValidationIssue("error", start_nodes[1], "Multiple start nodes"))

    end_nodes = [nid for nid, n in nodes.items() if n.type == "end"]
    if len(end_nodes) == 0:
        issues.append(ValidationIssue("warning", "", "No end node found"))

    for edge in graph.edges:
        if edge.source_id not in node_ids:
            issues.append(
                ValidationIssue(
                    "error", edge.source_id, f"Edge source '{edge.source_id}' not found"
                )
            )
            continue
        if edge.target_id not in node_ids:
            issues.append(
                ValidationIssue(
                    "error", edge.target_id, f"Edge target '{edge.target_id}' not found"
                )
            )
            continue

        src_type = nodes[edge.source_id].type
        tgt_type = nodes[edge.target_id].type
        if tgt_type not in VALID_TRANSITIONS.get(src_type, set()):
            issues.append(
                ValidationIssue(
                    "warning",
                    edge.source_id,
                    f"Unusual transition: {src_type} -> {tgt_type}",
                )
            )

    if _has_cycle(graph):
        issues.append(ValidationIssue("error", "", "Graph contains a cycle"))

    reachable = _reachable_nodes(graph)
    orphan_nodes = node_ids - reachable
    for nid in orphan_nodes:
        n = nodes[nid]
        if n.type != "start":
            issues.append(ValidationIssue("warning", nid, "Unreachable node"))

    cond_nodes = [nid for nid, n in nodes.items() if n.type == "condition"]
    for cn_id in cond_nodes:
        outgoing = [e for e in graph.edges if e.source_id == cn_id]
        if len(outgoing) < 2:
            issues.append(
                ValidationIssue(
                    "warning", cn_id, "Condition node has fewer than 2 outgoing edges"
                )
            )

    valid = not any(i.severity == "error" for i in issues)
    logger.info(
        "Graph validation: %s (%d issues)", "PASS" if valid else "FAIL", len(issues)
    )
    return ValidationResult(valid=valid, issues=issues)


def _has_cycle(graph: AgentGraph) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in graph.nodes}
    adj: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    for e in graph.edges:
        if e.source_id in adj and e.target_id in color:
            adj[e.source_id].append(e.target_id)

    def dfs(node):
        color[node] = GRAY
        for neighbor in adj.get(node, []):
            if color.get(neighbor) == GRAY:
                return True
            if color.get(neighbor) == WHITE and dfs(neighbor):
                return True
        color[node] = BLACK
        return False

    for nid in graph.nodes:
        if color[nid] == WHITE:
            if dfs(nid):
                return True
    return False


def _reachable_nodes(graph: AgentGraph) -> set[str]:
    adj: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    for e in graph.edges:
        if e.source_id in adj and e.target_id in adj:
            adj[e.source_id].append(e.target_id)

    start_ids = {nid for nid, n in graph.nodes.items() if n.type == "start"}
    visited = set()
    stack = list(start_ids)
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                stack.append(neighbor)
    return visited


def auto_layout(
    graph: AgentGraph, layer_gap: float = 150.0, node_gap: float = 250.0
) -> list[NodePosition]:
    """Compute layered layout positions for graph nodes."""
    adj: dict[str, list[str]] = {nid: [] for nid in graph.nodes}
    in_degree: dict[str, int] = {nid: 0 for nid in graph.nodes}
    for e in graph.edges:
        if e.source_id in adj and e.target_id in in_degree:
            adj[e.source_id].append(e.target_id)
            in_degree[e.target_id] = in_degree.get(e.target_id, 0) + 1

    layers: list[list[str]] = []
    remaining = dict(in_degree)
    queue = [nid for nid, deg in remaining.items() if deg == 0]

    while queue:
        layer = sorted(queue)
        layers.append(layer)
        next_queue = []
        for nid in layer:
            for neighbor in adj.get(nid, []):
                remaining[neighbor] -= 1
                if remaining[neighbor] == 0:
                    next_queue.append(neighbor)
        queue = next_queue

    remaining_nodes = [nid for nid in remaining if remaining[nid] > 0]
    if remaining_nodes:
        layers.append(sorted(remaining_nodes))

    positions = []
    for layer_idx, layer in enumerate(layers):
        total_width = len(layer) * node_gap
        start_x = -total_width / 2 + node_gap / 2
        for node_idx, nid in enumerate(layer):
            positions.append(
                NodePosition(
                    node_id=nid,
                    x=start_x + node_idx * node_gap,
                    y=layer_idx * layer_gap,
                )
            )

    logger.info("Auto-layout: %d nodes in %d layers", len(positions), len(layers))
    return positions


@dataclass
class GraphDocument:
    """Full graph document with metadata and layout positions."""

    id: str
    name: str
    description: str = ""
    graph_data: dict[str, Any] = field(default_factory=dict)
    positions: list[NodePosition] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "graph_data": self.graph_data,
            "positions": [p.to_dict() for p in self.positions],
            "tags": self.tags,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphDocument:
        positions = [NodePosition(**p) for p in data.get("positions", [])]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            graph_data=data.get("graph_data", {}),
            positions=positions,
            tags=data.get("tags", []),
            version=data.get("version", 1),
        )


class GraphEditor:
    """Backend for the DAG visual editor."""

    def __init__(self):
        self._documents: dict[str, GraphDocument] = {}

    def create(
        self, name: str, description: str = "", graph_data: dict | None = None
    ) -> GraphDocument:
        doc_id = str(uuid.uuid4())[:8]
        doc = GraphDocument(
            id=doc_id,
            name=name,
            description=description,
            graph_data=graph_data or {"nodes": [], "edges": []},
        )
        self._documents[doc_id] = doc
        logger.info("Created graph document: %s (%s)", doc_id, name)
        return doc

    def get(self, doc_id: str) -> GraphDocument | None:
        return self._documents.get(doc_id)

    def list_all(self) -> list[GraphDocument]:
        return list(self._documents.values())

    def update(self, doc_id: str, **kwargs) -> GraphDocument | None:
        doc = self._documents.get(doc_id)
        if not doc:
            return None
        for key, val in kwargs.items():
            if hasattr(doc, key):
                setattr(doc, key, val)
        doc.version += 1
        logger.info("Updated graph document: %s (v%d)", doc_id, doc.version)
        return doc

    def delete(self, doc_id: str) -> bool:
        if doc_id in self._documents:
            del self._documents[doc_id]
            logger.info("Deleted graph document: %s", doc_id)
            return True
        return False

    def validate(self, doc_id: str) -> ValidationResult:
        doc = self._documents.get(doc_id)
        if not doc:
            return ValidationResult(
                valid=False,
                issues=[ValidationIssue("error", "", f"Document {doc_id} not found")],
            )
        graph = AgentGraph.from_dict(doc.graph_data)
        return validate_graph(graph)

    def compute_layout(self, doc_id: str) -> list[NodePosition]:
        doc = self._documents.get(doc_id)
        if not doc:
            return []
        graph = AgentGraph.from_dict(doc.graph_data)
        positions = auto_layout(graph)
        doc.positions = positions
        return positions

    def duplicate(self, doc_id: str, new_name: str = "") -> GraphDocument | None:
        doc = self._documents.get(doc_id)
        if not doc:
            return None
        import copy

        new_id = str(uuid.uuid4())[:8]
        new_doc = GraphDocument(
            id=new_id,
            name=new_name or f"{doc.name} (copy)",
            description=doc.description,
            graph_data=copy.deepcopy(doc.graph_data),
            positions=copy.deepcopy(doc.positions),
            tags=list(doc.tags),
            version=1,
        )
        self._documents[new_id] = new_doc
        logger.info("Duplicated graph: %s -> %s", doc_id, new_id)
        return new_doc
