"""Sub-graph node — embed one agent graph as a node within another graph."""

from __future__ import annotations

from .graph import AgentGraph, NodeConfig


class SubGraphNode:
    """A node that embeds another agent graph as a sub-process.

    Sub-graphs allow:
    - Reusing common workflows (e.g., "code review" sub-graph)
    - Hierarchical composition of complex agents
    - Independent versioning of sub-graphs
    """

    def __init__(
        self,
        sub_graph: AgentGraph,
        input_mapping: dict | None = None,
        output_mapping: dict | None = None,
    ):
        self.sub_graph = sub_graph
        self.input_mapping = input_mapping or {}  # parent_var -> sub_graph_var
        self.output_mapping = output_mapping or {}  # sub_graph_var -> parent_var

    def to_node_config(self, node_id: str, label: str = "") -> NodeConfig:
        """Convert to a NodeConfig for embedding in a parent graph."""
        return NodeConfig(
            type="tool",
            label=label or self.sub_graph.name,
            tool_name="__sub_graph__",
            tool_params={
                "graph_id": self.sub_graph.id,
                "graph_json": self.sub_graph.to_json(),
                "input_mapping": self.input_mapping,
                "output_mapping": self.output_mapping,
            },
        )


class SubGraphRegistry:
    """Registry for reusable sub-graphs."""

    def __init__(self):
        self._graphs: dict[str, AgentGraph] = {}

    def register(self, graph: AgentGraph) -> None:
        self._graphs[graph.id] = graph

    def get(self, graph_id: str) -> AgentGraph:
        if graph_id not in self._graphs:
            raise KeyError(f"Sub-graph '{graph_id}' not found")
        return self._graphs[graph_id]

    def list(self) -> list[dict]:
        return [
            {
                "id": gid,
                "name": g.name,
                "description": g.description,
                "node_count": len(g.nodes),
            }
            for gid, g in self._graphs.items()
        ]

    @property
    def count(self) -> int:
        return len(self._graphs)
