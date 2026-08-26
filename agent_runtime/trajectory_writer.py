"""Trajectory writer — persists structured agent execution trajectories.

D1 轨迹飞轮: writes each graph execution as a structured JSON trajectory
to ~/.fusion/trajectories/agent-studio/, preserving iteration count, tool
calls, node transitions, and branch structure (parent_id/children_ids)
for fusion-trainer consumption.

Importers: agent_runtime/runtime.py (execute_graph_inner)
API: TrajectoryWriter.start/record_event/record_messages/flush
Data schemas: TrajectoryRecord dataclass -> JSON file
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

TRAJECTORY_DIR = Path.home() / ".fusion" / "trajectories" / "agent-studio"


@dataclass
class TrajectoryRecord:
    trace_id: str = ""
    session_id: str = ""
    graph_id: str = ""
    graph_name: str = ""
    agent_id: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = "running"
    iteration_count: int = 0
    max_iterations: int = 25
    events: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    node_transitions: list[dict] = field(default_factory=list)
    error: str = ""
    token_usage: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex[:16]
        if not self.started_at:
            self.started_at = time.time()

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "graph_id": self.graph_id,
            "graph_name": self.graph_name,
            "agent_id": self.agent_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "iteration_count": self.iteration_count,
            "max_iterations": self.max_iterations,
            "events": self.events,
            "messages": self.messages,
            "tool_calls": self.tool_calls,
            "node_transitions": self.node_transitions,
            "error": self.error,
            "token_usage": self.token_usage,
            "duration_ms": (self.finished_at - self.started_at) * 1000
            if self.finished_at
            else 0,
        }


class TrajectoryWriter:
    # 审计 P-4: _records 上限. 被放弃会话 (SSE 早断, finally 不执行, flush 不调)
    # 的 TrajectoryRecord 永留至 daemon 生命期 = 静默内存泄漏. 超 cap 淘汰最老
    # 未 flush 记录并记日志 (不落盘, 因无法判定其状态完整性; 仅防内存无界增长).
    _MAX_RECORDS = 256

    def __init__(self, output_dir: Path | str | None = None):
        self.output_dir = Path(output_dir) if output_dir else TRAJECTORY_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, TrajectoryRecord] = {}
        logger.info("TrajectoryWriter initialized: %s", self.output_dir)

    def start(
        self,
        session_id: str,
        graph_id: str = "",
        graph_name: str = "",
        agent_id: str = "",
        max_iterations: int = 25,
    ) -> str:
        record = TrajectoryRecord(
            session_id=session_id,
            graph_id=graph_id,
            graph_name=graph_name,
            agent_id=agent_id,
            max_iterations=max_iterations,
        )
        self._records[session_id] = record
        self._evict_if_needed()
        logger.debug(
            "Trajectory started: trace=%s session=%s graph=%s",
            record.trace_id, session_id, graph_name,
        )
        return record.trace_id

    def _evict_if_needed(self) -> None:
        # 审计 R-8/P2-17: 超上限淘汰最老未 flush 记录. 原仅日志不落盘 = 静默丢 trace.
        # 现落盘 (status=evicted_abandoned) 保数据, 仅释放内存.
        if len(self._records) <= self._MAX_RECORDS:
            return
        oldest = sorted(
            self._records.items(), key=lambda kv: kv[1].started_at
        )
        evict_count = len(self._records) - self._MAX_RECORDS
        for sid, rec in oldest[:evict_count]:
            self._records.pop(sid, None)
            self._write_record(rec, "evicted_abandoned")
            logger.warning(
                "TrajectoryWriter evicted abandoned record to disk (session=%s "
                "trace=%s events=%d) — flush never called (SSE early-cancel?)",
                sid, rec.trace_id, len(rec.events),
            )

    def record_event(self, session_id: str, event: dict) -> None:
        record = self._records.get(session_id)
        if record is None:
            logger.debug("No trajectory record for session %s", session_id)
            return
        record.events.append(event)
        etype = event.get("type", "")
        node_id = event.get("node_id", "")
        if node_id and etype in ("start", "think", "tool_call", "tool_result"):
            record.node_transitions.append({
                "node_id": node_id,
                "iteration": record.iteration_count,
                "timestamp": event.get("timestamp", time.time()),
            })
        if etype == "tool_call" or etype == "tool_call_start":
            record.tool_calls.append({
                "name": event.get("name", ""),
                "args": event.get("args", {}),
                "timestamp": event.get("timestamp", time.time()),
            })
        if etype == "error":
            record.status = "error"
            record.error = event.get("content", "")

    def record_iteration(self, session_id: str, count: int) -> None:
        record = self._records.get(session_id)
        if record is not None:
            record.iteration_count = count

    def record_messages(self, session_id: str, messages: list[dict]) -> None:
        record = self._records.get(session_id)
        if record is not None:
            record.messages = [self._serialize_msg(m) for m in messages]

    def _serialize_msg(self, msg: dict) -> dict:
        out = {
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
        }
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            out["tool_call_id"] = msg["tool_call_id"]
        if msg.get("parent_id"):
            out["parent_id"] = msg["parent_id"]
        if msg.get("children_ids"):
            out["children_ids"] = msg["children_ids"]
        usage = msg.get("usage")
        if usage:
            out["usage"] = usage
        return out

    def record_token_usage(self, session_id: str, usage: dict) -> None:
        record = self._records.get(session_id)
        if record is not None:
            record.token_usage = usage

    def _write_record(self, record, status: str) -> str | None:
        # 审计 R-8/P2-17: 抽出落盘逻辑, flush + evict 共用 (evict 不再静默丢 trace).
        record.finished_at = time.time()
        if record.status != "error":
            record.status = status
        fname = f"{int(record.started_at * 1000)}_{record.trace_id}.json"
        fpath = self.output_dir / fname
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info(
                "Trajectory flushed: %s (%d events, %d tool_calls, status=%s)",
                fpath.name, len(record.events), len(record.tool_calls), record.status,
            )
            # 审计 P1-13/3M-1: flush 后按文件数轮转. 原每会话写一个 JSON 永不删,
            # 长跑积累 GB 级轨迹. 超 max (默认 1000, FUSION_TRAJECTORY_MAX_FILES
            # 调) 删最旧 N 个.
            self._rotate_files()
            return str(fpath)
        except OSError as e:
            logger.error("Failed to flush trajectory %s: %s", fpath, e)
            return None

    def flush(self, session_id: str, status: str = "completed") -> str | None:
        record = self._records.pop(session_id, None)
        if record is None:
            return None
        return self._write_record(record, status)

    def _rotate_files(self) -> None:
        # 审计 P1-13/3M-1: 轨迹文件按数轮转, 删最旧.
        import os as _os

        try:
            max_files = int(_os.environ.get("FUSION_TRAJECTORY_MAX_FILES", "1000"))
        except ValueError:
            max_files = 1000
        if max_files <= 0:
            return
        try:
            files = sorted(self.output_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        overflow = len(files) - max_files
        if overflow <= 0:
            return
        for old in files[:overflow]:
            try:
                old.unlink()
            except OSError:
                pass
        logger.info("Trajectory rotated: removed %d old files (max=%d)", overflow, max_files)

    def list_trajectories(self, limit: int = 50) -> list[dict]:
        files = sorted(self.output_dir.glob("*.json"), reverse=True)
        results = []
        for f in files[:limit]:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                results.append({
                    "trace_id": data.get("trace_id", ""),
                    "session_id": data.get("session_id", ""),
                    "graph_name": data.get("graph_name", ""),
                    "status": data.get("status", ""),
                    "started_at": data.get("started_at", 0),
                    "duration_ms": data.get("duration_ms", 0),
                    "events": len(data.get("events", [])),
                    "file": f.name,
                })
            except (OSError, json.JSONDecodeError):
                continue
        return results


_writer: TrajectoryWriter | None = None


def get_trajectory_writer() -> TrajectoryWriter:
    global _writer
    if _writer is None:
        _writer = TrajectoryWriter()
    return _writer
