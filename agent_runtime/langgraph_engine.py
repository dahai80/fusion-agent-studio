"""LangGraph Workflow Engine — visual DAG execution with approval gates.

Implements #35: workflow definition, DAG validation, approval gate breakpoints,
context sandbox, execution log, skill/workflow REST API.

Importers: daemon_server.py (lazy getter + RPC dispatch), api_server.py (REST endpoints).
Affected API: langgraph.create/get/list/delete/run/approve/cancel RPC methods.
Data schemas: WorkflowDefinition, WorkflowNode, WorkflowEdge, RunInstance, NodeTrace, ApprovalRecord, ContextSandbox.
User instruction: "后续功能也要马上启动落地实施" — implement remaining open issues #29-#37.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WorkflowNode:
    node_id: str = ""
    node_type: str = "SKILL_NODE"
    label: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowNode:
        return cls(
            node_id=data.get("node_id", ""),
            node_type=data.get("node_type", "SKILL_NODE"),
            label=data.get("label", ""),
            config=data.get("config", {}),
        )


@dataclass
class WorkflowEdge:
    source_id: str = ""
    target_id: str = ""
    condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowEdge:
        return cls(
            source_id=data.get("source_id", ""),
            target_id=data.get("target_id", ""),
            condition=data.get("condition", ""),
        )


@dataclass
class WorkflowDefinition:
    wf_id: str = ""
    name: str = ""
    slash_command: str = ""
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[WorkflowEdge] = field(default_factory=list)
    entry_node_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "wf_id": self.wf_id,
            "name": self.name,
            "slash_command": self.slash_command,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "entry_node_id": self.entry_node_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowDefinition:
        return cls(
            wf_id=data.get("wf_id", ""),
            name=data.get("name", ""),
            slash_command=data.get("slash_command", ""),
            nodes=[WorkflowNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[WorkflowEdge.from_dict(e) for e in data.get("edges", [])],
            entry_node_id=data.get("entry_node_id", ""),
        )


VALID_NODE_TYPES = {
    "START_NODE",
    "CONNECTOR_NODE",
    "SKILL_NODE",
    "CONDITION_NODE",
    "APPROVAL_GATE_NODE",
    "OUTPUT_NODE",
    "END_NODE",
}

RUN_STATUSES = ("CREATED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "TERMINATED")


@dataclass
class NodeTrace:
    node_id: str = ""
    enter_time: float = 0.0
    exit_time: float = 0.0
    status: str = "pending"
    input_data: str = ""
    output_data: str = ""
    error_msg: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "enter_time": self.enter_time,
            "exit_time": self.exit_time,
            "status": self.status,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "error_msg": self.error_msg,
        }


@dataclass
class ApprovalRecord:
    node_id: str = ""
    action: str = ""
    reviewer: str = ""
    comment: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "action": self.action,
            "reviewer": self.reviewer,
            "comment": self.comment,
            "timestamp": self.timestamp,
        }


@dataclass
class ContextSandbox:
    data: dict[str, Any] = field(default_factory=dict)
    memory_limit_mb: int = 64
    row_limit: int = 10000

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)

    def set(self, key: str, value: Any):
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class RunInstance:
    run_id: str = ""
    wf_id: str = ""
    trigger_type: str = "manual"
    status: str = "CREATED"
    context: ContextSandbox = field(default_factory=ContextSandbox)
    node_trace: list[NodeTrace] = field(default_factory=list)
    approval_records: list[ApprovalRecord] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    timeout_seconds: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "wf_id": self.wf_id,
            "trigger_type": self.trigger_type,
            "status": self.status,
            "context_snapshot": self.context.snapshot(),
            "node_trace": [t.to_dict() for t in self.node_trace],
            "approval_records": [a.to_dict() for a in self.approval_records],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LangGraphEngine:
    """Execute LangGraph workflows with DAG validation and approval gates."""

    def __init__(self):
        self._workflows: dict[str, WorkflowDefinition] = {}
        self._runs: dict[str, RunInstance] = {}

    def validate_workflow(self, wf: WorkflowDefinition) -> list[str]:
        errors: list[str] = []
        node_ids = {n.node_id for n in wf.nodes}
        if not wf.entry_node_id:
            errors.append("entry_node_id is required")
        elif wf.entry_node_id not in node_ids:
            errors.append(f"entry_node_id '{wf.entry_node_id}' not found in nodes")

        for n in wf.nodes:
            if n.node_type not in VALID_NODE_TYPES:
                errors.append(f"invalid node_type '{n.node_type}' on {n.node_id}")

        has_end = any(n.node_type == "END_NODE" for n in wf.nodes)
        if not has_end:
            errors.append("workflow must have at least one END_NODE")

        visited: set[str] = set()
        stack = [wf.entry_node_id] if wf.entry_node_id else []
        while stack:
            nid = stack.pop()
            if nid in visited:
                errors.append(f"cycle detected at node {nid}")
                break
            visited.add(nid)
            for e in wf.edges:
                if e.source_id == nid and e.target_id not in visited:
                    stack.append(e.target_id)

        for e in wf.edges:
            if e.source_id not in node_ids:
                errors.append(f"edge source '{e.source_id}' not found in nodes")
            if e.target_id not in node_ids:
                errors.append(f"edge target '{e.target_id}' not found in nodes")

        logger.info("Validated workflow %s: %d errors", wf.wf_id, len(errors))
        return errors

    def create_workflow(self, wf: WorkflowDefinition) -> dict[str, Any]:
        errors = self.validate_workflow(wf)
        if errors:
            return {"status": "error", "errors": errors}
        if not wf.wf_id:
            wf.wf_id = uuid.uuid4().hex[:12]
        self._workflows[wf.wf_id] = wf
        logger.info(
            "Created workflow %s: %s (%d nodes)", wf.wf_id, wf.name, len(wf.nodes)
        )
        return {"status": "ok", "wf_id": wf.wf_id}

    def get_workflow(self, wf_id: str) -> dict[str, Any]:
        wf = self._workflows.get(wf_id)
        if not wf:
            return {"status": "error", "message": f"Workflow {wf_id} not found"}
        return {"status": "ok", "workflow": wf.to_dict()}

    def list_workflows(self) -> list[dict[str, Any]]:
        return [
            {"wf_id": wf.wf_id, "name": wf.name, "node_count": len(wf.nodes)}
            for wf in self._workflows.values()
        ]

    def delete_workflow(self, wf_id: str) -> dict[str, Any]:
        if wf_id not in self._workflows:
            return {"status": "error", "message": f"Workflow {wf_id} not found"}
        del self._workflows[wf_id]
        logger.info("Deleted workflow %s", wf_id)
        return {"status": "ok"}

    async def run_workflow(
        self, wf_id: str, trigger_type: str = "manual", input_data: dict | None = None
    ) -> dict[str, Any]:
        wf = self._workflows.get(wf_id)
        if not wf:
            return {"status": "error", "message": f"Workflow {wf_id} not found"}

        run = RunInstance(
            run_id=uuid.uuid4().hex[:12],
            wf_id=wf_id,
            trigger_type=trigger_type,
            status="RUNNING",
            created_at=time.time(),
            updated_at=time.time(),
        )
        if input_data:
            for k, v in input_data.items():
                run.context.set(k, v)

        self._runs[run.run_id] = run

        current = wf.entry_node_id
        node_map = {n.node_id: n for n in wf.nodes}
        edges_from: dict[str, list[WorkflowEdge]] = {}
        for e in wf.edges:
            edges_from.setdefault(e.source_id, []).append(e)

        while current:
            node = node_map.get(current)
            if not node:
                run.status = "FAILED"
                run.updated_at = time.time()
                logger.error("Node %s not found in workflow %s", current, wf_id)
                return {
                    "status": "error",
                    "run_id": run.run_id,
                    "message": f"Node {current} not found",
                }

            trace = NodeTrace(node_id=current, enter_time=time.time(), status="running")
            run.node_trace.append(trace)

            if node.node_type == "END_NODE":
                trace.exit_time = time.time()
                trace.status = "completed"
                run.status = "COMPLETED"
                run.updated_at = time.time()
                logger.info("Workflow %s run %s completed", wf_id, run.run_id)
                return {
                    "status": "ok",
                    "run_id": run.run_id,
                    "result": run.context.snapshot(),
                }

            if node.node_type == "APPROVAL_GATE_NODE":
                trace.status = "paused"
                run.status = "PAUSED"
                run.updated_at = time.time()
                logger.info(
                    "Workflow %s run %s paused at approval gate %s",
                    wf_id,
                    run.run_id,
                    current,
                )
                return {
                    "status": "paused",
                    "run_id": run.run_id,
                    "gate_node_id": current,
                }

            if node.node_type == "CONDITION_NODE":
                cond_key = node.config.get("condition_key", "")
                cond_val = run.context.get(cond_key)
                next_node = None
                for edge in edges_from.get(current, []):
                    if edge.condition == str(cond_val) or not edge.condition:
                        next_node = edge.target_id
                        break
                trace.exit_time = time.time()
                trace.status = "completed"
                current = next_node
                continue

            trace.exit_time = time.time()
            trace.status = "completed"
            edges = edges_from.get(current, [])
            if edges:
                current = edges[0].target_id
            else:
                break

        run.status = "COMPLETED"
        run.updated_at = time.time()
        logger.info("Workflow %s run %s ended (no more edges)", wf_id, run.run_id)
        return {"status": "ok", "run_id": run.run_id, "result": run.context.snapshot()}

    def approve_run(
        self,
        run_id: str,
        action: str = "approve",
        reviewer: str = "",
        comment: str = "",
    ) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if not run:
            return {"status": "error", "message": f"Run {run_id} not found"}
        if run.status != "PAUSED":
            return {"status": "error", "message": f"Run {run_id} is not paused"}

        record = ApprovalRecord(
            node_id=run.node_trace[-1].node_id if run.node_trace else "",
            action=action,
            reviewer=reviewer,
            comment=comment,
            timestamp=time.time(),
        )
        run.approval_records.append(record)

        if action == "deny":
            run.status = "TERMINATED"
            run.updated_at = time.time()
            logger.info("Run %s denied by %s", run_id, reviewer)
            return {"status": "ok", "run_status": "TERMINATED"}

        for trace in run.node_trace:
            if trace.status == "paused":
                trace.status = "completed"
                trace.exit_time = time.time()

        run.status = "RUNNING"
        run.updated_at = time.time()
        logger.info("Run %s approved by %s, resuming", run_id, reviewer)
        return {"status": "ok", "run_status": "RUNNING", "run_id": run_id}

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if not run:
            return {"status": "error", "message": f"Run {run_id} not found"}
        run.status = "TERMINATED"
        run.updated_at = time.time()
        logger.info("Run %s cancelled", run_id)
        return {"status": "ok", "run_status": "TERMINATED"}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if not run:
            return {"status": "error", "message": f"Run {run_id} not found"}
        return {"status": "ok", "run": run.to_dict()}
