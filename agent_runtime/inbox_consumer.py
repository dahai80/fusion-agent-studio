"""#457: autonomous inbox consumer — daemon-resident mailbox dispatcher.

Rostered agents with `autonomous_inbox: true` get their mailbox consumed by
a background task: each unread message becomes an agent run (message body as
input, sender context injected), is acked at-least-once on dispatch, and the
run's final output is posted back to the sender's mailbox as a reply.

Crash safety: acks happen only after the run task is durably submitted
(TaskStore owns the run_id), so a crash mid-conversation re-delivers the
message on restart (at-least-once). Reply tracking is in-memory; a crash
before the reply simply means the sender re-asks — the durable contract is
the ack, not the reply.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "canceled"}


class InboxConsumer:
    def __init__(
        self,
        roster,
        mailbox,
        submit_run,
        get_run,
        poll_interval: float = 5.0,
    ):
        # submit_run(agent: dict, message: dict) -> str  (task/run id, async)
        # get_run(run_id: str) -> dict | None            (status, last_result, last_error)
        self.roster = roster
        self.mailbox = mailbox
        self.submit_run = submit_run
        self.get_run = get_run
        self.poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._running = False
        self._pending_replies: dict[str, dict] = {}
        self.metrics = {
            "processed": 0,
            "replies_sent": 0,
            "dispatch_errors": 0,
            "inbox_depth": 0,
            "dispatch_latency_ms": 0.0,
            "per_agent": {},
        }

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            "inbox consumer started poll_interval=%.1fs", self.poll_interval
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("inbox consumer stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("inbox consumer tick failed: %s", exc)
            await asyncio.sleep(self.poll_interval)

    async def tick(self) -> None:
        await self._dispatch_new_messages()
        await self._post_completed_replies()

    async def _dispatch_new_messages(self) -> None:
        depth = 0
        for agent in self.roster.list():
            if not agent.get("autonomous_inbox"):
                continue  # human-driven agent: mail waits untouched (#457 AC2)
            if not agent.get("graph_id"):
                # Opted in but unbindable: leave mail unacked (redelivered
                # once a graph binding exists) rather than dropping it.
                logger.warning(
                    "inbox skip agent=%s autonomous but no graph_id binding",
                    agent.get("agent_id"),
                )
                continue
            messages = self.mailbox.inbox(agent["agent_id"])
            depth += len(messages)
            for message in messages:
                await self._dispatch_one(agent, message)
        self.metrics["inbox_depth"] = depth

    async def _dispatch_one(self, agent: dict, message: dict) -> None:
        agent_id = agent["agent_id"]
        started = time.time()
        try:
            run_id = await self.submit_run(agent, message)
        except Exception as exc:
            self.metrics["dispatch_errors"] += 1
            # No ack: at-least-once redelivery on the next tick.
            logger.error(
                "inbox dispatch failed agent=%s message=%s err=%s",
                agent_id, message.get("message_id"), exc,
            )
            return
        latency_ms = (time.time() - started) * 1000
        self.metrics["dispatch_latency_ms"] = latency_ms
        per_agent = self.metrics["per_agent"].setdefault(
            agent_id, {"processed": 0, "replies": 0, "errors": 0}
        )
        per_agent["processed"] += 1
        self.metrics["processed"] += 1
        # Ack AFTER durable submission: crash before this line redelivers.
        self.mailbox.ack(agent_id, message["message_id"])
        if message.get("sender"):
            self._pending_replies[run_id] = {
                "agent_id": agent_id,
                "sender": message["sender"],
                "message_id": message["message_id"],
                "team": message.get("team", ""),
            }
        logger.info(
            "inbox dispatched agent=%s message=%s run=%s latency_ms=%.1f",
            agent_id, message.get("message_id"), run_id, latency_ms,
        )

    async def _post_completed_replies(self) -> None:
        for run_id, info in list(self._pending_replies.items()):
            try:
                run = await self.get_run(run_id)
            except Exception as exc:
                logger.warning("inbox reply poll failed run=%s err=%s", run_id, exc)
                continue
            if run is None:
                continue
            status = run.get("status", "")
            if status not in TERMINAL_STATUSES:
                continue
            body = self._reply_body(run, info)
            self.mailbox.send(
                info["agent_id"],
                info["sender"],
                body,
                kind="reply",
                team=info.get("team", ""),
            )
            per_agent = self.metrics["per_agent"].setdefault(
                info["agent_id"], {"processed": 0, "replies": 0, "errors": 0}
            )
            per_agent["replies"] += 1
            self.metrics["replies_sent"] += 1
            del self._pending_replies[run_id]
            logger.info(
                "inbox reply sent agent=%s to=%s run=%s status=%s",
                info["agent_id"], info["sender"], run_id, status,
            )

    @staticmethod
    def _reply_body(run: dict, info: dict) -> str:
        status = run.get("status", "")
        if status == "completed":
            last = run.get("last_result") or {}
            if isinstance(last, dict):
                text = str(last.get("output") or last.get("result") or last.get("summary") or "")
                if text.strip():
                    return text[:20000]
            return f"[autonomous run completed — no textual output] run_id={info.get('message_id', '')}"
        return f"[autonomous run {status}] {run.get('last_error', '')}".strip()[:20000]

    def stats(self) -> dict:
        return {
            "running": self._running,
            "pending_replies": len(self._pending_replies),
            **self.metrics,
        }
