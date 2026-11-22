# Issue #716 (C2): teams.* dispatcher passthrough for the harness 0.14.0
# TeamMessage extension — msg_id/reply_to/mentions/attachments/audio_ref/
# transcript ride params into the kernel contract; send() returns the
# kernel outcome dict (delivered + kernel-generated msg_id); broadcast
# passes mentions through (kernel validates against the member list before
# anything is queued); inbox records surface every new field.
from __future__ import annotations

import json

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from tests.test_issue_341_subagent import _make_parent
from tools.registry import ToolRegistry


def _stub_registry() -> ToolRegistry:
    return ToolRegistry()


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition():
    return {
        "name": "chat-pair",
        "namespace_key": "team:chat-pair",
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
            {"name": "worker-b", "role": "does B", "budget_tokens": 40000},
        ],
    }


def _runtime(**kwargs):
    kwargs.setdefault("responses", [])
    kwargs.setdefault("tools", _stub_registry())
    rt, client = _make_parent(**kwargs)
    rt.kernel = {"orchestrator": build_kernel_orchestrator(rt)}
    return rt, client


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def _active(daemon):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    return rt


async def test_send_full_extension_roundtrip(daemon, _home):
    await _active(daemon)
    res = await _run(
        daemon,
        "teams.send",
        {
            "team": "chat-pair",
            "from": "worker-a",
            "to": "worker-b",
            "type": "text",
            "body": "see attached",
            "reply_to": "msg_deadbeef",
            "mentions": ["worker-b"],
            "attachments": ["run/worker-a.text"],
            "audio_ref": "run/worker-a.wav",
            "transcript": "voice note text",
        },
    )
    assert res["status"] == "ok"
    assert res["delivered"] is True
    assert res["msg_id"].startswith("msg_"), "kernel generates msg_id when absent"
    inbox = await _run(daemon, "teams.inbox", {"team": "chat-pair", "member": "worker-b"})
    m = inbox["messages"][0]
    assert m["reply_to"] == "msg_deadbeef"
    assert m["mentions"] == ["worker-b"]
    assert m["attachments"] == ["run/worker-a.text"]
    assert m["audio_ref"] == "run/worker-a.wav"
    assert m["transcript"] == "voice note text"
    assert m["msg_id"] == res["msg_id"]
    on_disk = json.loads((_home / "teams" / "chat-pair" / "inboxes" / "worker-b.json").read_text())
    assert on_disk[0]["msg_id"] == res["msg_id"]


async def test_send_bad_reply_to_rejected(daemon):
    await _active(daemon)
    res = await _run(
        daemon,
        "teams.send",
        {
            "team": "chat-pair",
            "from": "worker-a",
            "to": "worker-b",
            "reply_to": "not-a-msg-id",
            "body": "x",
        },
    )
    assert res["status"] == "error"


async def test_send_unknown_mention_rejected(daemon):
    await _active(daemon)
    res = await _run(
        daemon,
        "teams.send",
        {
            "team": "chat-pair",
            "from": "worker-a",
            "to": "worker-b",
            "mentions": ["ghost"],
            "body": "x",
        },
    )
    assert res["status"] == "error"


async def test_broadcast_mentions_passthrough(daemon):
    await _active(daemon)
    res = await _run(
        daemon,
        "teams.broadcast",
        {"team": "chat-pair", "from": "worker-a", "body": "all hands", "mentions": ["worker-b"]},
    )
    assert res["delivered"] == 1
    inbox = await _run(daemon, "teams.inbox", {"team": "chat-pair", "member": "worker-b"})
    assert inbox["messages"][0]["mentions"] == ["worker-b"]


async def test_broadcast_unknown_mention_rejects_all(daemon):
    await _active(daemon)
    res = await _run(
        daemon,
        "teams.broadcast",
        {"team": "chat-pair", "from": "worker-a", "body": "x", "mentions": ["ghost"]},
    )
    assert res["status"] == "error"
    inbox = await _run(daemon, "teams.inbox", {"team": "chat-pair", "member": "worker-b"})
    assert inbox["messages"] == [], "failed broadcast must queue nothing"


async def test_msg_id_persisted_and_inbox_surface(daemon, _home):
    await _active(daemon)
    r1 = await _run(
        daemon,
        "teams.send",
        {"team": "chat-pair", "from": "worker-a", "to": "worker-b", "body": "one"},
    )
    r2 = await _run(
        daemon,
        "teams.send",
        {
            "team": "chat-pair",
            "from": "worker-a",
            "to": "worker-b",
            "body": "two",
            "reply_to": r1["msg_id"],
        },
    )
    assert r2["status"] == "ok" and r2["msg_id"] != r1["msg_id"]
    inbox = await _run(daemon, "teams.inbox", {"team": "chat-pair", "member": "worker-b"})
    assert [m["msg_id"] for m in inbox["messages"]] == [r1["msg_id"], r2["msg_id"]]
    assert inbox["messages"][1]["reply_to"] == r1["msg_id"]
