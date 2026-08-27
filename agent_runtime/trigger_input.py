"""Trigger input schema — frozen contract for fusion-event -> task.submit.

#240: task.submit `input` is a JSON-encoded string with a frozen schema so
fusion-event (producer) and agent-studio DAG nodes (consumer) decode it
identically. Contract authority: fusion-event (D-10). Backward compatible:
helper returns None on parse failure -> caller falls back to raw string.

Schema (input string decodes to):
    {
      "trigger_id": "<uuid>",          # E2 first-class correlation id
      "event": {                        # normalized SystemEvent
        "event_id": "<uuid>",
        "type": "fileModified|processTerminated|clipboardChanged|networkStatusChanged",
        "target_path": "/path",
        "timestamp": 1693027200000,
        "payload": {...},
        "node_id": "macbook"
      },
      "context": "<string from fusion-memory, may be empty>",
      "rule_name": "swift-watch",
      "node_id": "macbook"
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TriggerEvent:
    event_id: str = ""
    type: str = ""
    target_path: str = ""
    timestamp: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    node_id: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "target_path": self.target_path,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "node_id": self.node_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TriggerEvent:
        return cls(
            event_id=str(d.get("event_id", "")),
            type=str(d.get("type", "")),
            target_path=str(d.get("target_path", "")),
            timestamp=int(d.get("timestamp", 0) or 0),
            payload=d.get("payload", {}) if isinstance(d.get("payload"), dict) else {},
            node_id=str(d.get("node_id", "")),
        )


@dataclass
class TriggerInput:
    trigger_id: str = ""
    event: TriggerEvent = field(default_factory=TriggerEvent)
    context: str = ""
    rule_name: str = ""
    node_id: str = ""

    def to_dict(self) -> dict:
        return {
            "trigger_id": self.trigger_id,
            "event": self.event.to_dict(),
            "context": self.context,
            "rule_name": self.rule_name,
            "node_id": self.node_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TriggerInput:
        event_raw = d.get("event", {})
        event = TriggerEvent.from_dict(event_raw) if isinstance(event_raw, dict) else TriggerEvent()
        return cls(
            trigger_id=str(d.get("trigger_id", "")),
            event=event,
            context=str(d.get("context", "")),
            rule_name=str(d.get("rule_name", "")),
            node_id=str(d.get("node_id", "")),
        )


def parse_trigger_input(input_str: str) -> TriggerInput | None:
    # #240: 单一解码入口. DAG 节点/cron handler 用此 helper, 不各自 json.loads.
    # 返回 None -> 调用方回退原始字符串 (向后兼容旧自由格式 input).
    if not input_str:
        return None
    try:
        data = json.loads(input_str)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.debug("trigger input not JSON, fallback to raw string: %s", exc)
        return None
    if not isinstance(data, dict):
        logger.debug("trigger input JSON not object, fallback to raw string")
        return None
    return TriggerInput.from_dict(data)
