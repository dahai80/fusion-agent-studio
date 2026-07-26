"""Plaza broadcast mechanism — multi-agent shared log stream with @Mention,
supervisor designate, 3-round circuit breaker, and human break-in."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

MENTION_PATTERN = re.compile(r"@(\w+)")


@dataclass
class PlazaMessage:
    id: str = ""
    channel: str = ""
    sender: str = ""
    content: str = ""
    mentions: list[str] = field(default_factory=list)
    round_number: int = 0
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "sender": self.sender,
            "content": self.content,
            "mentions": self.mentions,
            "round_number": self.round_number,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlazaMessage:
        return cls(
            id=data.get("id", ""),
            channel=data.get("channel", ""),
            sender=data.get("sender", ""),
            content=data.get("content", ""),
            mentions=data.get("mentions", []),
            round_number=data.get("round_number", 0),
            timestamp=data.get("timestamp", 0.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class PlazaChannel:
    name: str = ""
    participants: list[str] = field(default_factory=list)
    max_rounds: int = 3
    current_round: int = 0
    pending_queue: list[PlazaMessage] = field(default_factory=list)
    is_suspended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "participants": self.participants,
            "max_rounds": self.max_rounds,
            "current_round": self.current_round,
            "pending_queue": [m.to_dict() for m in self.pending_queue],
            "is_suspended": self.is_suspended,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlazaChannel:
        pending = [
            PlazaMessage.from_dict(m) if isinstance(m, dict) else m
            for m in data.get("pending_queue", [])
        ]
        return cls(
            name=data.get("name", ""),
            participants=data.get("participants", []),
            max_rounds=data.get("max_rounds", 3),
            current_round=data.get("current_round", 0),
            pending_queue=pending,
            is_suspended=data.get("is_suspended", False),
        )


def _parse_mentions(content: str) -> list[str]:
    return MENTION_PATTERN.findall(content)


class Plaza:
    def __init__(self, max_rounds: int = 3):
        self._max_rounds = max_rounds
        self._channels: dict[str, PlazaChannel] = {}
        self._messages: dict[str, list[PlazaMessage]] = {}
        self._subscriptions: dict[str, tuple[str, str, Callable[[PlazaMessage], None]]] = {}
        self._lock = threading.Lock()
        logger.info("Plaza initialized with max_rounds=%d", max_rounds)

    def create_channel(self, name: str, participants: list[str]) -> PlazaChannel:
        with self._lock:
            if name in self._channels:
                logger.warning("Plaza channel already exists: %s", name)
                return self._channels[name]
            channel = PlazaChannel(
                name=name,
                participants=list(participants),
                max_rounds=self._max_rounds,
            )
            self._channels[name] = channel
            self._messages[name] = []
            logger.info(
                "Plaza channel created: %s with %d participants",
                name,
                len(participants),
            )
            return channel

    def delete_channel(self, name: str) -> bool:
        with self._lock:
            if name not in self._channels:
                logger.warning("Plaza channel not found for delete: %s", name)
                return False
            del self._channels[name]
            self._messages.pop(name, None)
            subs_to_remove = [
                sid
                for sid, (ch, _, _) in self._subscriptions.items()
                if ch == name
            ]
            for sid in subs_to_remove:
                del self._subscriptions[sid]
            logger.info("Plaza channel deleted: %s", name)
            return True

    def broadcast(
        self,
        channel: str,
        sender: str,
        content: str,
        mentions: list[str] | None = None,
    ) -> PlazaMessage:
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None:
                logger.error("Plaza broadcast to unknown channel: %s", channel)
                raise ValueError(f"Channel not found: {channel}")

            if ch.is_suspended:
                logger.warning(
                    "Plaza broadcast rejected — channel %s is suspended",
                    channel,
                )
                raise ValueError(f"Channel is suspended: {channel}")

            parsed = _parse_mentions(content)
            effective_mentions = list(set((mentions or []) + parsed))

            if effective_mentions:
                effective_mentions = [
                    m for m in effective_mentions if m in ch.participants or m == "human"
                ]

            ch.current_round += 1
            msg = PlazaMessage(
                channel=channel,
                sender=sender,
                content=content,
                mentions=effective_mentions,
                round_number=ch.current_round,
            )

            self._messages[channel].append(msg)
            ch.pending_queue.append(msg)

            if self._check_circuit_breaker_unlocked(channel):
                ch.is_suspended = True
                logger.warning(
                    "Plaza circuit breaker TRIPPED on channel %s at round %d",
                    channel,
                    ch.current_round,
                )
            else:
                self._notify_subscribers(channel, msg)

            logger.info(
                "Plaza broadcast: channel=%s sender=%s round=%d mentions=%s",
                channel,
                sender,
                ch.current_round,
                effective_mentions,
            )
            return msg

    def get_messages(
        self,
        channel: str,
        since_id: str = "",
        limit: int = 100,
    ) -> list[PlazaMessage]:
        with self._lock:
            msgs = self._messages.get(channel, [])
            if since_id:
                start = 0
                for i, m in enumerate(msgs):
                    if m.id == since_id:
                        start = i + 1
                        break
                msgs = msgs[start:]
            return list(msgs[:limit])

    def designate_speaker(self, channel: str, agent_id: str) -> PlazaMessage:
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None:
                logger.error("Plaza designate_speaker on unknown channel: %s", channel)
                raise ValueError(f"Channel not found: {channel}")

            if ch.is_suspended:
                logger.warning(
                    "Plaza designate_speaker rejected — channel %s is suspended",
                    channel,
                )
                raise ValueError(f"Channel is suspended: {channel}")

            ch.current_round += 1
            msg = PlazaMessage(
                channel=channel,
                sender="supervisor",
                content=f"Designated speaker: @{agent_id}",
                mentions=[agent_id],
                round_number=ch.current_round,
                metadata={"action": "designate_speaker", "designated": agent_id},
            )

            self._messages[channel].append(msg)
            ch.pending_queue.append(msg)

            if self._check_circuit_breaker_unlocked(channel):
                ch.is_suspended = True
                logger.warning(
                    "Plaza circuit breaker TRIPPED on channel %s at round %d (after designate)",
                    channel,
                    ch.current_round,
                )
            else:
                self._notify_subscribers(channel, msg)

            logger.info(
                "Plaza designate_speaker: channel=%s agent=%s round=%d",
                channel,
                agent_id,
                ch.current_round,
            )
            return msg

    def human_break_in(self, channel: str, content: str) -> PlazaMessage:
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None:
                logger.error("Plaza human_break_in on unknown channel: %s", channel)
                raise ValueError(f"Channel not found: {channel}")

            cleared_count = len(ch.pending_queue)
            ch.pending_queue.clear()
            ch.current_round = 0
            ch.is_suspended = False

            msg = PlazaMessage(
                channel=channel,
                sender="human",
                content=content,
                mentions=[],
                round_number=0,
                metadata={"action": "human_break_in", "cleared_pending": cleared_count},
            )

            self._messages[channel].append(msg)
            self._notify_subscribers(channel, msg)

            logger.info(
                "Plaza human_break_in: channel=%s cleared=%d pending messages",
                channel,
                cleared_count,
            )
            return msg

    def check_circuit_breaker(self, channel: str) -> bool:
        with self._lock:
            return self._check_circuit_breaker_unlocked(channel)

    def _check_circuit_breaker_unlocked(self, channel: str) -> bool:
        ch = self._channels.get(channel)
        if ch is None:
            return False
        return ch.current_round >= ch.max_rounds

    def get_channel(self, name: str) -> PlazaChannel | None:
        with self._lock:
            return self._channels.get(name)

    def list_channels(self) -> list[PlazaChannel]:
        with self._lock:
            return list(self._channels.values())

    def subscribe(
        self,
        channel: str,
        agent_id: str,
        callback: Callable[[PlazaMessage], None],
    ) -> str:
        sub_id = uuid.uuid4().hex[:10]
        with self._lock:
            if channel not in self._channels:
                logger.error("Plaza subscribe to unknown channel: %s", channel)
                raise ValueError(f"Channel not found: {channel}")
            self._subscriptions[sub_id] = (channel, agent_id, callback)
            logger.info(
                "Plaza subscribe: channel=%s agent=%s sub_id=%s",
                channel,
                agent_id,
                sub_id,
            )
            return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        with self._lock:
            if subscription_id not in self._subscriptions:
                logger.warning("Plaza unsubscribe: unknown sub_id=%s", subscription_id)
                return False
            ch, agent_id, _ = self._subscriptions.pop(subscription_id)
            logger.info(
                "Plaza unsubscribe: channel=%s agent=%s sub_id=%s",
                ch,
                agent_id,
                subscription_id,
            )
            return True

    def get_pending_for(self, agent_id: str) -> list[PlazaMessage]:
        with self._lock:
            result: list[PlazaMessage] = []
            for ch in self._channels.values():
                for msg in ch.pending_queue:
                    is_mentioned = agent_id in msg.mentions
                    is_designated = (
                        msg.metadata.get("action") == "designate_speaker"
                        and msg.metadata.get("designated") == agent_id
                    )
                    if is_mentioned or is_designated:
                        result.append(msg)
            return result

    def _notify_subscribers(self, channel: str, msg: PlazaMessage) -> None:
        to_notify: list[tuple[str, Callable[[PlazaMessage], None]]] = []
        for sub_id, (ch, agent_id, callback) in self._subscriptions.items():
            if ch != channel:
                continue
            if agent_id in msg.mentions or msg.sender == "human":
                to_notify.append((sub_id, callback))

        for sub_id, callback in to_notify:
            try:
                callback(msg)
            except Exception:
                logger.exception(
                    "Plaza subscriber callback error: sub_id=%s channel=%s",
                    sub_id,
                    channel,
                )
