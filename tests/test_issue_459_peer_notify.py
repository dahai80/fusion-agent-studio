"""Tests for issue #459 — peer-daemon notify on mailbox send.

Importers: pytest runner
API: Mailbox.send/broadcast fire best-effort POST to configured peers
     (fusion-cowork POST /api/team/notify contract); env toggle + URL list.
Data schemas: none (wire protocol {"team": "<team>"})
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_runtime import peer_notify
from agent_runtime.durable_roster import DurableRoster, Mailbox


class _Sink(BaseHTTPRequestHandler):
    requests: list[tuple[bytes, dict]] = []

    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        body = self.rfile.read(length)
        _Sink.requests.append((body, dict(self.headers)))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"notified": true, "running": true}')

    def log_message(self, *args):
        pass


@pytest.fixture()
def sink():
    _Sink.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api/team/notify"
    server.shutdown()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("FUSION_TEAM_PEER_NOTIFY", raising=False)
    monkeypatch.delenv("FUSION_TEAM_PEER_NOTIFY_URLS", raising=False)


def _wait_for(count: int, timeout: float = 3.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_Sink.requests) >= count:
            return
        time.sleep(0.02)


def test_notify_peers_posts_team_payload(env, sink):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = sink
    assert peer_notify.notify_peers("ops") == 1
    _wait_for(1)
    assert len(_Sink.requests) == 1
    body, headers = _Sink.requests[0]
    assert json.loads(body) == {"team": "ops"}
    assert "application/json" in headers.get("Content-Type", "")


def test_notify_peers_disabled_by_env(env, sink):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY"] = "0"
    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = sink
    assert peer_notify.notify_peers("ops") == 0
    _wait_for(0)
    assert _Sink.requests == []


def test_notify_peers_sends_bearer_key(env, sink):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = sink
    os.environ["FUSION_TEAM_PEER_NOTIFY_KEY"] = "secret-123"
    assert peer_notify.notify_peers("t") == 1
    _wait_for(1)
    _, headers = _Sink.requests[0]
    assert headers.get("Authorization") == "Bearer secret-123"


def test_notify_peers_no_urls_configured(env):
    assert peer_notify.notify_peers("ops") == 0


def test_notify_peers_multiple_urls(env, sink):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = f"{sink},{sink}"
    assert peer_notify.notify_peers("t") == 2
    _wait_for(2)
    assert len(_Sink.requests) == 2


def test_notify_peers_unreachable_peer_never_raises(env):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = "http://127.0.0.1:1/api/team/notify"
    assert peer_notify.notify_peers("t") == 1


def test_mailbox_send_fires_notify(env, sink, tmp_path):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = sink
    DurableRoster(str(tmp_path / "team.db")).register(
        agent_id="a1", name="A1", team="ops"
    )
    mb = Mailbox(str(tmp_path / "team.db"))
    rec = mb.send("a1", "a2", "hello", team="ops")
    assert rec["message_id"]
    _wait_for(1)
    assert len(_Sink.requests) == 1
    assert json.loads(_Sink.requests[0][0]) == {"team": "ops"}


def test_mailbox_broadcast_notifies_once(env, sink, tmp_path):
    import os

    os.environ["FUSION_TEAM_PEER_NOTIFY_URLS"] = sink
    roster = DurableRoster(str(tmp_path / "team.db"))
    roster.register(agent_id="a1", name="A1", team="ops")
    roster.register(agent_id="a2", name="A2", team="ops")
    roster.register(agent_id="a3", name="A3", team="ops")
    mb = Mailbox(str(tmp_path / "team.db"))
    sent = mb.broadcast("a1", "ops", "hi all")
    assert len(sent) == 2
    _wait_for(1)
    assert len(_Sink.requests) == 1


def test_mailbox_send_without_config_still_works(env, tmp_path):
    DurableRoster(str(tmp_path / "team.db")).register(
        agent_id="a1", name="A1", team="ops"
    )
    mb = Mailbox(str(tmp_path / "team.db"))
    rec = mb.send("a1", "a2", "hello", team="ops")
    assert rec["message_id"]
    assert mb.inbox("a2")
