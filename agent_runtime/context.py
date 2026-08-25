"""Agent context — manages conversation history, state, and events during execution."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    """Types of events emitted during agent execution."""

    THINK = "think"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR = "error"
    START = "start"
    END = "end"
    TOKEN = "token"
    THINKING_TOKEN = "thinking_token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    SAFETY_APPROVAL = "safety_approval"
    SAFETY_TIMEOUT = "safety_timeout"
    CHECKPOINT = "checkpoint"
    VERIFY = "verify"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    RETRY = "retry"
    RETRY_SUCCESS = "retry_success"
    # C6 plan-as-mode: emitted when exit_plan_mode transitions read-only
    # explore -> execution (plan_mode flipped off).
    PLAN_MODE_EXIT = "plan_mode_exit"
    # C6 plan-as-mode: emitted when planner node blocks awaiting approval.
    PLAN_APPROVAL = "plan_approval"


@dataclass
class AgentEvent:
    """A single event emitted during agent execution."""

    type: AgentEventType
    content: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    node_id: str = ""
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "content": self.content,
            "name": self.name,
            "args": self.args,
            "node_id": self.node_id,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentEvent:
        return cls(
            type=AgentEventType(data["type"]),
            content=data.get("content", ""),
            name=data.get("name", ""),
            args=data.get("args", {}),
            node_id=data.get("node_id", ""),
            timestamp=data.get("timestamp", 0.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class AgentContext:
    """Conversation context and state for an agent execution session."""

    agent_id: str = ""
    session_id: str = ""
    messages: list[dict] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    current_node_id: str = ""
    iteration_count: int = 0
    max_iterations: int = 25
    artifact_turn_count: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    # 审计 A-1 Tier2: per-exec 状态迁出 singleton. 并发执行互不踩.
    # C6 plan-as-mode: read-only explore 门禁, per-exec.
    plan_mode: bool = False
    # 连续 tool_call 链深度计数 (防 LLM 无限 tool 自调), per-exec.
    tool_call_chain_count: int = 0
    # 审计 A-2/A-1 Tier3: per-exec 变量空间. 顶层 exec 从 runtime seed
    # 快照 copy (隔离不共享), 子 runtime 再 copy (已有 sub_vars 模式).
    # 持 VariableManager 实例 (非裸 dict), 保留 interpolate/nested/coerce.
    variables: Any = None

    def __post_init__(self):
        if not self.session_id:
            self.session_id = uuid.uuid4().hex[:16]

    def add_message(
        self,
        role: str,
        content: str,
        tool_calls: list | None = None,
        tool_call_id: str = "",
    ) -> None:
        msg = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        self.messages.append(msg)

    def add_event(self, event: AgentEvent) -> None:
        self.events.append(event)

    def is_complete(self) -> bool:
        return bool(self.finished_at) or bool(self.error)

    def is_max_iterations_reached(self) -> bool:
        return self.iteration_count >= self.max_iterations

    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    def token_usage(self) -> dict:
        """Aggregate token usage from all messages."""
        prompt = 0
        completion = 0
        for msg in self.messages:
            usage = msg.get("usage", {}) if isinstance(msg, dict) else {}
            prompt += usage.get("prompt_tokens", 0)
            completion += usage.get("completion_tokens", 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total": prompt + completion,
        }

    def to_dict(self) -> dict:
        # 审计 A-1 Tier3: variables 序列化取 to_dict 快照 (VariableManager).
        var_snapshot = {}
        vm = self.variables
        if vm is not None and hasattr(vm, "to_dict"):
            try:
                var_snapshot = vm.to_dict()
            except Exception:
                var_snapshot = {}
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "messages": self.messages,
            "events": [e.to_dict() for e in self.events],
            "metadata": self.metadata,
            "current_node_id": self.current_node_id,
            "iteration_count": self.iteration_count,
            "max_iterations": self.max_iterations,
            "artifact_turn_count": self.artifact_turn_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "plan_mode": self.plan_mode,
            "tool_call_chain_count": self.tool_call_chain_count,
            "variables": var_snapshot,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentContext:
        events = [AgentEvent.from_dict(e) for e in data.get("events", [])]
        ctx = cls(
            agent_id=data.get("agent_id", ""),
            session_id=data.get("session_id", ""),
            messages=data.get("messages", []),
            events=events,
            metadata=data.get("metadata", {}),
            current_node_id=data.get("current_node_id", ""),
            iteration_count=data.get("iteration_count", 0),
            max_iterations=data.get("max_iterations", 25),
            artifact_turn_count=data.get("artifact_turn_count", 0),
            started_at=data.get("started_at", 0.0),
            finished_at=data.get("finished_at", 0.0),
            error=data.get("error", ""),
            plan_mode=data.get("plan_mode", False),
            tool_call_chain_count=data.get("tool_call_chain_count", 0),
        )
        # 审计 A-1 Tier3: 反序列化重建 VariableManager, 载入快照.
        var_snapshot = data.get("variables", {})
        if var_snapshot:
            try:
                from .variable_manager import VariableManager

                vm = VariableManager()
                vm.load_from(var_snapshot if isinstance(var_snapshot, dict) else {})
                ctx.variables = vm
            except Exception:
                pass
        return ctx
