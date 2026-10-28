"""#485: LifecycleBus production consumer — feed, replay, JSONL round-trip."""
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.funnels import LifecycleBus
from agent_runtime.lifecycle_consumer import SessionLifecycleFeed


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    return await handler(params or {})


def _emit_sample(bus, session_id="s1", n=3):
    for i in range(n):
        bus.emit("tool", "tool_before", {"tool": f"t{i}"}, session_id=session_id)
    bus.emit("agent", "subagent_start", {"name": "researcher"}, session_id=session_id)


def test_feed_subscribes_and_mirrors_events():
    # AC1: a production consumer subscribes to the bus and mirrors events.
    bus = LifecycleBus()
    feed = SessionLifecycleFeed()
    feed.attach(bus)
    _emit_sample(bus)
    events = feed.events("s1")
    assert len(events) == 4
    assert events[0]["event"] == "tool_before"
    assert events[-1]["domain"] == "agent"
    assert feed.summary("s1")["by_domain"] == {"tool": 3, "agent": 1}


def test_feed_sink_forwarding():
    seen = []
    bus = LifecycleBus()
    feed = SessionLifecycleFeed(sink=lambda d: seen.append(d))
    feed.attach(bus)
    bus.emit("tool", "tool_after", {}, session_id="sx")
    assert len(seen) == 1 and seen[0]["event"] == "tool_after"


def test_daemon_wires_feed_into_event_ring(daemon):
    # AC1: the daemon runtime is the production wiring — _get_runtime
    # attaches the feed, bus events land in the daemon WS event ring.
    rt = daemon._get_runtime()
    feed = daemon._get_lifecycle_feed()
    assert feed is daemon._get_lifecycle_feed()  # singleton
    rt.lifecycle_bus.emit("tool", "tool_before", {"team": "red"}, session_id="s1")
    assert len(feed.events("s1")) == 1
    ring = [e for e in daemon._event_log if e["type"] == "lifecycle.event"]
    assert len(ring) == 1 and ring[0]["team"] == "red"


async def test_sessions_lifecycle_rpc(daemon):
    daemon._get_runtime()
    daemon._get_lifecycle_feed()
    daemon._runtime.lifecycle_bus.emit("tool", "tool_before", {}, session_id="s2")
    result = await _run(daemon, "sessions.lifecycle", {"session_id": "s2"})
    assert result["status"] == "ok"
    assert result["restored_from_jsonl"] == 0
    assert len(result["events"]) == 1
    assert result["summary"]["total_events"] == 1


def test_restart_reconstruction_from_jsonl(tmp_path):
    # AC2: restart a session — a fresh feed reconstructs its state from the
    # bus JSONL persistence and the sequence matches the pre-restart feed.
    jsonl_dir = str(tmp_path / "lifecycle")
    bus = LifecycleBus(jsonl_dir=jsonl_dir)
    live = SessionLifecycleFeed()
    live.attach(bus)
    _emit_sample(bus, session_id="s1", n=5)
    before = live.events("s1")
    assert len(before) == 6

    restarted = SessionLifecycleFeed(jsonl_dir=jsonl_dir)
    restored = restarted.restore_from_jsonl("s1")
    assert restored == 6
    after = restarted.events("s1")
    assert after == before
    assert restarted.summary("s1") == live.summary("s1")


async def test_rpc_reconstructs_after_restart(tmp_path, monkeypatch):
    # AC2 end-to-end through the RPC: empty in-memory feed (post-restart
    # daemon) still serves the session's event history from JSONL.
    jsonl_dir = str(tmp_path / "lifecycle")
    monkeypatch.setenv("FUSION_LIFECYCLE_JSONL_DIR", jsonl_dir)
    daemon = DaemonServer(
        socket_path=str(tmp_path / "t.sock"),
        store_path=str(tmp_path / "store.db"),
    )
    daemon._get_runtime()
    bus = daemon._runtime.lifecycle_bus
    _emit_sample(bus, session_id="s3", n=2)
    assert len(daemon._get_lifecycle_feed().events("s3")) == 3

    # simulate restart: fresh daemon + fresh bus, same JSONL dir
    daemon2 = DaemonServer(
        socket_path=str(tmp_path / "t2.sock"),
        store_path=str(tmp_path / "store2.db"),
    )
    daemon2._get_runtime()
    result = await _run(daemon2, "sessions.lifecycle", {"session_id": "s3"})
    assert result["status"] == "ok"
    assert result["restored_from_jsonl"] == 3
    assert [e["event"] for e in result["events"]] == [
        "tool_before", "tool_before", "subagent_start",
    ]
    assert result["summary"]["total_events"] == 3


def test_jsonl_round_trip_identical_sequence(tmp_path):
    # AC3: write, reload, replay produce an identical event sequence.
    jsonl_dir = str(tmp_path / "lifecycle")
    bus = LifecycleBus(jsonl_dir=jsonl_dir)
    _emit_sample(bus, session_id="s4", n=4)
    _emit_sample(bus, session_id="", n=1)  # global session file

    feed = SessionLifecycleFeed(jsonl_dir=jsonl_dir)
    reloaded = feed.replay_jsonl("s4")
    live = [e.to_dict() for e in bus.replay(session_id="s4")]
    assert reloaded == live
    assert len(feed.replay_jsonl("")) == 2  # global file

    # replaying the reloaded sequence through a fresh feed reproduces state
    fresh = SessionLifecycleFeed()
    for rec in reloaded:
        fresh._ingest(rec)
    assert fresh.events("s4") == live


def test_jsonl_off_and_missing_paths():
    feed = SessionLifecycleFeed(jsonl_dir=None)
    assert feed.replay_jsonl("nope") == []
    assert feed.restore_from_jsonl("nope") == 0
    assert feed.events("nope") == []
    assert feed.summary("nope")["total_events"] == 0
