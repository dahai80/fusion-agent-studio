"""Swarm Router — agent handoff with hop_count limit and task delegation.

Lighter-weight agent-to-agent routing layer.  Composes FMProtocol for
messaging, SafetyGateway for L3 escalation.  Enforces max 3 hops per
ar1.md §5 to prevent infinite handoff loops.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_HOPS = 3


@dataclass
class SwarmAgent:
    id: str = ""
    name: str = ""
    capabilities: list[str] = field(default_factory=list)
    handoff_targets: list[str] = field(default_factory=list)
    max_hops: int = MAX_HOPS
    status: str = "online"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:8]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": self.capabilities,
            "handoff_targets": self.handoff_targets,
            "max_hops": self.max_hops,
            "status": self.status,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SwarmAgent:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            capabilities=data.get("capabilities", []),
            handoff_targets=data.get("handoff_targets", []),
            max_hops=data.get("max_hops", MAX_HOPS),
            status=data.get("status", "online"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TaskDelegation:
    id: str = ""
    task: str = ""
    delegator: str = ""
    delegatee: str = ""
    trigger_condition: str = ""
    deliverable: str = ""
    status: str = "pending"
    hop_count: int = 0
    created_at: float = 0.0
    completed_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:10]
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "delegator": self.delegator,
            "delegatee": self.delegatee,
            "trigger_condition": self.trigger_condition,
            "deliverable": self.deliverable,
            "status": self.status,
            "hop_count": self.hop_count,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDelegation:
        return cls(
            id=data.get("id", ""),
            task=data.get("task", ""),
            delegator=data.get("delegator", ""),
            delegatee=data.get("delegatee", ""),
            trigger_condition=data.get("trigger_condition", ""),
            deliverable=data.get("deliverable", ""),
            status=data.get("status", "pending"),
            hop_count=data.get("hop_count", 0),
            created_at=data.get("created_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
            result=data.get("result", {}),
        )


@dataclass
class HandoffContext:
    conversation: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    hop_count: int = 0
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation": self.conversation,
            "metadata": self.metadata,
            "hop_count": self.hop_count,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffContext:
        return cls(
            conversation=data.get("conversation", []),
            metadata=data.get("metadata", {}),
            hop_count=data.get("hop_count", 0),
            task_id=data.get("task_id", ""),
        )


class SwarmRouter:
    """Agent handoff with hop_count limit and task delegation."""

    def __init__(self, max_hops: int = MAX_HOPS):
        self.max_hops = max_hops
        self._agents: dict[str, SwarmAgent] = {}
        self._delegations: dict[str, TaskDelegation] = {}
        self._handoff_log: list[dict[str, Any]] = []
        logger.info("SwarmRouter initialized (max_hops=%d)", max_hops)

    def register_agent(self, agent: SwarmAgent) -> None:
        self._agents[agent.id] = agent
        logger.info("Registered swarm agent: %s (%s)", agent.id, agent.name)

    def unregister_agent(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            logger.info("Unregistered swarm agent: %s", agent_id)
            return True
        return False

    def get_agent(self, agent_id: str) -> SwarmAgent | None:
        return self._agents.get(agent_id)

    def list_agents(self) -> list[SwarmAgent]:
        return list(self._agents.values())

    def find_agent_by_capability(self, capability: str, exclude: set[str] | None = None) -> SwarmAgent | None:
        candidates = [
            a for a in self._agents.values()
            if a.status == "online"
            and capability in a.capabilities
            and (not exclude or a.id not in exclude)
        ]
        if not candidates:
            logger.warning("No agent found for capability='%s'", capability)
            return None
        return candidates[0]

    def delegate(self, delegator_id: str, task: str, capability: str = "",
                 trigger_condition: str = "", deliverable: str = "") -> TaskDelegation | None:
        delegator = self._agents.get(delegator_id)
        if not delegator:
            logger.error("Delegator %s not found", delegator_id)
            return None
        if capability:
            delegatee = self.find_agent_by_capability(capability, exclude={delegator_id})
        else:
            targets = [t for t in delegator.handoff_targets if t in self._agents and self._agents[t].status == "online"]
            delegatee = self._agents[targets[0]] if targets else None
        if not delegatee:
            logger.warning("No delegatee found for delegation from %s", delegator_id)
            return None
        delegation = TaskDelegation(
            task=task,
            delegator=delegator_id,
            delegatee=delegatee.id,
            trigger_condition=trigger_condition,
            deliverable=deliverable,
            hop_count=1,
        )
        self._delegations[delegation.id] = delegation
        logger.info("Delegated task %s: %s → %s (hop=1)", delegation.id, delegator_id, delegatee.id)
        return delegation

    def handoff(self, from_agent_id: str, to_agent_id: str, context: HandoffContext) -> HandoffContext | None:
        from_agent = self._agents.get(from_agent_id)
        to_agent = self._agents.get(to_agent_id)
        if not from_agent or not to_agent:
            logger.error("Handoff agents not found: %s → %s", from_agent_id, to_agent_id)
            return None
        new_hop = context.hop_count + 1
        effective_max = min(from_agent.max_hops, to_agent.max_hops, self.max_hops)
        if new_hop > effective_max:
            logger.warning("Handoff BLOCKED: hop_count=%d exceeds max_hops=%d (%s → %s)",
                           new_hop, effective_max, from_agent_id, to_agent_id)
            return None
        new_context = HandoffContext(
            conversation=list(context.conversation),
            metadata={**context.metadata, "handed_off_from": from_agent_id, "handed_off_at": time.time()},
            hop_count=new_hop,
            task_id=context.task_id,
        )
        self._handoff_log.append({
            "from": from_agent_id,
            "to": to_agent_id,
            "hop_count": new_hop,
            "task_id": context.task_id,
            "timestamp": time.time(),
        })
        logger.info("Handoff: %s → %s (hop=%d/%d)", from_agent_id, to_agent_id, new_hop, effective_max)
        return new_context

    def evaluate(self, task_id: str, result: dict[str, Any]) -> TaskDelegation | None:
        delegation = self._delegations.get(task_id)
        if not delegation:
            logger.error("Task %s not found for evaluation", task_id)
            return None
        delegation.status = "completed"
        delegation.result = result
        delegation.completed_at = time.time()
        logger.info("Task %s evaluated: status=completed", task_id)
        return delegation

    def escalate(self, task_id: str, reason: str = "") -> TaskDelegation | None:
        delegation = self._delegations.get(task_id)
        if not delegation:
            return None
        delegation.status = "escalated"
        delegation.result = {"escalated": True, "reason": reason}
        delegation.completed_at = time.time()
        logger.warning("Task %s ESCALATED: %s", task_id, reason)
        return delegation

    def auto_escalate_if_needed(self, task_id: str) -> TaskDelegation | None:
        delegation = self._delegations.get(task_id)
        if not delegation:
            return None
        agent = self._agents.get(delegation.delegatee)
        if not agent or agent.status != "online":
            return self.escalate(task_id, reason="delegatee_agent_offline")
        if delegation.hop_count >= agent.max_hops or delegation.hop_count >= self.max_hops:
            return self.escalate(task_id, reason="max_hops_exceeded")
        return None

    def get_delegation(self, task_id: str) -> TaskDelegation | None:
        return self._delegations.get(task_id)

    def list_delegations(self, status: str = "") -> list[TaskDelegation]:
        delegations = list(self._delegations.values())
        if status:
            delegations = [d for d in delegations if d.status == status]
        return delegations

    def get_handoff_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._handoff_log[-limit:]

    def get_stats(self) -> dict[str, Any]:
        delegations = list(self._delegations.values())
        return {
            "agents": len(self._agents),
            "total_delegations": len(delegations),
            "pending": len([d for d in delegations if d.status == "pending"]),
            "completed": len([d for d in delegations if d.status == "completed"]),
            "escalated": len([d for d in delegations if d.status == "escalated"]),
            "handoffs": len(self._handoff_log),
        }
