# Issue #714 part 3: trajectory_writer emits ONLY the normative
# fusion_harness.trajectory schema (JSONL, one TrajectoryRecord per line,
# kernel event set, model_call_meta privacy rule); the reader keeps
# pre-#714 run-shaped .json files readable — gradual adoption, no history
# rewrite.
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fusion_harness.trajectory import (
    EVENTS,
    TrajectorySchemaError,
)
from fusion_harness.trajectory import (
    TrajectoryRecord as KernelTrajectoryRecord,
)

from agent_runtime.context import AgentEvent, AgentEventType
from agent_runtime.skill_drafter import TrajectoryMiner
from agent_runtime.trajectory_writer import (
    TrajectoryWriter,
    normalize_any,
    read_run,
    validate_jsonl,
)


def _writer(tmp_path: Path) -> TrajectoryWriter:
    return TrajectoryWriter(output_dir=tmp_path / "traces")


def _rec_from_line(d: dict) -> KernelTrajectoryRecord:
    return KernelTrajectoryRecord(
        run_id=d["run_id"],
        node_id=d["node_id"],
        member=d.get("member", ""),
        ts=d["ts"],
        event=d["event"],
        payload=d.get("payload") or {},
        duration_ms=d.get("duration_ms", 0),
        status=d.get("status", ""),
    )


def test_flush_emits_only_normative_schema(tmp_path: Path):
    w = _writer(tmp_path)
    tid = w.start(session_id="s1", graph_id="g1", graph_name="gn", agent_id="a1")
    w.record_event("s1", AgentEvent(type=AgentEventType.START, node_id="n1").to_dict())
    w.record_event(
        "s1",
        AgentEvent(type=AgentEventType.TOOL_CALL, name="t", node_id="n1").to_dict(),
    )
    path = Path(w.flush("s1", status="completed"))
    assert path.suffix == ".jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    for line in lines:
        rec = _rec_from_line(line)
        assert rec.event in EVENTS
        assert rec.run_id == tid
        assert rec.node_id or rec.member
    assert lines[-1]["event"] == "end"
    assert lines[-1]["payload"]["schema"] == "harness_trajectory.v1"
    assert lines[-1]["payload"]["graph_name"] == "gn"


def test_model_call_meta_privacy_rule_enforced(tmp_path: Path):
    w = _writer(tmp_path)
    w.start(session_id="s2", graph_id="g2")
    w.record_event(
        "s2",
        {
            "type": "think",
            "node_id": "n1",
            "timestamp": time.time(),
            "content": "SECRET PROMPT OUTPUT",
            "usage": {"total_tokens": 42},
        },
    )
    path = Path(w.flush("s2"))
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    meta = next(r for r in lines if r["event"] == "model_call_meta")
    banned = ("prompt", "completion", "messages", "content", "text")
    assert not [k for k in meta["payload"] if k.lower() in banned]
    assert meta["payload"]["legacy_event"] == "think"
    assert meta["payload"]["usage"]["total_tokens"] == 42


def test_kernel_rejects_leaking_meta_but_writer_strips(tmp_path: Path):
    with pytest.raises(TrajectorySchemaError):
        KernelTrajectoryRecord(
            run_id="r", node_id="n", event="model_call_meta", payload={"content": "x"}
        )


def test_old_format_json_still_readable(tmp_path: Path):
    # Compat fixture: pre-#714 run-shaped JSON file stays readable through
    # read_run and appears in list_trajectories.
    w = _writer(tmp_path)
    legacy = {
        "trace_id": "legacy01",
        "session_id": "s-old",
        "graph_id": "g-old",
        "graph_name": "Old Graph",
        "agent_id": "",
        "started_at": time.time() - 10,
        "finished_at": time.time(),
        "status": "error",
        "iteration_count": 2,
        "max_iterations": 25,
        "events": [{"type": "error", "node_id": "n1", "content": "boom"}],
        "messages": [{"role": "user", "content": "hi"}],
        "tool_calls": [{"name": "t1", "args": {}, "timestamp": time.time()}],
        "node_transitions": [],
        "error": "boom",
        "token_usage": {},
        "hit_provenance": {},
        "duration_ms": 10000,
    }
    (w.output_dir / "1000_legacy01.json").write_text(json.dumps(legacy))
    run = read_run(w.output_dir / "1000_legacy01.json")
    assert run["error"] == "boom"
    assert run["messages"][0]["role"] == "user"
    assert run["tool_calls"][0]["name"] == "t1"
    listed = [m for m in w.list_trajectories() if m["trace_id"] == "legacy01"]
    assert listed and listed[0]["status"] == "error"
    # kernel normalizer also still adapts the legacy shape
    recs = normalize_any(legacy)
    assert isinstance(recs, list) and recs[0].event == "error"


def test_new_format_reads_back_as_run_shape(tmp_path: Path):
    # eval_runner.from_trace / skill_drafter contract: run-shaped keys come
    # back from a normative .jsonl file through read_run.
    w = _writer(tmp_path)
    w.start(session_id="s3", graph_id="g3", graph_name="G3", agent_id="a3")
    w.record_event(
        "s3", AgentEvent(type=AgentEventType.ERROR, content="kaboom", node_id="n").to_dict()
    )
    ctx_msgs = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    w.record_messages("s3", ctx_msgs)
    w.record_hit_provenance("s3", {"memory": [{"id": "m1"}]})
    w.flush("s3", status="failed")
    f = next((w.output_dir).glob("*.jsonl"))
    run = read_run(f)
    assert run["trace_id"]
    assert run["status"] == "error"
    assert run["error"] == "kaboom"
    assert run["graph_id"] == "g3"
    assert run["messages"][-1]["content"] == "answer"
    assert run["hit_provenance"]["memory"][0]["id"] == "m1"
    valid, total = validate_jsonl(f)
    assert valid == total and total >= 2


def test_skill_miner_reads_new_format(tmp_path: Path):
    w = _writer(tmp_path)
    for i, err in enumerate(["boom A", "boom A", "boom B"]):
        sid = f"m{i}"
        w.start(session_id=sid, graph_id=f"g{i}")
        w.record_event(
            sid, AgentEvent(type=AgentEventType.ERROR, content=err, node_id="n").to_dict()
        )
        w.flush(sid, status="completed")
    miner = TrajectoryMiner(writer=w, min_count=2)
    candidates = miner.mine()
    assert candidates, "failure pattern must cluster from normative jsonl traces"
    assert candidates[0]["count"] >= 2


def test_unknown_event_maps_into_normative_set(tmp_path: Path):
    w = _writer(tmp_path)
    w.start(session_id="s4", graph_id="g4")
    w.record_event(
        "s4",
        {"type": "permission_request", "node_id": "n1", "timestamp": time.time(), "tool": "t"},
    )
    path = Path(w.flush("s4"))
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    ev = next(r for r in lines if r["payload"].get("legacy_event"))
    assert ev["event"] in EVENTS
