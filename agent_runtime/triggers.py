"""Webhook and Cron trigger system for agent execution."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Webhook:
    """A webhook trigger configuration."""
    id: str
    name: str
    secret: str = ""
    graph_id: str = ""
    enabled: bool = True
    created_at: float = 0.0


@dataclass
class CronJob:
    """A scheduled cron job configuration."""
    id: str
    name: str
    expression: str  # cron expression: "*/5 * * * *"
    graph_id: str = ""
    enabled: bool = True
    last_run: float = 0.0
    next_run: float = 0.0
    created_at: float = 0.0


class WebhookManager:
    """Manages webhook triggers for agent execution."""

    def __init__(self):
        self._webhooks: dict[str, Webhook] = {}
        self._handlers: dict[str, Callable] = {}

    def register(self, webhook: Webhook, handler: Callable | None = None) -> None:
        self._webhooks[webhook.id] = webhook
        if handler:
            self._handlers[webhook.id] = handler

    def unregister(self, webhook_id: str) -> None:
        self._webhooks.pop(webhook_id, None)
        self._handlers.pop(webhook_id, None)

    def get(self, webhook_id: str) -> Webhook | None:
        return self._webhooks.get(webhook_id)

    def list(self) -> list[dict]:
        return [
            {"id": w.id, "name": w.name, "graph_id": w.graph_id, "enabled": w.enabled}
            for w in self._webhooks.values()
        ]

    async def handle(self, webhook_id: str, payload: dict, headers: dict | None = None) -> dict:
        webhook = self._webhooks.get(webhook_id)
        if not webhook or not webhook.enabled:
            return {"error": "Webhook not found or disabled"}
        # Verify signature if secret is set
        if webhook.secret and headers:
            signature = self._compute_signature(payload, webhook.secret)
            received = headers.get("x-webhook-signature", "")
            if signature != received:
                return {"error": "Invalid signature"}
        handler = self._handlers.get(webhook_id)
        if handler:
            return await handler(webhook, payload)
        logger.info("Webhook %s triggered (no handler)", webhook_id)
        return {"status": "received", "webhook_id": webhook_id}

    @staticmethod
    def _compute_signature(payload: dict, secret: str) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    @property
    def count(self) -> int:
        return len(self._webhooks)


class CronManager:
    """Manages scheduled cron jobs for agent execution."""

    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._handlers: dict[str, Callable] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    def register(self, job: CronJob, handler: Callable | None = None) -> None:
        job.next_run = self._compute_next_run(job.expression)
        self._jobs[job.id] = job
        if handler:
            self._handlers[job.id] = handler

    def unregister(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._handlers.pop(job_id, None)

    def get(self, job_id: str) -> CronJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [
            {
                "id": j.id, "name": j.name, "expression": j.expression,
                "graph_id": j.graph_id, "enabled": j.enabled,
                "last_run": j.last_run, "next_run": j.next_run,
            }
            for j in self._jobs.values()
        ]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            now = time.time()
            for job in self._jobs.values():
                if not job.enabled:
                    continue
                if job.next_run > 0 and now >= job.next_run:
                    handler = self._handlers.get(job.id)
                    if handler:
                        try:
                            await handler(job)
                        except Exception as e:
                            logger.error("Cron job %s failed: %s", job.id, e)
                    job.last_run = now
                    job.next_run = self._compute_next_run(job.expression)
            await asyncio.sleep(15)  # Check every 15 seconds

    def _compute_next_run(self, expression: str) -> float:
        """Simple cron parser — supports minute/hour/day-of-month/month/day-of-week."""
        now = time.time()
        parts = expression.strip().split()
        if len(parts) != 5:
            return now + 3600  # Default: 1 hour
        try:
            from datetime import datetime, timedelta
            current = datetime.fromtimestamp(now)
            # Parse minute field
            minute_field = parts[0]
            if minute_field == "*":
                next_min = current + timedelta(minutes=1)
                return next_min.timestamp()
            elif minute_field.startswith("*/"):
                interval = int(minute_field[2:])
                next_min = current + timedelta(minutes=interval)
                return next_min.timestamp()
            else:
                minute = int(minute_field)
                if current.minute < minute:
                    next_dt = current.replace(minute=minute, second=0, microsecond=0)
                else:
                    next_dt = current.replace(minute=minute, second=0, microsecond=0) + timedelta(hours=1)
                return next_dt.timestamp()
        except Exception:
            return now + 3600

    @property
    def count(self) -> int:
        return len(self._jobs)