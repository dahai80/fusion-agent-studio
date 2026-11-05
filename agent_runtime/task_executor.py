#604: daemon-resident task executor — the single execution exit for the
# task board. task.submit is the only entry; submitted = must be executed.
#
# Worker loop (same pattern as inbox_consumer.start()): dequeues pending
# tasks via TaskStore.dequeue (atomic cross-process claim + attempt_token),
# then dispatches:
#   - agent_id  -> subagent.run_task (spec resolved from the agent package;
#                  per-run spec overrides are just parameters, no second path)
#   - graph_id  -> graph.execute_async (variables parsed from task.input)
# Wires up the dead task-board machinery: max_retries (requeue with fresh
# attempt), depends_on (dequeue deps_met revert), priority (dequeue SQL).
# Crash recovery: orphan running tasks reset to pending at startup.
#
# Env:
#   FUSION_TASK_EXECUTOR_CONCURRENCY  max parallel task executions (default 4)
#   FUSION_TASK_EXECUTOR_DISABLED     1/true disables the worker (CLI-only mode)

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

_OWNER = "task-executor"
_TERMINAL_STATUSES = {"completed", "failed", "canceled"}
_EMPTY_POLL_SLEEP = 0.5
_MAX_POLL_SLEEP = 5.0
_TERMINAL_POLL_INTERVAL = 1.0
_TERMINAL_WAIT_CAP = 4 * 3600


def executor_enabled() -> bool:
    return os.environ.get("FUSION_TASK_EXECUTOR_DISABLED", "").lower() not in (
        "1",
        "true",
        "yes",
    )


def executor_concurrency() -> int:
    raw = os.environ.get("FUSION_TASK_EXECUTOR_CONCURRENCY", "")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 4
    return max(1, min(32, n))


class DisabledTaskExecutor:
    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @property
    def running(self) -> bool:
        return False


