"""M1-6 contract tests: team.events WS/SSE channel + last_event_id incremental.

Issue #305: per-client team subscription + event ring buffer + Last-Event-ID
catch-up. Events carry monotonic event_id + team filter.

Runner: pytest tests/test_m1_6_team_events.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer


async def _rpc_call(socket_path, method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=10.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def daemon(socket_path, tmp_path):
    d = DaemonServer(
        socket_path=socket_path,
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "test_store.db"),
    )
    await d.start()
    yield d
    await d.stop()


class _FakeWriter:
    # Captures WS frames written via _ws_write_frame for assertion.
    def __init__(self):
        self.frames = []
        self._closed = False

    def get_extra_info(self, name, default=None):
        return default

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None

    def close(self):
        self._closed = True

    async def wait_closed(self):
        return None


class TestEventLog:
    def test_append_event_assigns_monotonic_id(self, daemon):
        e1 = daemon._append_event("task.created", {"task_id": "t1"}, team="ops")
        e2 = daemon._append_event("task.completed", {"task_id": "t2"}, team="ops")
        assert e1["event_id"] == 1
        assert e2["event_id"] == 2
        assert e1["type"] == "task.created"
        assert e1["team"] == "ops"
        assert e1["ts"] > 0

    def test_event_log_stores_events(self, daemon):
        daemon._append_event("task.created", {"task_id": "t1"}, team="ops")
        daemon._append_event("task.completed", {"task_id": "t1"}, team="ops")
        assert len(daemon._event_log) == 2

    def test_ring_buffer_eviction(self, daemon):
        # maxlen=1000. Insert 1002 → oldest 2 evicted.
        for i in range(1002):
            daemon._append_event("tick", {"i": i}, team="ops")
        assert len(daemon._event_log) == 1000
        first = daemon._event_log[0]
        assert first["event_id"] == 3  # first 2 (id 1,2) evicted

    def test_events_since_filters_team(self, daemon):
        daemon._append_event("a", {}, team="ops")
        daemon._append_event("b", {}, team="dev")
        daemon._append_event("c", {}, team="ops")
        ops = daemon._events_since(0, "ops")
        assert len(ops) == 2
        assert all(e["team"] == "ops" for e in ops)

    def test_events_since_last_id(self, daemon):
        daemon._append_event("a", {}, team="ops")
        e2 = daemon._append_event("b", {}, team="ops")
        daemon._append_event("c", {}, team="ops")
        since = daemon._events_since(e2["event_id"], "ops")
        assert len(since) == 1
        assert since[0]["type"] == "c"


class TestBroadcastFiltering:
    @pytest.mark.asyncio
    async def test_only_subscribed_clients_receive(self, daemon):
        w_ops = _FakeWriter()
        w_dev = _FakeWriter()
        w_unsub = _FakeWriter()
        daemon._ws_clients = [w_ops, w_dev, w_unsub]
        daemon._ws_subscriptions = {id(w_ops): "ops", id(w_dev): "dev"}

        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")

        # w_ops gets event (subscribed ops), w_dev + w_unsub don't
        assert len(w_ops.frames) == 1
        assert len(w_dev.frames) == 0
        assert len(w_unsub.frames) == 0
        # _ws_write_frame writes binary WS frames; payload JSON is embedded
        # as UTF-8 text within the frame. Search raw bytes for substrings.
        raw = w_ops.frames[0]
        if not isinstance(raw, (bytes, bytearray)):
            raw = raw.encode("utf-8")
        assert b"task.created" in raw
        assert b"event_id" in raw

    @pytest.mark.asyncio
    async def test_cross_team_isolation(self, daemon):
        w_ops = _FakeWriter()
        w_dev = _FakeWriter()
        daemon._ws_clients = [w_ops, w_dev]
        daemon._ws_subscriptions = {id(w_ops): "ops", id(w_dev): "dev"}

        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")
        await daemon._broadcast_event("task.created", {"task_id": "t2"}, team="dev")

        assert len(w_ops.frames) == 1
        assert len(w_dev.frames) == 1

    @pytest.mark.asyncio
    async def test_broadcast_logs_event_regardless_of_clients(self, daemon):
        # No clients → event still logged (for late-subscriber catch-up).
        daemon._ws_clients = []
        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")
        assert len(daemon._event_log) == 1
        assert daemon._event_log[0]["event_id"] == 1

    @pytest.mark.asyncio
    async def test_dead_client_pruned_on_broadcast(self, daemon):
        w_dead = _FakeWriter()
        w_alive = _FakeWriter()

        # Make w_dead.drain raise to simulate broken connection.
        async def _boom():
            raise ConnectionError("dead")

        w_dead.drain = _boom
        daemon._ws_clients = [w_dead, w_alive]
        daemon._ws_subscriptions = {id(w_dead): "ops", id(w_alive): "ops"}

        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")

        assert w_dead not in daemon._ws_clients
        assert id(w_dead) not in daemon._ws_subscriptions
        assert w_alive in daemon._ws_clients


class TestSubscribeReplay:
    @pytest.mark.asyncio
    async def test_subscribe_replays_missed_events(self, daemon):
        # Pre-log events for team "ops".
        daemon._append_event("task.created", {"task_id": "t1"}, team="ops")
        e2 = daemon._append_event("task.completed", {"task_id": "t1"}, team="ops")

        writer = _FakeWriter()
        daemon._ws_clients.append(writer)

        msg = {"action": "subscribe", "team": "ops", "last_event_id": e2["event_id"] - 1}
        await daemon._handle_ws_message(writer, msg)

        # First frame = subscribed ack, then 1 replayed event (e2).
        assert len(writer.frames) >= 2
        # Subscription stored.
        assert daemon._ws_subscriptions[id(writer)] == "ops"

    @pytest.mark.asyncio
    async def test_subscribe_no_last_event_id_replays_all(self, daemon):
        daemon._append_event("a", {}, team="ops")
        daemon._append_event("b", {}, team="ops")
        daemon._append_event("c", {}, team="dev")  # other team, not replayed

        writer = _FakeWriter()
        daemon._ws_clients.append(writer)

        msg = {"action": "subscribe", "team": "ops"}
        await daemon._handle_ws_message(writer, msg)

        # 1 ack + 2 ops events (dev event excluded).
        assert len(writer.frames) == 3

    @pytest.mark.asyncio
    async def test_subscribe_with_no_events(self, daemon):
        writer = _FakeWriter()
        daemon._ws_clients.append(writer)

        msg = {"action": "subscribe", "team": "ops"}
        await daemon._handle_ws_message(writer, msg)

        # 1 ack only, no replay.
        assert len(writer.frames) == 1
        assert daemon._ws_subscriptions[id(writer)] == "ops"


class TestEndToEndEvents:
    @pytest.mark.asyncio
    async def test_broadcast_then_subscribe_replay(self, daemon):
        # Full flow: broadcast events (no clients) → late subscriber catches up.
        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")
        await daemon._broadcast_event("task.completed", {"task_id": "t1"}, team="ops")
        await daemon._broadcast_event("task.created", {"task_id": "t2"}, team="dev")

        assert len(daemon._event_log) == 3

        # Late ops subscriber joins → gets 2 ops events (dev excluded).
        writer = _FakeWriter()
        daemon._ws_clients.append(writer)
        msg = {"action": "subscribe", "team": "ops"}
        await daemon._handle_ws_message(writer, msg)

        # 1 ack + 2 ops events.
        assert len(writer.frames) == 3
        assert daemon._ws_subscriptions[id(writer)] == "ops"

    @pytest.mark.asyncio
    async def test_live_broadcast_to_subscribed_client(self, daemon):
        # Subscribe first, then broadcast → client receives live event.
        writer = _FakeWriter()
        daemon._ws_clients.append(writer)
        msg = {"action": "subscribe", "team": "ops"}
        await daemon._handle_ws_message(writer, msg)
        assert len(writer.frames) == 1  # ack only, no prior events

        await daemon._broadcast_event("task.created", {"task_id": "t1"}, team="ops")
        assert len(writer.frames) == 2  # ack + 1 live event

        raw = writer.frames[1]
        if not isinstance(raw, (bytes, bytearray)):
            raw = raw.encode("utf-8")
        assert b"task.created" in raw
        assert b"event_id" in raw
