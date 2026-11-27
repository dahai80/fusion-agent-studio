# Trajectory writer — persists structured agent execution trajectories.
#
# D1 trajectory flywheel. #714 part 3 (K3): the writer now emits ONLY the normative
# fusion_harness.trajectory schema — one JSONL file per run
# (<ms>_<trace>.jsonl), one TrajectoryRecord per line, written through the
# kernel TrajectoryWriter. Studio event types map onto the normative event
# set via the kernel's own legacy-event map (the same map the kernel's
# reader-side studio adapter uses), and model_call_meta records are
# sanitized against the kernel privacy rule (no prompt/completion content).
#
# Migration is gradual, no history rewrite: pre-#714 run-shaped .json files
# stay on disk and stay readable. read_run() normalizes BOTH shapes into the
# run-shaped dict consumers (eval_runner.from_trace, skill_drafter miner)
# have always parsed; list_trajectories() lists both.
#
# Importers: agent_runtime/runtime.py (execute_graph_inner)
# API: TrajectoryWriter.start/record_event/record_messages/flush
#      read_run(path) / TrajectoryWriter.list_trajectories()

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fusion_harness.trajectory import (
    SCHEMA_VERSION as KERNEL_SCHEMA_VERSION,
)
from fusion_harness.trajectory import (
    TrajectoryRecord as KernelTrajectoryRecord,
)
from fusion_harness.trajectory import (
    TrajectorySchemaError,
    _map_legacy_event,
    normalize_record,
)
from fusion_harness.trajectory import (
    TrajectoryWriter as KernelTrajectoryWriter,
)

logger = logging.getLogger(__name__)

TRAJECTORY_DIR = Path.home() / ".fusion" / "trajectories" / "agent-studio"

RUN_SCHEMA = "harness_trajectory.v%d" % KERNEL_SCHEMA_VERSION

# Kernel privacy rule (trajectory.py __post_init__): model_call_meta payload
# must not carry prompt/completion content. Mirrored here so the writer
# sanitizes BEFORE construction instead of failing the graph run.
_MC_META_BANNED = ("prompt", "completion", "messages", "content", "text")


def _to_ms(ts) -> int:
    # studio events carry time.time() float seconds; kernel records carry ms
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return int(v * 1000) if v < 10**12 else int(v)


def _sanitize_mc_meta(payload: dict) -> dict:
    leaked = [k for k in payload if str(k).lower() in _MC_META_BANNED]
    if not leaked:
        return payload
    logger.warning(
        "trajectory model_call_meta payload carried prompt/completion content "
        "(keys=%s) — stripped per harness privacy rule",
        leaked,
    )
    return {k: v for k, v in payload.items() if str(k).lower() not in _MC_META_BANNED}


@dataclass
class TrajectoryRecord:
    # In-memory accumulator only — NOT the on-disk schema. On flush the
    # accumulated events/messages are re-shaped into normative kernel records.
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
    # #697: per-run asset hit provenance (memory/skill) for eval attribution.
    hit_provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex[:16]
        if not self.started_at:
            self.started_at = time.time()


def _event_records(record: TrajectoryRecord) -> list[KernelTrajectoryRecord]:
    # Studio event dicts -> normative kernel records. Mirrors the kernel's
    # own _adapt_studio flattening: same event map, same node_id fallback
    # chain, same per-record status rule.
    run_id = record.trace_id
    member = record.agent_id
    out = []
    for ev in record.events:
        if not isinstance(ev, dict):
            continue
        payload = {k: v for k, v in ev.items() if k not in ("type", "timestamp")}
        raw_event = str(ev.get("type") or "")
        event = _map_legacy_event(raw_event, payload)
        if event == "model_call_meta":
            payload = _sanitize_mc_meta(payload)
        node_id = str(ev.get("node_id") or record.graph_id or record.session_id or run_id or "run")
        try:
            out.append(
                KernelTrajectoryRecord(
                    run_id=run_id,
                    node_id=node_id,
                    member=member,
                    ts=_to_ms(ev.get("timestamp")),
                    event=event,
                    payload=payload,
                    status="error" if raw_event == "error" else "",
                )
            )
        except TrajectorySchemaError as e:
            logger.warning("trajectory event skipped (schema): %s", e)
    return out


