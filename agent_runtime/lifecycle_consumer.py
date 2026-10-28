"""#485: production LifecycleBus consumer — session lifecycle feed.

The bus (fusion-harness funnels) emits tool/llm/agent lifecycle events that
previously had zero subscribers in production. SessionLifecycleFeed is that
subscriber: it mirrors bus events into a bounded per-session ring the
`sessions.lifecycle` RPC reads (governance event stream / canvas run-event
surface), forwards them into the daemon WS event ring, and reconstructs its
state from the bus JSONL after a daemon restart.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = ("seq", "ts", "domain", "event", "payload", "session_id")


class SessionLifecycleFeed:
    def __init__(self, maxlen: int = 10000, jsonl_dir: str | None = None, sink=None):
        self._lock = threading.Lock()
        self._maxlen = maxlen
        self._events: dict[str, deque] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._jsonl_dir = jsonl_dir
        self._sink = sink

    def attach(self, bus) -> None:
        # #485 AC1: production consumer subscribes to the lifecycle bus.
        bus.subscribe(self._on_event)
        logger.info("lifecycle feed attached to bus (jsonl_dir=%s)", self._jsonl_dir or "off")

    def _ingest(self, ev: dict) -> None:
        session_id = str(ev.get("session_id") or "") or "global"
        with self._lock:
            dq = self._events.setdefault(session_id, deque(maxlen=self._maxlen))
            dq.append(ev)
            counts = self._counts.setdefault(session_id, {})
            domain = str(ev.get("domain") or "")
            counts[domain] = counts.get(domain, 0) + 1

    def _on_event(self, ev) -> None:
        try:
            d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
            self._ingest(d)
            if self._sink is not None:
                try:
                    self._sink(d)
                except Exception as e:
                    logger.warning("lifecycle feed sink failed: %s", e)
        except Exception as e:
            logger.warning("lifecycle feed ingest failed: %s", e)

    def events(self, session_id: str, limit: int = 0) -> list[dict]:
        key = session_id or "global"
        with self._lock:
            out = list(self._events.get(key, ()))
        if limit:
            out = out[-limit:]
        return out

    def summary(self, session_id: str) -> dict:
        key = session_id or "global"
        with self._lock:
            counts = dict(self._counts.get(key, {}))
            evs = self._events.get(key)
            last_ts = evs[-1].get("ts", 0.0) if evs else 0.0
            total = len(evs) if evs else 0
        return {
            "session_id": session_id,
            "total_events": total,
            "by_domain": counts,
            "last_event_ts": last_ts,
        }

    def _jsonl_path(self, session_id: str) -> Path | None:
        if not self._jsonl_dir:
            return None
        return Path(self._jsonl_dir) / f"{session_id or 'global'}.jsonl"

    def replay_jsonl(self, session_id: str) -> list[dict]:
        # #485 AC3: pure read-back of the persisted event sequence.
        path = self._jsonl_path(session_id)
        if path is None or not path.exists():
            return []
        out: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    logger.warning("lifecycle jsonl bad line skipped: %s", path)
                    continue
                if all(k in rec for k in _REQUIRED_KEYS):
                    out.append(rec)
        except OSError as e:
            logger.warning("lifecycle jsonl read failed %s: %s", path, e)
            return []
        return out

    def restore_from_jsonl(self, session_id: str) -> int:
        # #485 AC2: restart reconstruction — a fresh feed (post-restart)
        # rebuilds its per-session state from the bus JSONL persistence.
        restored = 0
        for rec in self.replay_jsonl(session_id):
            self._ingest(rec)
            restored += 1
        if restored:
            logger.info(
                "lifecycle feed restored %d events from jsonl session=%s", restored, session_id
            )
        return restored
