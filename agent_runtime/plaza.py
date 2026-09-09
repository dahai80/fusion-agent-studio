"""Plaza broadcast mechanism — multi-agent shared log stream with @Mention,
supervisor designate, 3-round circuit breaker, and human break-in."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .persistence import AgentStore

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


def _message_hash(channel: str, sender: str, content: str, round_number: int) -> str:
    # sha256 去重: 同 channel+sender+content+round 视为重复 (重试/崩溃恢复场景).
    raw = f"{channel}|{sender}|{content}|{round_number}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Plaza:
    def __init__(self, max_rounds: int = 3, store: AgentStore | None = None):
        self._max_rounds = max_rounds
        self._channels: dict[str, PlazaChannel] = {}
        self._messages: dict[str, list[PlazaMessage]] = {}
        self._subscriptions: dict[str, tuple[str, str, Callable[[PlazaMessage], None]]] = {}
        self._lock = threading.Lock()
        self._store = store
        if store is not None:
            self._load_from_store()
        logger.info(
            "Plaza initialized (max_rounds=%d, store=%s, channels=%d)",
            max_rounds,
            "on" if store else "off",
            len(self._channels),
        )

    def _load_from_store(self) -> None:
        # 启动: 从 SQLite 加载频道 + 消息进内存缓存. 内存 dict 降为缓存层.
        if self._store is None:
            return
        try:
            for ch_data in self._store.load_plaza_channels():
                channel = PlazaChannel(
                    name=ch_data.get("name", ""),
                    participants=ch_data.get("participants", []),
                    max_rounds=ch_data.get("max_rounds", self._max_rounds),
                    current_round=ch_data.get("current_round", 0),
                    is_suspended=ch_data.get("suspended", False),
                )
                self._channels[channel.name] = channel
                self._messages[channel.name] = []
            for msg_data in self._store.load_all_plaza_messages():
                ch_name = msg_data.get("channel", "")
                if ch_name not in self._messages:
                    self._messages[ch_name] = []
                msg = PlazaMessage(
                    id=msg_data.get("message_id", ""),
                    channel=ch_name,
                    sender=msg_data.get("sender", ""),
                    content=msg_data.get("payload", {}).get("content", ""),
                    mentions=msg_data.get("payload", {}).get("mentions", []),
                    round_number=msg_data.get("payload", {}).get("round_number", 0),
                    timestamp=msg_data.get("ts", 0.0),
                    metadata=msg_data.get("payload", {}).get("metadata", {}),
                )
                self._messages[ch_name].append(msg)
            logger.info(
                "Plaza loaded from store: %d channels, %d messages",
                len(self._channels),
                sum(len(v) for v in self._messages.values()),
            )
        except Exception:
            logger.exception("Plaza _load_from_store failed, starting empty cache")

    def _persist_message(self, msg: PlazaMessage) -> bool:
        # 持久化消息 + 回读验证 + hash 去重. 返回 True=新写入, False=重复跳过.
        if self._store is None:
            return True
        hash_val = _message_hash(msg.channel, msg.sender, msg.content, msg.round_number)
        payload = {
            "content": msg.content,
            "mentions": msg.mentions,
            "round_number": msg.round_number,
            "metadata": msg.metadata,
        }
        written = self._store.save_plaza_message(
            message_id=msg.id,
            channel=msg.channel,
            sender=msg.sender,
            ts=msg.timestamp,
            payload=payload,
            hash_val=hash_val,
        )
        if not written:
            logger.info("Plaza message dedup skipped (hash=%s): id=%s", hash_val[:12], msg.id)
            return False
        # 回读验证: 写后立即确认落盘. 缺失=告警不静默 (源方案 §5.5).
        if not self._store.verify_plaza_message(msg.id):
            logger.error(
                "Plaza message readback VERIFY FAILED: id=%s channel=%s — missing after write",
                msg.id,
                msg.channel,
            )
        return True

    def _persist_channel(self, channel: PlazaChannel) -> None:
        if self._store is None:
            return
        try:
            self._store.save_plaza_channel(
                name=channel.name,
                participants=channel.participants,
                max_rounds=channel.max_rounds,
                current_round=channel.current_round,
                suspended=channel.is_suspended,
            )
        except Exception:
            logger.exception("Plaza _persist_channel failed: %s", channel.name)

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
            self._persist_channel(channel)
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
            if self._store is not None:
                try:
                    self._store.delete_plaza_channel(name)
                except Exception:
                    logger.exception("Plaza delete_channel store fail: %s", name)
            subs_to_remove = [sid for sid, (ch, _, _) in self._subscriptions.items() if ch == name]
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
            self._persist_message(msg)
            self._persist_channel(ch)

            if self._check_circuit_breaker_unlocked(channel):
                ch.is_suspended = True
                self._persist_channel(ch)
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
            self._persist_message(msg)
            self._persist_channel(ch)

            if self._check_circuit_breaker_unlocked(channel):
                ch.is_suspended = True
                self._persist_channel(ch)
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
            self._persist_message(msg)
            self._persist_channel(ch)
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
