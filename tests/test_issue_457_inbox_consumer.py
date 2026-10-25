"""Tests for issue #457 — autonomous inbox consumer (AS side).

Importers: pytest runner
API: InboxConsumer (dispatch/ack/reply/metrics), DurableRoster
     autonomous_inbox + graph_id columns
Data schemas: team.db roster/mailbox tables (#457 migration)
"""

from __future__ import annotations

import pytest

from agent_runtime.durable_roster import DurableRoster, Mailbox
from agent_runtime.inbox_consumer import InboxConsumer


@pytest.fixture()
def stores(tmp_path):
    db = str(tmp_path / "team.db")
    return DurableRoster(db), Mailbox(db)


def _autonomous_agent(roster: DurableRoster, agent_id="auto-1", graph_id="graph-x"):
    return roster.register(
        agent_id,
        name="Autonomous",
        team="t1",
        graph_id=graph_id,
        autonomous_inbox=True,
    )


def _make_consumer(roster, mailbox, runs=None):
    runs = runs if runs is not None else {}
    submitted = []

    async def submit_run(agent, message):
        run_id = f"run-{len(submitted) + 1}"
        submitted.append({"agent": agent, "message": message, "run_id": run_id})
        return run_id

    async def get_run(run_id):
        return runs.get(run_id)

    consumer = InboxConsumer(
        roster=roster, mailbox=mailbox, submit_run=submit_run, get_run=get_run
    )
    return consumer, submitted


async def test_autonomous_agent_message_dispatched_and_acked(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("cowork-agent", "auto-1", "please summarize the report")
    consumer, submitted = _make_consumer(roster, mailbox)

    await consumer.tick()

    assert len(submitted) == 1
    assert submitted[0]["message"]["body"] == "please summarize the report"
    # At-least-once: acked after durable submission.
    assert mailbox.inbox("auto-1") == []
    assert consumer.metrics["processed"] == 1
    assert consumer.metrics["per_agent"]["auto-1"]["processed"] == 1


async def test_non_autonomous_agent_mail_untouched(stores):
    # #457 AC2: human-driven agents are unaffected.
    roster, mailbox = stores
    roster.register("human-1", name="Human", team="t1")
    mailbox.send("cowork-agent", "human-1", "pick this up when you look")
    consumer, _ = _make_consumer(roster, mailbox)

    await consumer.tick()

    assert len(mailbox.inbox("human-1")) == 1


async def test_agent_without_graph_binding_not_dispatched(stores):
    roster, mailbox = stores
    roster.register("loose-1", name="Loose", team="t1", autonomous_inbox=True)
    mailbox.send("x", "loose-1", "hello")
    consumer, submitted = _make_consumer(roster, mailbox)

    await consumer.tick()

    assert submitted == []
    # No ack either: the message is not lost, it waits for a binding.
    assert len(mailbox.inbox("loose-1")) == 1
    assert consumer.metrics["dispatch_errors"] == 0


async def test_reply_posted_back_to_sender_on_completion(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("cowork-agent", "auto-1", "summarize")
    runs = {"run-1": {"status": "completed", "last_result": {"output": "the summary"}, "last_error": ""}}
    consumer, _ = _make_consumer(roster, mailbox, runs)

    await consumer.tick()  # dispatch
    await consumer.tick()  # reply poll

    replies = mailbox.inbox("cowork-agent")
    assert len(replies) == 1
    assert replies[0]["kind"] == "reply"
    assert replies[0]["body"] == "the summary"
    assert replies[0]["sender"] == "auto-1"
    assert consumer.metrics["replies_sent"] == 1


async def test_reply_waits_until_terminal_status(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("cowork-agent", "auto-1", "summarize")
    runs = {"run-1": {"status": "running", "last_result": {}, "last_error": ""}}
    consumer, _ = _make_consumer(roster, mailbox, runs)

    await consumer.tick()
    await consumer.tick()
    assert mailbox.inbox("cowork-agent") == []

    runs["run-1"] = {"status": "completed", "last_result": {"output": "done"}, "last_error": ""}
    await consumer.tick()
    assert len(mailbox.inbox("cowork-agent")) == 1


async def test_failed_run_replies_with_error(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("cowork-agent", "auto-1", "summarize")
    runs = {"run-1": {"status": "failed", "last_result": {}, "last_error": "boom"}}
    consumer, _ = _make_consumer(roster, mailbox, runs)

    await consumer.tick()
    await consumer.tick()

    replies = mailbox.inbox("cowork-agent")
    assert len(replies) == 1
    assert "failed" in replies[0]["body"]
    assert "boom" in replies[0]["body"]


async def test_dispatch_error_no_ack_redelivered(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("cowork-agent", "auto-1", "hi")

    async def boom(agent, message):
        raise RuntimeError("executor down")

    consumer = InboxConsumer(
        roster=roster, mailbox=mailbox, submit_run=boom, get_run=lambda rid: None
    )
    await consumer.tick()
    assert consumer.metrics["dispatch_errors"] == 1
    assert len(mailbox.inbox("auto-1")) == 1  # still unacked → redelivered


async def test_start_stop_lifecycle(stores):
    roster, mailbox = stores
    consumer, _ = _make_consumer(roster, mailbox)
    consumer.start()
    assert consumer.running
    await consumer.stop()
    assert not consumer.running


async def test_roster_migration_adds_columns(tmp_path):
    # Pre-#457 DB (old schema) migrates in place.
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE roster (agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "team TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'idle', updated_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO roster VALUES ('old-1', 'Old', 't', '', 'idle', 0)"
    )
    conn.commit()
    conn.close()

    roster = DurableRoster(db)
    row = roster.get("old-1")
    assert row["autonomous_inbox"] == 0
    assert row["graph_id"] == ""
    # re-register with the new fields upgrades the row
    updated = roster.register("old-1", autonomous_inbox=True, graph_id="g9")
    assert updated["autonomous_inbox"] == 1
    assert updated["graph_id"] == "g9"


async def test_metrics_exposed_via_stats(stores):
    roster, mailbox = stores
    _autonomous_agent(roster)
    mailbox.send("a", "auto-1", "m1")
    runs = {"run-1": {"status": "completed", "last_result": {"output": "ok"}, "last_error": ""}}
    consumer, _ = _make_consumer(roster, mailbox, runs)
    await consumer.tick()
    await consumer.tick()

    stats = consumer.stats()
    assert stats["processed"] == 1
    assert stats["replies_sent"] == 1
    assert stats["inbox_depth"] == 0
    assert stats["running"] is False
