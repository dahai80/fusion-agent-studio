"""FMP router — Fusion Message Protocol with turn-taking, circuit breaker, @Mention.

Multi-agent message routing protocol:
- Turn-taking: round-robin or priority-based agent selection
- 3-round circuit breaker: auto-isolate failing agents
- @Mention: direct agent targeting with broadcast fallback
- Message dedup: prevent duplicate processing
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_RESET_SECONDS = 30.0
DEDUP_WINDOW_SECONDS = 60.0


@dataclass
class AgentInfo:
    id: str = ""
    name: str = ""
    capabilities: list[str] = field(default_factory=list)
    priority: int = 5
    status: str = "online"
    last_active: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.last_active:
            self.last_active = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": self.capabilities,
            "priority": self.priority,
            "status": self.status,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentInfo:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            capabilities=data.get("capabilities", []),
            priority=data.get("priority", 5),
            status=data.get("status", "online"),
            last_active=data.get("last_active", 0.0),
        )


@dataclass
class FMPMessageV2:
    message_id: str = ""
    sender: str = ""
    recipient: str = ""
    message_type: str = "request"
    payload: dict[str, Any] = field(default_factory=dict)
    round_number: int = 0
    timestamp: float = 0.0
    mention_targets: list[str] = field(default_factory=list)
    priority: int = 5
    reply_to: str = ""

    def __post_init__(self):
        if not self.message_id:
            self.message_id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type,
            "payload": self.payload,
            "round_number": self.round_number,
            "timestamp": self.timestamp,
            "mention_targets": self.mention_targets,
            "priority": self.priority,
            "reply_to": self.reply_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FMPMessageV2:
        return cls(
            message_id=data.get("message_id", ""),
            sender=data.get("sender", ""),
            recipient=data.get("recipient", ""),
            message_type=data.get("message_type", "request"),
            payload=data.get("payload", {}),
            round_number=data.get("round_number", 0),
            timestamp=data.get("timestamp", 0.0),
            mention_targets=data.get("mention_targets", []),
            priority=data.get("priority", 5),
            reply_to=data.get("reply_to", ""),
        )


class AgentCircuitBreaker:
    """Per-agent circuit breaker with 3-round trip detection."""

    def __init__(
        self,
        threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        reset_time: float = CIRCUIT_RESET_SECONDS,
    ):
        self.threshold = threshold
        self.reset_time = reset_time
        self._failures: dict[str, int] = {}
        self._trip_times: dict[str, float] = {}

    def record_success(self, agent_id: str) -> None:
        self._failures.pop(agent_id, None)
        self._trip_times.pop(agent_id, None)

    def record_failure(self, agent_id: str) -> None:
        count = self._failures.get(agent_id, 0) + 1
        self._failures[agent_id] = count
        if count >= self.threshold:
            self._trip_times[agent_id] = time.time()
            logger.warning(
                "Circuit breaker TRIPPED for agent %s (%d failures)", agent_id, count
            )

    def is_open(self, agent_id: str) -> bool:
        if agent_id not in self._trip_times:
            return False
        if time.time() - self._trip_times[agent_id] >= self.reset_time:
            self._failures.pop(agent_id, None)
            self._trip_times.pop(agent_id, None)
            logger.info("Circuit breaker RESET for agent %s", agent_id)
            return False
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "tripped_agents": list(self._trip_times.keys()),
            "failure_counts": dict(self._failures),
        }


class MessageDedup:
    """Prevent duplicate message processing within a time window."""

    def __init__(self, window_seconds: float = DEDUP_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        expired = [
            mid for mid, ts in self._seen.items() if now - ts > self.window_seconds
        ]
        for mid in expired:
            del self._seen[mid]

        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False


class TurnManager:
    """Turn-taking: round-robin or priority-based agent selection."""

    def __init__(self):
        self._order: list[str] = []
        self._index: int = 0

    def set_order(self, agent_ids: list[str]) -> None:
        self._order = list(agent_ids)
        self._index = 0

    def next_turn(self, exclude: set[str] | None = None) -> str | None:
        if not self._order:
            return None
        exclude = exclude or set()
        for _ in range(len(self._order)):
            agent_id = self._order[self._index % len(self._order)]
            self._index += 1
            if agent_id not in exclude:
                return agent_id
        return None

    def reset(self) -> None:
        self._index = 0


class MentionRouter:
    """Parse @Mention syntax and route to specific agents."""

    MENTION_PREFIX = "@"

    def parse_mentions(self, text: str) -> list[str]:
        mentions = []
        for word in text.split():
            if word.startswith(self.MENTION_PREFIX):
                mentions.append(word[1:])
        return mentions

    def route_by_mention(
        self,
        message: FMPMessageV2,
        agents: dict[str, AgentInfo],
    ) -> list[str]:
        targets = []

        for mention in message.mention_targets:
            for aid, agent in agents.items():
                if agent.name == mention or aid == mention:
                    targets.append(aid)
                    break

        if not targets:
            text_mentions = self.parse_mentions(message.payload.get("text", ""))
            for mention in text_mentions:
                for aid, agent in agents.items():
                    if agent.name == mention or aid == mention:
                        targets.append(aid)
                        break

        return targets


class FMProtocol:
    """Fusion Message Protocol: turn-taking + circuit breaker + @Mention."""

    def __init__(self, local_agent_id: str = ""):
        self.local_agent_id = local_agent_id or uuid.uuid4().hex[:8]
        self.circuit_breaker = AgentCircuitBreaker()
        self.dedup = MessageDedup()
        self.turn_manager = TurnManager()
        self.mention_router = MentionRouter()
        self._agents: dict[str, AgentInfo] = {}
        self._message_log: list[FMPMessageV2] = []
        self._stats = {
            "sent": 0,
            "received": 0,
            "dropped_dedup": 0,
            "circuit_blocked": 0,
            "routed": 0,
        }

    def register_agent(self, agent: AgentInfo) -> None:
        self._agents[agent.id] = agent
        self.turn_manager.set_order(list(self._agents.keys()))
        logger.info("FMP registered agent: %s (%s)", agent.name, agent.id)

    def unregister_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)
        self.turn_manager.set_order(list(self._agents.keys()))

    def send(
        self,
        recipient: str = "",
        message_type: str = "request",
        payload: dict | None = None,
        mention_targets: list[str] | None = None,
        priority: int = 5,
        round_number: int = 0,
        reply_to: str = "",
    ) -> FMPMessageV2:
        msg = FMPMessageV2(
            sender=self.local_agent_id,
            recipient=recipient,
            message_type=message_type,
            payload=payload or {},
            mention_targets=mention_targets or [],
            priority=priority,
            round_number=round_number,
            reply_to=reply_to,
        )
        self._message_log.append(msg)
        self._stats["sent"] += 1
        logger.info("FMP send: %s -> %s type=%s", msg.sender, recipient, message_type)
        return msg

    def receive(self, message: FMPMessageV2) -> dict[str, Any]:
        if self.dedup.is_duplicate(message.message_id):
            self._stats["dropped_dedup"] += 1
            return {"action": "drop", "reason": "duplicate"}

        if message.round_number >= MAX_ROUNDS:
            self._stats["dropped_dedup"] += 1
            return {"action": "drop", "reason": "max_rounds_exceeded"}

        targets = self.route(message)
        self._stats["received"] += 1

        if not targets:
            return {"action": "drop", "reason": "no_available_targets"}

        self._stats["routed"] += 1
        return {"action": "route", "targets": targets}

    def route(self, message: FMPMessageV2) -> list[str]:
        online = {
            aid: a
            for aid, a in self._agents.items()
            if a.status == "online" and not self.circuit_breaker.is_open(aid)
        }
        if not online:
            logger.warning("FMP: no online agents available")
            return []

        if message.mention_targets:
            targets = self.mention_router.route_by_mention(message, online)
            if targets:
                return targets

        if message.recipient and message.recipient != "broadcast":
            if message.recipient in online:
                return [message.recipient]
            self._stats["circuit_blocked"] += 1
            return []

        next_id = self.turn_manager.next_turn(
            exclude={aid for aid in self._agents if self.circuit_breaker.is_open(aid)}
        )
        if next_id and next_id in online:
            return [next_id]

        return [aid for aid in online.keys()][:1]

    def record_success(self, agent_id: str) -> None:
        self.circuit_breaker.record_success(agent_id)

    def record_failure(self, agent_id: str) -> None:
        self.circuit_breaker.record_failure(agent_id)

    def get_stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "agents": len(self._agents),
            "circuit_breaker": self.circuit_breaker.get_status(),
        }
