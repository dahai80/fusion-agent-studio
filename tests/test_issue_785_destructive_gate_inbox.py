# Issue #785: destructive_gate cards in the typed kernel inbox.
#
# Kernel side: fusion-harness 0.30.0 admits "destructive_gate" to MSG_TYPES
# as inert mailbox data (harness #153 / PR #154). AS side: the dispatcher
# surfaces the cards and teams.inbox_respond resolves the pending
# ApprovalGate question over the shared HumanGate queue, parsing the
# gate_call_id line from the card body. The mailbox reply stays the same
# reply_to-anchored text decision as plan_approval, so answered-detection
# and the web-shell KINDS contract are unchanged.
from __future__ import annotations

import pytest
from fusion_harness.human_gate import HumanGate

from agent_runtime.daemon_server import DaemonServer
from tests.test_issue_341_subagent import _llm_response
from tests.test_issue_707_teams_dispatcher import _definition, _run, _runtime

GATE_BODY = "destructive call requested\ngate_call_id=dg_abc123\ntool: terminal"


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def human_gate(tmp_path):
    return HumanGate(str(tmp_path / "human_gate"))


async def _setup(daemon, human_gate):
    rt, _client = _runtime(responses=[_llm_response(content="ok")])
    rt._human_gate = human_gate
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    return rt


async def _send_card(daemon):
    return await _run(
        daemon,
        "teams.send",
        {"team": "pair", "from": "worker-a", "to": "worker-b", "type": "destructive_gate", "body": GATE_BODY},
    )


def _pending_question(gate, call_id):
    for q in gate.list_questions(status="pending"):
        if str((q.payload or {}).get("call_id", "")) == call_id:
            return q
    return None


class TestInboxTypedDestructiveGate:
    async def test_card_surfaced_with_kind(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        sent = await _send_card(daemon)
        out = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        assert out["status"] == "ok"
        assert len(out["messages"]) == 1
        card = out["messages"][0]
        assert card["kind"] == "destructive_gate"
        assert card["answered"] is False
        assert card["msg_id"] == sent["msg_id"]
        assert "gate_call_id=dg_abc123" in card["body"]

    async def test_kernel_admits_type(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        sent = await _send_card(daemon)
        assert sent["status"] == "ok"
        assert sent["msg_id"].startswith("msg_")


class TestInboxRespondDestructiveGate:
    async def test_approve_resolves_pending_question(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        gate = human_gate
        gate.ask(
            "sess-1",
            "approve destructive call dg_abc123 (terminal)?",
            payload={"call_id": "dg_abc123", "tool_name": "terminal"},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            requester="host",
            ttl_s=600,
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        assert out["status"] == "ok"
        assert out["gate_question_id"].startswith("hq_")
        q = _pending_question(gate, "dg_abc123")
        assert q is None  # no longer pending
        answered = [x for x in gate.list_questions(status="answered") if x.id == out["gate_question_id"]]
        assert len(answered) == 1
        assert answered[0].reply.get("approved") is True

    async def test_reject_resolves_with_approved_false(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        gate = human_gate
        gate.ask(
            "sess-1",
            "approve destructive call dg_abc123 (terminal)?",
            payload={"call_id": "dg_abc123", "tool_name": "terminal"},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            requester="host",
            ttl_s=600,
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "reject"},
        )
        assert out["status"] == "ok"
        answered = [x for x in gate.list_questions(status="answered") if x.id == out["gate_question_id"]]
        assert answered[0].reply.get("approved") is False

    async def test_reply_lands_in_sender_inbox(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        gate = human_gate
        gate.ask(
            "sess-1",
            "approve destructive call dg_abc123 (terminal)?",
            payload={"call_id": "dg_abc123", "tool_name": "terminal"},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            requester="host",
            ttl_s=600,
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        sender_inbox = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-a"})
        replies = [m for m in sender_inbox["messages"] if m.get("reply_to") == message_id]
        assert len(replies) == 1
        assert replies[0]["body"] == "approved"
        assert out["reply_type"] == "text"

    async def test_missing_gate_call_id_errors(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _run(
            daemon,
            "teams.send",
            {"team": "pair", "from": "worker-a", "to": "worker-b", "type": "destructive_gate", "body": "no call id here"},
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        assert out["status"] == "error"
        assert "missing gate_call_id" in out["message"]

    async def test_unknown_call_id_errors(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        assert out["status"] == "error"
        assert "no pending destructive gate" in out["message"]

    async def test_second_respond_rejected(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        gate = human_gate
        gate.ask(
            "sess-1",
            "approve destructive call dg_abc123 (terminal)?",
            payload={"call_id": "dg_abc123", "tool_name": "terminal"},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            requester="host",
            ttl_s=600,
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        first = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        assert first["status"] == "ok"
        again = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        assert again["status"] == "error"
        assert "already answered" in again["message"]

    async def test_answered_card_filter(self, daemon, _home, human_gate):
        await _setup(daemon, human_gate)
        await _send_card(daemon)
        gate = human_gate
        gate.ask(
            "sess-1",
            "approve destructive call dg_abc123 (terminal)?",
            payload={"call_id": "dg_abc123", "tool_name": "terminal"},
            reply_schema={"required": ["approved"], "properties": {"approved": "bool"}},
            requester="host",
            ttl_s=600,
        )
        sent = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        message_id = sent["messages"][0]["msg_id"]
        await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": message_id, "decision": "approve"},
        )
        pending = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        assert pending["messages"] == []
        with_answered = await _run(
            daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b", "answered": True}
        )
        assert len(with_answered["messages"]) == 1
        assert with_answered["messages"][0]["answered"] is True