def _end_record(record: TrajectoryRecord, status: str) -> KernelTrajectoryRecord:
    # Run summary rides the normative "end" record payload: every legacy
    # top-level key stays reachable through read_run() so eval replay and
    # skill mining keep working unchanged.
    payload = {
        "schema": RUN_SCHEMA,
        "session_id": record.session_id,
        "graph_id": record.graph_id,
        "graph_name": record.graph_name,
        "agent_id": record.agent_id,
        "iteration_count": record.iteration_count,
        "max_iterations": record.max_iterations,
        "error": record.error,
        "token_usage": record.token_usage,
        "hit_provenance": record.hit_provenance,
        "messages": record.messages,
        "tool_calls": record.tool_calls,
        "node_transitions": record.node_transitions,
    }
    node_id = record.graph_id or record.session_id or record.trace_id or "run"
    return KernelTrajectoryRecord(
        run_id=record.trace_id,
        node_id=node_id,
        member=record.agent_id,
        event="end",
        payload=payload,
        duration_ms=int((record.finished_at - record.started_at) * 1000)
        if record.finished_at
        else 0,
        status=status,
    )


def read_run(path) -> dict | None:
    # Normalized run-shaped view over one trajectory file. Legacy .json
    # (pre-#714 run shape) passes through as-is; normative .jsonl is
    # rebuilt from the end record's payload so consumers see the same keys.
    p = Path(path)
    try:
        if p.suffix == ".jsonl":
            recs = [
                json.loads(line)
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if not recs:
                return None
            end = next((r for r in reversed(recs) if r.get("event") == "end"), recs[-1])
            run = dict(end.get("payload") or {})
            run["trace_id"] = end.get("run_id", run.get("trace_id", ""))
            run["status"] = end.get("status") or run.get("status", "")
            run["duration_ms"] = end.get("duration_ms", 0)
            return run
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("trajectory read failed path=%s: %s", p, e)
        return None


def validate_jsonl(path) -> tuple[int, int]:
    # Validate every line of a .jsonl trajectory against the normative
    # schema (kernel TrajectoryRecord construction). Returns (valid, total).
    p = Path(path)
    total = valid = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            data = json.loads(line)
            KernelTrajectoryRecord(
                run_id=data.get("run_id", ""),
                node_id=data.get("node_id", ""),
                member=data.get("member", ""),
                ts=data.get("ts", 0),
                event=data.get("event", ""),
                payload=data.get("payload") or {},
                duration_ms=data.get("duration_ms", 0),
                status=data.get("status", ""),
            )
            valid += 1
        except (ValueError, TypeError, TrajectorySchemaError) as e:
            logger.warning("trajectory line invalid path=%s: %s", p, e)
    return valid, total


def normalize_any(data: dict):
    # Reader-side adapter straight from the kernel: legacy cowork/studio
    # shapes and normative lines all normalize; unknown shapes raise loudly.
    return normalize_record(data)


class TrajectoryWriter:
    # Audit P-4: cap on _records. Abandoned sessions (SSE dropped early,
    # finally never runs, flush never called) keep their TrajectoryRecord for
    # the whole daemon lifetime = silent memory leak. Above the cap, evict the
    # oldest unflushed records and log it (not persisted, since their state
    # integrity cannot be judged; this only bounds memory growth).
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
            record.trace_id,
            session_id,
            graph_name,
        )
        return record.trace_id

    def _evict_if_needed(self) -> None:
        # Audit R-8/P2-17: above the cap, evict the oldest unflushed records.
        # Previously log-only without persisting = silent trace loss. Now they
        # are persisted (status=evicted_abandoned) to keep the data; only the
        # memory is freed.
        if len(self._records) <= self._MAX_RECORDS:
            return
        oldest = sorted(self._records.items(), key=lambda kv: kv[1].started_at)
        evict_count = len(self._records) - self._MAX_RECORDS
        for sid, rec in oldest[:evict_count]:
            self._records.pop(sid, None)
            self._write_record(rec, "evicted_abandoned")
            logger.warning(
                "TrajectoryWriter evicted abandoned record to disk (session=%s "
                "trace=%s events=%d) — flush never called (SSE early-cancel?)",
                sid,
                rec.trace_id,
                len(rec.events),
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
            record.node_transitions.append(
                {
                    "node_id": node_id,
                    "iteration": record.iteration_count,
                    "timestamp": event.get("timestamp", time.time()),
                }
            )
        if etype == "tool_call" or etype == "tool_call_start":
            record.tool_calls.append(
                {
                    "name": event.get("name", ""),
                    "args": event.get("args", {}),
                    "timestamp": event.get("timestamp", time.time()),
                }
            )
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

    def record_hit_provenance(self, session_id: str, provenance: dict) -> None:
        # #697: merge per-run asset hit provenance into the record so eval
        # replay reproduces identical attribution.
        record = self._records.get(session_id)
        if record is None:
            return
        for key, hits in (provenance or {}).items():
            if isinstance(hits, list):
                record.hit_provenance.setdefault(key, []).extend(hits)
            else:
                record.hit_provenance[key] = hits

    def _write_record(self, record, status: str) -> str | None:
        # Flush = emit ONLY the normative schema: per-event records + one end
        # summary record, JSONL through the kernel TrajectoryWriter.
        record.finished_at = time.time()
        if record.status != "error":
            record.status = status
        final_status = record.status
        fname = f"{int(record.started_at * 1000)}_{record.trace_id}.jsonl"
        fpath = self.output_dir / fname
        try:
            kwriter = KernelTrajectoryWriter(str(fpath))
            written = 0
            for rec in _event_records(record):
                kwriter.write(rec)
                written += 1
            kwriter.write(_end_record(record, final_status))
            logger.info(
                "Trajectory flushed: %s (%d event records, %d tool_calls, status=%s)",
                fpath.name,
                written,
                len(record.tool_calls),
                final_status,
            )
            # Audit P1-13/3M-1: rotate by file count after flush. Previously one
            # JSON per session was never deleted, accumulating GB-scale
            # trajectories on long runs. Above max (default 1000, tune via
            # FUSION_TRAJECTORY_MAX_FILES) delete the oldest N.
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
        # Audit P1-13/3M-1: rotate trajectory files by count, delete the oldest
        # (legacy .json + normative .jsonl).
        import os as _os

        try:
            max_files = int(_os.environ.get("FUSION_TRAJECTORY_MAX_FILES", "1000"))
        except ValueError:
            max_files = 1000
        if max_files <= 0:
            return
        try:
            files = sorted(
                list(self.output_dir.glob("*.json")) + list(self.output_dir.glob("*.jsonl")),
                key=lambda p: p.stat().st_mtime,
            )
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
        try:
            files = sorted(
                list(self.output_dir.glob("*.json")) + list(self.output_dir.glob("*.jsonl")),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            return []
        results = []
        for f in files[:limit]:
            run = read_run(f)
            if run is None:
                continue
            if f.suffix == ".jsonl":
                try:
                    n_events = sum(
                        1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip()
                    )
                    started_at = 0
                    for line in f.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            started_at = json.loads(line).get("ts", 0) / 1000
                            break
                except (OSError, json.JSONDecodeError):
                    n_events, started_at = 0, 0
                results.append(
                    {
                        "trace_id": run.get("trace_id", ""),
                        "session_id": run.get("session_id", ""),
                        "graph_name": run.get("graph_name", ""),
                        "status": run.get("status", ""),
                        "started_at": started_at,
                        "duration_ms": run.get("duration_ms", 0),
                        "events": n_events,
                        "file": f.name,
                    }
                )
            else:
                results.append(
                    {
                        "trace_id": run.get("trace_id", ""),
                        "session_id": run.get("session_id", ""),
                        "graph_name": run.get("graph_name", ""),
                        "status": run.get("status", ""),
                        "started_at": run.get("started_at", 0),
                        "duration_ms": run.get("duration_ms", 0),
                        "events": len(run.get("events", [])),
                        "file": f.name,
                    }
                )
        return results


_writer: TrajectoryWriter | None = None


def get_trajectory_writer() -> TrajectoryWriter:
    global _writer
    if _writer is None:
        _writer = TrajectoryWriter()
    return _writer
