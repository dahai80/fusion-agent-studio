"""Agent Graph data model — defines the agent workflow as a directed graph."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

NodeType = Literal[
    "start",
    "llm",
    "tool",
    "condition",
    "loop",
    "parallel",
    "end",
    "error_handler",
    "rag",
    "planner",
    "verify",
]


@dataclass
class NodeConfig:
    """Configuration for a single node in the agent graph."""

    type: NodeType
    label: str = ""
    # LLM node config
    model: str = ""
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    # Tool node config
    tool_name: str = ""
    tool_params: dict = field(default_factory=dict)
    # Condition node config
    condition_expr: str = ""
    # Loop node config
    max_iterations: int = 10
    # Error handler node config
    max_retries: int = 3
    retry_delay: float = 1.0
    # Self-repair retry for LLM tool_call errors
    retry_on_error: bool = False
    # Allow LLM to dynamically register/unregister tools at runtime
    allow_dynamic_tools: bool = False
    # Disable tool injection for this LLM node (pure text/structured output)
    disable_tools: bool = False
    # Effort level for reasoning models
    effort: str = ""
    # Agent loop: ""=off (graph-reentry), "agent"=内生多轮工具回灌
    loop_mode: str = ""
    max_loop_iterations: int = 0
    stop_sequences: list = field(default_factory=list)
    # Canvas position
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "type": self.type,
            "label": self.label,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tool_name": self.tool_name,
            "tool_params": self.tool_params,
            "condition_expr": self.condition_expr,
            "max_iterations": self.max_iterations,
            "retry_on_error": self.retry_on_error,
            "allow_dynamic_tools": self.allow_dynamic_tools,
            "disable_tools": self.disable_tools,
            "effort": self.effort,
            "loop_mode": self.loop_mode,
            "max_loop_iterations": self.max_loop_iterations,
            "stop_sequences": self.stop_sequences,
            "x": self.x,
            "y": self.y,
        }
        return {k: v for k, v in d.items() if v}

    @classmethod
    def from_dict(cls, data: dict) -> NodeConfig:
        return cls(**data)


@dataclass
class Edge:
    """Directed edge between two nodes."""

    source_id: str
    target_id: str
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Edge:
        d = dict(data)
        if "source" in d and "source_id" not in d:
            d["source_id"] = d.pop("source")
        if "target" in d and "target_id" not in d:
            d["target_id"] = d.pop("target")
        d.pop("source", None)
        d.pop("target", None)
        return cls(**d)


@dataclass
class AgentGraph:
    """Complete agent workflow graph."""

    id: str = ""
    name: str = ""
    description: str = ""
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    start_node_id: str = ""
    version: str = "1.0"

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:16]

    def add_node(self, node_id: str, config: NodeConfig) -> None:
        self.nodes[node_id] = config
        if not self.start_node_id and config.type == "start":
            self.start_node_id = node_id

    def add_edge(self, source_id: str, target_id: str, label: str = "") -> None:
        self.edges.append(Edge(source_id=source_id, target_id=target_id, label=label))

    def get_node(self, node_id: str) -> NodeConfig | None:
        return self.nodes.get(node_id)

    def get_outgoing_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source_id == node_id]

    def get_next_node(self, current_id: str, condition_result: str = "") -> str | None:
        edges = self.get_outgoing_edges(current_id)
        if not edges:
            return None
        if len(edges) == 1:
            return edges[0].target_id
        # Multiple edges: use condition label to select
        for e in edges:
            if e.label == condition_result:
                return e.target_id
        # Try truthy/falsy normalization for condition results
        truthy = {"true", "yes", "1"}
        falsy = {"false", "no", "0"}
        if condition_result.lower() in truthy:
            for e in edges:
                if e.label.lower() in truthy:
                    return e.target_id
        elif condition_result.lower() in falsy:
            for e in edges:
                if e.label.lower() in falsy:
                    return e.target_id
        # Fallback: return first edge without label
        for e in edges:
            if not e.label:
                return e.target_id
        logger.warning(
            "No matching edge label for condition_result=%r from node %s, "
            "falling back to first edge %s",
            condition_result, current_id, edges[0].target_id,
        )
        return edges[0].target_id

    def find_llm_model(self) -> str:
        """Find the first LLM node's model name."""
        for node in self.nodes.values():
            if node.type == "llm" and node.model:
                return node.model
        return ""

    def validate(self) -> list[str]:
        """Validate the graph structure. Returns list of errors."""
        errors = []
        if not self.start_node_id:
            errors.append("Graph has no start node")
        if not self.nodes:
            errors.append("Graph has no nodes")
        if self.start_node_id and self.start_node_id not in self.nodes:
            errors.append(f"Start node '{self.start_node_id}' not found in nodes")
        # Check all edges reference valid nodes
        for edge in self.edges:
            if edge.source_id not in self.nodes:
                errors.append(f"Edge source '{edge.source_id}' not found in nodes")
            if edge.target_id not in self.nodes:
                errors.append(f"Edge target '{edge.target_id}' not found in nodes")
        # Check all nodes are reachable from start
        if self.start_node_id:
            reachable = self._reachable_nodes()
            for nid in self.nodes:
                if nid not in reachable and nid != self.start_node_id:
                    errors.append(f"Node '{nid}' is unreachable from start")
        return errors

    def _reachable_nodes(self) -> set[str]:
        """BFS from start node to find all reachable nodes."""
        visited: set[str] = set()
        queue = [self.start_node_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for edge in self.get_outgoing_edges(current):
                queue.append(edge.target_id)
        return visited

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "start_node_id": self.start_node_id,
            "version": self.version,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> AgentGraph:
        raw_nodes = data.get("nodes", {})
        if isinstance(raw_nodes, dict):
            nodes = {nid: NodeConfig.from_dict(n) for nid, n in raw_nodes.items()}
        elif isinstance(raw_nodes, list):
            nodes = {}
            for n in raw_nodes:
                node_id = n.get("id", f"node-{len(nodes)}")
                node_data = {k: v for k, v in n.items() if k != "id"}
                nodes[node_id] = NodeConfig.from_dict(node_data)
        else:
            nodes = {}
        edges = [Edge.from_dict(e) for e in data.get("edges", [])]
        start_node_id = data.get("start_node_id", "")
        if not start_node_id:
            for nid, ncfg in nodes.items():
                if ncfg.type == "start":
                    start_node_id = nid
                    break
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            nodes=nodes,
            edges=edges,
            start_node_id=start_node_id,
            version=data.get("version", "1.0"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> AgentGraph:
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def create_default(cls, name: str = "Default Agent") -> AgentGraph:
        """Create a simple default graph: start → llm → end."""
        graph = cls(name=name)
        graph.add_node("start", NodeConfig(type="start", label="Start", x=100, y=200))
        graph.add_node(
            "llm_1",
            NodeConfig(
                type="llm",
                label="LLM Think",
                model="qwen3.5-9b",
                system_prompt="You are a helpful assistant.",
                x=300,
                y=200,
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="End", x=500, y=200))
        graph.add_edge("start", "llm_1")
        graph.add_edge("llm_1", "end")
        return graph
