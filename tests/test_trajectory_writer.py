import json
from pathlib import Path

from agent_runtime.context import AgentContext, AgentEvent, AgentEventType
from agent_runtime.trajectory_writer import TrajectoryRecord, TrajectoryWriter


def test_trajectory_record_defaults():
    rec = TrajectoryRecord(session_id="s1")
    assert rec.trace_id
    assert rec.started_at > 0
    assert rec.status == "running"
    assert rec.events == []


def test_trajectory_writer_start_and_flush(tmp_path: Path):
    writer = TrajectoryWriter(output_dir=tmp_path)
    trace_id = writer.start(
        session_id="sess-1",
        graph_id="g1",
        graph_name="Test Graph",
        agent_id="a1",
        max_iterations=10,
    )
    assert trace_id

    ev = AgentEvent(type=AgentEventType.START, content="start", node_id="n1")
    writer.record_event("sess-1", ev.to_dict())
    writer.record_iteration("sess-1", 3)

    ctx = AgentContext()
    ctx.session_id = "sess-1"
    ctx.add_message("user", "hello")
    ctx.add_message("assistant", "hi there")
    writer.record_messages("sess-1", ctx.messages)

    path = writer.flush("sess-1", status="completed")
    assert path is not None
    p = Path(path)
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["session_id"] == "sess-1"
    assert data["graph_name"] == "Test Graph"
    assert data["agent_id"] == "a1"
    assert data["status"] == "completed"
    assert data["iteration_count"] == 3
    assert data["max_iterations"] == 10
    assert len(data["events"]) == 1
    assert data["events"][0]["type"] == "start"
    assert len(data["node_transitions"]) == 1
    assert data["node_transitions"][0]["node_id"] == "n1"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][0]["content"] == "hello"
    assert data["duration_ms"] >= 0


def test_trajectory_writer_tool_call_capture(tmp_path: Path):
    writer = TrajectoryWriter(output_dir=tmp_path)
    writer.start(session_id="s2", graph_name="Tool Graph")
    ev = AgentEvent(
        type=AgentEventType.TOOL_CALL,
        name="calculator",
        args={"expr": "1+1"},
    )
    writer.record_event("s2", ev.to_dict())
    writer.flush("s2")
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["tool_calls"]) == 1
    assert data["tool_calls"][0]["name"] == "calculator"
    assert data["tool_calls"][0]["args"]["expr"] == "1+1"


def test_trajectory_writer_error_status(tmp_path: Path):
    writer = TrajectoryWriter(output_dir=tmp_path)
    writer.start(session_id="s3")
    err_ev = AgentEvent(type=AgentEventType.ERROR, content="boom")
    writer.record_event("s3", err_ev.to_dict())
    writer.flush("s3", status="completed")
    files = list(tmp_path.glob("*.json"))
    data = json.loads(files[0].read_text())
    assert data["status"] == "error"
    assert data["error"] == "boom"


def test_trajectory_writer_list(tmp_path: Path):
    writer = TrajectoryWriter(output_dir=tmp_path)
    for i in range(3):
        writer.start(session_id=f"s{i}", graph_name=f"g{i}")
        writer.flush(f"s{i}")
    listed = writer.list_trajectories()
    assert len(listed) == 3
    assert "trace_id" in listed[0]
    assert "file" in listed[0]


def test_trajectory_writer_no_record_for_unknown_session(tmp_path: Path):
    writer = TrajectoryWriter(output_dir=tmp_path)
    assert writer.flush("nonexistent") is None
    ev = AgentEvent(type=AgentEventType.START, content="x")
    writer.record_event("unknown", ev.to_dict())
    assert writer.list_trajectories() == []


def test_runtime_trajectory_integration(tmp_path: Path, monkeypatch):
    import asyncio

    from agent_runtime import trajectory_writer as tw_mod
    from agent_runtime.graph import AgentGraph, NodeConfig
    from agent_runtime.runtime import AgentRuntime
    from tools import create_default_registry

    writer = TrajectoryWriter(output_dir=tmp_path)
    monkeypatch.setattr(tw_mod, "_writer", writer)

    graph = AgentGraph(name="Integration Test")
    start = NodeConfig(type="start")
    end = NodeConfig(type="end")
    graph.add_node("start", start)
    graph.add_node("end", end)
    graph.add_edge("start", "end")
    graph.start_node_id = "start"

    runtime = AgentRuntime(tool_registry=create_default_registry())

    async def collect():
        out = []
        async for e in runtime.execute_graph(graph, initial_input="hello"):
            out.append(e)
        return out

    events = asyncio.run(collect())
    assert any(e.type == AgentEventType.END for e in events)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["graph_name"] == "Integration Test"
    assert len(data["events"]) >= 2
    assert data["status"] in ("completed", "error")