class TaskExecutor:
    def __init__(self, daemon):
        self._daemon = daemon
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._sem = asyncio.Semaphore(executor_concurrency())

    # ── lifecycle ──

    def start(self) -> None:
        if self._task is not None:
            return
        try:
            reset = self._daemon._get_task_store().reset_orphans(_OWNER)
            if reset:
                logger.info(
                    "TaskExecutor recovered %d orphan task(s) at startup", reset
                )
        except Exception as e:
            logger.warning("TaskExecutor orphan recovery failed: %s", e)
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            "TaskExecutor started (concurrency=%d)", executor_concurrency()
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("TaskExecutor stopped")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── pickup loop ──

    async def _loop(self) -> None:
        sleep = _EMPTY_POLL_SLEEP
        while not self._stopping:
            try:
                store = self._daemon._get_task_store()
                task = None
                # Read pre-check: idle polling must not take the SQLite write
                # lock every tick — UPDATE..RETURNING would starve concurrent
                # writers (cron manager, API submissions). Only claim when a
                # pending task actually exists.
                if store.count_by_status("pending") > 0:
                    task = store.dequeue(team="*", owner_agent=_OWNER)
            except Exception as e:
                logger.warning("TaskExecutor dequeue failed: %s", e)
                task = None
            if task is None:
                await asyncio.sleep(sleep)
                sleep = min(sleep * 2, _MAX_POLL_SLEEP)
                continue
            sleep = _EMPTY_POLL_SLEEP
            await self._sem.acquire()
            asyncio.ensure_future(self._run_one(task.task_id))

    # ── single task execution ──

    async def _run_one(self, task_id: str) -> None:
        store = self._daemon._get_task_store()
        try:
            task = store.get(task_id)
            if task is None or task.status != "running":
                return
            await self._broadcast(
                "task.updated",
                {
                    "task_id": task_id,
                    "status": "running",
                    "agent_id": task.agent_id,
                    "graph_id": task.graph_id,
                    "team": task.team,
                },
                team=task.team,
            )
            if task.agent_id and not task.graph_id:
                await self._execute_agent(task)
            elif task.graph_id:
                await self._execute_graph(task)
            else:
                store.update_status(
                    task_id,
                    "failed",
                    last_error="task has neither agent_id nor graph_id",
                )
            await self._await_terminal(task_id)
            await self._finalize(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("TaskExecutor task %s crashed", task_id)
            try:
                store.update_status(task_id, "failed", last_error=str(e))
            except Exception:
                pass
        finally:
            self._sem.release()

    async def _execute_agent(self, task) -> None:
        # Single convergence point with the /v1/runs spec-override path:
        # subagent.run_task machinery, spec carries the agent package config
        # (soul prompt / model / tools) plus any per-run overrides.
        spec = self._spec_for_agent(task.agent_id)
        if spec.get("system_prompt") == "__missing_agent__":
            self._daemon._get_task_store().update_status(
                task.task_id,
                "failed",
                last_error=f"agent not found: {task.agent_id}",
            )
            return
        res = await self._rpc(
            "subagent.run_task",
            {
                "task_id": task.task_id,
                "spec": spec,
                "goal": task.input or task.description or task.title,
                "agent_id": task.agent_id,
            },
        )
        if isinstance(res, dict) and res.get("status") == "error":
            self._daemon._get_task_store().update_status(
                task.task_id, "failed", last_error=res.get("message", "dispatch failed")
            )

    def _spec_for_agent(self, agent_id: str) -> dict:
        from .agent_package import resolve_soul_prompt

        spec: dict = {"name": agent_id}
        meta = (self._daemon._agents or {}).get(agent_id)
        if meta is None:
            return {"name": agent_id, "system_prompt": "__missing_agent__"}
        model = str(meta.get("model", "") or "")
        if model:
            spec["model"] = model
        tools = meta.get("tools") or []
        if tools:
            spec["tools"] = list(tools)
        prompt = resolve_soul_prompt(agent_id)
        if prompt:
            spec["system_prompt"] = prompt
        return spec

    async def _execute_graph(self, task) -> None:
        variables: dict = {}
        raw = task.input or ""
        if raw:
            import json as _json

            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    variables = parsed
            except Exception:
                variables = {"input": raw}
        res = await self._rpc(
            "graph.execute_async",
            {
                "graph_id": task.graph_id,
                "input": task.input or "",
                "task_id": task.task_id,
                "variables": variables,
            },
        )
        if isinstance(res, dict) and res.get("status") == "error":
            self._daemon._get_task_store().update_status(
                task.task_id, "failed", last_error=str(res.get("message", ""))
            )

    async def _await_terminal(self, task_id: str) -> None:
        # The dispatchers write terminal status from their own background
        # tasks; the executor holds the claim and waits so retry/orphan
        # bookkeeping stays in one place.
        store = self._daemon._get_task_store()
        deadline = time.time() + _TERMINAL_WAIT_CAP
        while time.time() < deadline and not self._stopping:
            task = store.get(task_id)
            if task is None or task.status in _TERMINAL_STATUSES:
                return
            await asyncio.sleep(_TERMINAL_POLL_INTERVAL)
        logger.warning("TaskExecutor terminal wait capped for task %s", task_id)

    async def _finalize(self, task_id: str) -> None:
        store = self._daemon._get_task_store()
        task = store.get(task_id)
        if task is None:
            return
        status = task.status
        await self._broadcast(
            "task.completed" if status == "completed" else (
                "task.failed" if status == "failed" else "task.canceled"
            ),
            {
                "task_id": task_id,
                "status": status,
                "agent_id": task.agent_id,
                "graph_id": task.graph_id,
                "team": task.team,
            },
            team=task.team,
        )
        if status == "failed" and task.retry_count < task.max_retries:
            # #604: M1 dead machinery wired — terminal failure within the
            # retry budget re-enters the queue with a fresh attempt token.
            # attempt_token ownership check: only the claim holder requeues.
            if task.attempt_token and task.owner_agent == _OWNER:
                store.requeue(
                    task_id,
                    actor=_OWNER,
                    reason=f"retry {task.retry_count + 1}/{task.max_retries}",
                )

    async def _broadcast(self, event: str, payload: dict, team: str = "") -> None:
        try:
            await self._daemon._broadcast_event(event, payload, team=team or "default")
        except Exception as e:
            logger.warning("TaskExecutor broadcast %s failed: %s", event, e)

    async def _rpc(self, method: str, params: dict) -> dict:
        handler = self._daemon._get_handler(method)
        if handler is None:
            return {"status": "error", "message": f"RPC method not found: {method}"}
        return await handler(params)
