"""Cowork Manager — collaboration space IPC methods and context injection.

Implements #36 (Agent IPC cowork methods) and #37 (Space context injection).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SpaceAgentBinding:
    agent_id: str = ""
    space_id: str = ""
    call_permission: str = "all_member"
    added_at: float = 0.0
    last_called_at: float = 0.0
    call_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "space_id": self.space_id,
            "call_permission": self.call_permission,
            "added_at": self.added_at,
            "last_called_at": self.last_called_at,
            "call_count": self.call_count,
        }


@dataclass
class SpaceConversationMessage:
    role: str = ""
    content: str = ""
    sender: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "sender": self.sender,
            "timestamp": self.timestamp,
        }


class CoworkManager:
    """Manage agent-space bindings, cowork calls, and context injection."""

    def __init__(self):
        self._bindings: dict[str, list[SpaceAgentBinding]] = {}
        self._conversations: dict[str, list[SpaceConversationMessage]] = {}

    def list_agents(self, space_id: str) -> list[dict[str, Any]]:
        bindings = self._bindings.get(space_id, [])
        logger.debug("Listed %d agents in space %s", len(bindings), space_id)
        return [b.to_dict() for b in bindings]

    def add_agent(
        self, space_id: str, agent_id: str, call_permission: str = "all_member"
    ) -> dict[str, Any]:
        if space_id not in self._bindings:
            self._bindings[space_id] = []
        for b in self._bindings[space_id]:
            if b.agent_id == agent_id:
                return {
                    "status": "error",
                    "message": f"Agent {agent_id} already in space {space_id}",
                }
        binding = SpaceAgentBinding(
            agent_id=agent_id,
            space_id=space_id,
            call_permission=call_permission,
            added_at=time.time(),
        )
        self._bindings[space_id].append(binding)
        logger.info("Added agent %s to space %s", agent_id, space_id)
        return {"status": "ok", "binding": binding.to_dict()}

    def remove_agent(self, space_id: str, agent_id: str) -> dict[str, Any]:
        bindings = self._bindings.get(space_id, [])
        before = len(bindings)
        self._bindings[space_id] = [b for b in bindings if b.agent_id != agent_id]
        if len(self._bindings[space_id]) == before:
            return {
                "status": "error",
                "message": f"Agent {agent_id} not in space {space_id}",
            }
        logger.info("Removed agent %s from space %s", agent_id, space_id)
        return {"status": "ok"}

    def check_permission(
        self, space_id: str, agent_id: str, caller_role: str = "member"
    ) -> bool:
        bindings = self._bindings.get(space_id, [])
        for b in bindings:
            if b.agent_id == agent_id:
                if b.call_permission == "all_member":
                    return True
                if b.call_permission == "admin_only" and caller_role == "admin":
                    return True
                return False
        return False

    async def call_agent(
        self, space_id: str, agent_id: str, messages: list[dict], stream: bool = False
    ) -> dict[str, Any]:
        bindings = self._bindings.get(space_id, [])
        binding = None
        for b in bindings:
            if b.agent_id == agent_id:
                binding = b
                break
        if not binding:
            return {
                "status": "error",
                "message": f"Agent {agent_id} not in space {space_id}",
            }

        for msg in messages:
            self._add_message(
                space_id,
                SpaceConversationMessage(
                    role=msg.get("role", "user"),
                    content=msg.get("content", ""),
                    sender=msg.get("sender", agent_id),
                    timestamp=time.time(),
                ),
            )

        binding.last_called_at = time.time()
        binding.call_count += 1
        logger.info(
            "Called agent %s in space %s (stream=%s)", agent_id, space_id, stream
        )
        return {
            "status": "ok",
            "agent_id": agent_id,
            "space_id": space_id,
            "stream": stream,
            "message_count": len(messages),
        }

    def get_agent_status(self, space_id: str, agent_id: str) -> dict[str, Any]:
        bindings = self._bindings.get(space_id, [])
        for b in bindings:
            if b.agent_id == agent_id:
                return {"status": "ok", "binding": b.to_dict()}
        return {
            "status": "error",
            "message": f"Agent {agent_id} not in space {space_id}",
        }

    def _add_message(self, space_id: str, msg: SpaceConversationMessage):
        if space_id not in self._conversations:
            self._conversations[space_id] = []
        self._conversations[space_id].append(msg)
        if len(self._conversations[space_id]) > 10000:
            self._conversations[space_id] = self._conversations[space_id][-10000:]

    def inject_context(
        self,
        space_id: str,
        mode: str = "full",
        recent_n: int = 50,
        enable_rag: bool = False,
    ) -> list[dict[str, Any]]:
        messages = self._conversations.get(space_id, [])
        if not messages:
            logger.debug("No conversation history for space %s", space_id)
            return []

        if mode == "full":
            result = [m.to_dict() for m in messages]
        elif mode == "recent_n":
            result = [m.to_dict() for m in messages[-recent_n:]]
        elif mode == "rag":
            result = [m.to_dict() for m in messages[-recent_n:]]
            if enable_rag:
                logger.debug(
                    "RAG context injection would query knowledge base for space %s",
                    space_id,
                )
        else:
            result = [m.to_dict() for m in messages[-50:]]

        logger.info(
            "Injected %d messages (mode=%s) for space %s", len(result), mode, space_id
        )
        return result
