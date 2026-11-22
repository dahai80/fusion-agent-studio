# Issue #781: typed kernel inbox — plan_approval / shutdown_request cards
# with a respond RPC mapping decisions to kernel reply types.
#
# - teams.inbox_typed surfaces typed messages as actionable cards with
#   kind + answered (answered = reply with reply_to sits in the original
#   sender's inbox — the kernel mailbox is per-recipient).
# - teams.inbox_respond delivers the decision: shutdown_request answers
#   with the typed shutdown_response; plan_approval (no typed response in
#   kernel MSG_TYPES) answers with a text reply anchored by reply_to.
#   Body is the bare decision word. Second respond on the same message is
#   rejected (idempotence guard against the sender-side inbox).
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from tests.test_issue_341_subagent import _llm_response
from tests.test_issue_707_teams_dispatcher import _definition, _run, _runtime


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


async def _setup(daemon):
    rt, _client = _runtime(responses=[_llm_response(content="ok")])
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    return rt


async def _send(daemon, **overrides):
    params = {"team": "pair", "from": "worker-a", "to": "worker-b", "body": "the plan"}
    params.update(overrides)
    return await _run(daemon, "teams.send", params)


class TestInboxTyped:
    async def test_plan_approval_card_pending(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval", body="step plan v1")
        assert sent["status"] == "ok"
        out = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        assert out["status"] == "ok"
        assert len(out["messages"]) == 1
        card = out["messages"][0]
        assert card["kind"] == "plan_approval"
        assert card["answered"] is False
        assert card["msg_id"] == sent["msg_id"]
        assert card["body"] == "step plan v1"

    async def test_text_messages_not_surfaced(self, daemon, _home):
        await _setup(daemon)
        await _send(daemon, type="text", body="plain chat")
        out = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        assert out["messages"] == []

    async def test_unknown_member_typed_error(self, daemon, _home):
        await _setup(daemon)
        out = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "ghost"})
        assert out["status"] == "error"
        assert out.get("reason_code") == "member_unknown"


class TestInboxRespond:
    async def test_approve_plan_approval_text_reply(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval")
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "approve",
            },
        )
        assert out["status"] == "ok"
        assert out["decision"] == "approved"
        assert out["reply_type"] == "text"
        assert out["reply_msg_id"].startswith("msg_")
        # Reply lands in the original sender's inbox, anchored by reply_to.
        sender_inbox = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-a"})
        replies = [m for m in sender_inbox["messages"] if m.get("reply_to") == sent["msg_id"]]
        assert len(replies) == 1
        assert replies[0]["body"] == "approved"

    async def test_shutdown_request_maps_to_shutdown_response(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(
            daemon, type="shutdown_request", **{"from": "worker-b", "to": "worker-a"}
        )
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-a",
                "message_id": sent["msg_id"],
                "decision": "reject",
            },
        )
        assert out["status"] == "ok"
        assert out["reply_type"] == "shutdown_response"
        assert out["decision"] == "rejected"

    async def test_decline_alias(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval")
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "decline",
            },
        )
        assert out["status"] == "ok"
        assert out["decision"] == "rejected"

    async def test_second_respond_rejected(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval")
        first = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "approve",
            },
        )
        assert first["status"] == "ok"
        again = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "approve",
            },
        )
        assert again["status"] == "error"
        assert "already answered" in again["message"]

    async def test_answered_card_flag_and_filter(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval")
        await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "approve",
            },
        )
        pending = await _run(daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b"})
        assert pending["messages"] == []
        with_answered = await _run(
            daemon, "teams.inbox_typed", {"team": "pair", "member": "worker-b", "answered": True}
        )
        assert len(with_answered["messages"]) == 1
        assert with_answered["messages"][0]["answered"] is True

    async def test_invalid_decision_typed_error(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="plan_approval")
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "maybe",
            },
        )
        assert out["status"] == "error"
        assert "valid: approve/reject" in out["message"]

    async def test_unknown_message_rejected(self, daemon, _home):
        await _setup(daemon)
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {"team": "pair", "member": "worker-b", "message_id": "msg_nope", "decision": "approve"},
        )
        assert out["status"] == "error"
        assert "not in member inbox" in out["message"]

    async def test_text_message_not_actionable(self, daemon, _home):
        await _setup(daemon)
        sent = await _send(daemon, type="text")
        out = await _run(
            daemon,
            "teams.inbox_respond",
            {
                "team": "pair",
                "member": "worker-b",
                "message_id": sent["msg_id"],
                "decision": "approve",
            },
        )
        assert out["status"] == "error"
        assert "not an actionable typed message" in out["message"]
