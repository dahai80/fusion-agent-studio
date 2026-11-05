#604 acceptance: task.submit is the only entry, TaskExecutor is the only
# exit. Plain agent_id submissions must execute without external polling;
# graph_id submissions execute and write results back; max_retries requeues;
# depends_on gates pickup; orphan running tasks recover on daemon restart.

import asyncio
import json
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.subagent import SubAgentReport
from agent_runtime.task_executor import TaskExecutor
from agent_runtime.task_store import Task, TaskStore


async def _wait_status(store, task_id, statuses, timeout=15.0):
    # await sleep — blocking time.sleep would starve the executor coroutine
    # (same event loop) and the task would never progress.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    task = store.get(task_id)
    while loop.time() < deadline:
        task = store.get(task_id)
        if task is not None and task.status in statuses:
            return task
        await asyncio.sleep(0.1)
    return task


class FakeRunner:
    def __init__(self, fail_first=0, error="boom"):
        self.calls = []
        self.fail_first = fail_first
        self.error = error

    async def run(self, spec):
        self.calls.append(spec)
        if len(self.calls) <= self.fail_first:
            return SubAgentReport(
                name=spec.name, ok=False, report="", error=self.error
            )
        return SubAgentReport(
            name=spec.name, ok=True, report="done", turns=1, agent_id="sess-1"
        )


@pytest.fixture
def task_db(tmp_path):
    return str(tmp_path / "tasks604.db")


@pytest.fixture
async def daemon(tmp_path, task_db):
    short_dir = tempfile.mkdtemp(prefix="fas604-")
    d = DaemonServer(
        socket_path=f"{short_dir}/s.sock",
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "store.db"),
    )
    d._load_agents_index()
    await d.start()
    d._task_store = TaskStore(db_path=task_db)
    yield d
    await d.stop()


def _fake_runner(daemon, runner):
    daemon._get_runtime().subagent_runner = runner


def _history(store, task_id):
    rows = store._conn.execute(
        "SELECT from_status, to_status, actor, reason FROM task_history "
        "WHERE task_id = ? ORDER BY ts",
        (task_id,),
    ).fetchall()
    return [
        dict(zip(("from_status", "to_status", "actor", "reason"), r))
        for r in rows
    ]


def _submit(store, **kw):
    params = {
        "title": kw.pop("title", "t"),
        "trigger": "immediate",
        "input": kw.pop("input", "do the thing"),
    }
    params.update(kw)
    return store.submit(Task(**params))


class TestPlainAgentExecution:
    async def test_plain_agent_submit_reaches_running_and_completes(self, daemon):
        store = daemon._get_task_store()
        runner = FakeRunner()
        _fake_runner(daemon, runner)
        task = _submit(store, agent_id="governance-steward")
        final = await _wait_status(store, task.task_id, {"completed"})
        assert final is not None and final.status == "completed"
        assert final.last_result.get("ok") is True
        # executor resolved the agent package into the spec
        assert runner.calls, "executor never dispatched subagent.run_task"
        assert runner.calls[0].name == "governance-steward"

    async def test_missing_agent_fails_visibly(self, daemon):
        store = daemon._get_task_store()
        runner = FakeRunner()
        _fake_runner(daemon, runner)
        task = _submit(store, agent_id="no-such-agent-604")
        final = await _wait_status(store, task.task_id, {"failed"})
        assert final is not None and final.status == "failed"
        assert "no-such-agent-604" in (final.last_error or "")
        assert not runner.calls


class TestGraphExecution:
    def _make_graph(self, daemon, graph_id):
        from agent_runtime.graph import AgentGraph, NodeConfig
        from agent_runtime.runtime import AgentRuntime
        from tools.registry import ToolRegistry

        class _EchoTool:
            name = "echo"
            description = "Echo tool"

            @property
            def parameters(self):
                return {"msg": {"type": "string"}}

            async def execute(self, **kwargs):
                return json.dumps({"status": "ok", "msg": kwargs.get("msg", "")})

            def openai_schema(self):
                return {
                    "type": "function",
                    "function": {"name": self.name, "description": self.description},
                }

        reg = ToolRegistry()
        reg.register(_EchoTool())
        daemon._runtime = AgentRuntime(tool_registry=reg)
        graph = AgentGraph(id=graph_id, name="604 Test Graph")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "echo",
            NodeConfig(type="tool", label="Echo", tool_name="echo", tool_params={"msg": "hi"}),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "echo")
        graph.add_edge("echo", "end")
        daemon.store.save_graph(graph)

    async def test_graph_submit_executes_without_cron(self, daemon):
        store = daemon._get_task_store()
        self._make_graph(daemon, "g-604")
        task = _submit(store, graph_id="g-604", input=json.dumps({"team": "t"}))
        final = await _wait_status(store, task.task_id, {"completed"})
        assert final is not None and final.status == "completed"
        assert final.last_result.get("execution_id", "").startswith("exec_")


class TestRetries:
    async def test_max_retries_two_then_terminal(self, daemon):
        store = daemon._get_task_store()
        runner = FakeRunner(fail_first=2, error="transient-604")
        _fake_runner(daemon, runner)
        task = _submit(
            store, agent_id="governance-steward", max_retries=2
        )
        final = await _wait_status(store, task.task_id, {"completed"}, timeout=25)
        assert final is not None and final.status == "completed"
        assert len(runner.calls) == 3
        requeues = [
            h for h in _history(store, task.task_id)
            if h["to_status"] == "pending" and "retry" in h["reason"]
        ]
        assert len(requeues) == 2

    async def test_retries_exhausted_fails_terminal(self, daemon):
        store = daemon._get_task_store()
        runner = FakeRunner(fail_first=99, error="always-604")
        _fake_runner(daemon, runner)
        task = _submit(store, agent_id="governance-steward", max_retries=1)
        # wait past the transient failed sighting (requeue flips it back to
        # pending): terminal failed means retry_count has hit max_retries
        deadline = asyncio.get_event_loop().time() + 25
        final = store.get(task.task_id)
        while asyncio.get_event_loop().time() < deadline:
            final = store.get(task.task_id)
            if (
                final is not None
                and final.status == "failed"
                and final.retry_count >= 1
            ):
                break
            await asyncio.sleep(0.1)
        assert final is not None and final.status == "failed"
        assert len(runner.calls) == 2


class TestDependencies:
    async def test_unmet_dep_blocks_pickup_then_releases(self, daemon):
        store = daemon._get_task_store()
        runner = FakeRunner(fail_first=99, error="dep-block-604")
        _fake_runner(daemon, runner)
        blocker = _submit(store, agent_id="governance-steward", max_retries=0)
        final = await _wait_status(store, blocker.task_id, {"failed"})
        assert final.status == "failed"
        dependent = _submit(
            store,
            agent_id="governance-steward",
            depends_on=[blocker.task_id],
        )
        await asyncio.sleep(2)
        still = store.get(dependent.task_id)
        assert still.status == "pending", f"picked up despite unmet dep: {still.status}"
        assert still.owner_agent == ""
        # dependency satisfied -> pickup releases (runner now succeeds)
        runner.fail_first = 0
        store.update_status(blocker.task_id, "completed")
        released = await _wait_status(store, dependent.task_id, {"completed"}, timeout=20)
        assert released is not None and released.status == "completed"


class TestOrphanRecovery:
    async def test_orphan_running_task_recovers_on_restart(self, daemon, task_db):
        store = daemon._get_task_store()
        runner = FakeRunner()
        _fake_runner(daemon, runner)
        # simulate a crash: task claimed by the executor, then daemon died
        orphan = _submit(store, agent_id="governance-steward")
        store.update_status(orphan.task_id, "running")
        claimed = store.get(orphan.task_id)
        claimed.owner_agent = "task-executor"
        claimed.attempt_token = "tok-604"
        store._save_task(claimed)
        store._tasks.pop(orphan.task_id, None)

        assert daemon._task_executor is not None
        await daemon.stop()
        await daemon.start()
        d2_store = daemon._get_task_store()
        recovered = await _wait_status(d2_store, orphan.task_id, {"completed"}, timeout=20)
        assert recovered is not None and recovered.status == "completed"
        assert any(
            "orphan reset" in h["reason"] for h in _history(d2_store, orphan.task_id)
        )


class TestDisabledMode:
    async def test_disabled_executor_never_claims(self, daemon, monkeypatch):
        executor = TaskExecutor(daemon)
        assert executor.running is False
        from agent_runtime import task_executor as te

        monkeypatch.setenv("FUSION_TASK_EXECUTOR_DISABLED", "1")
        assert te.executor_enabled() is False
        from agent_runtime.task_executor import DisabledTaskExecutor

        stub = DisabledTaskExecutor()
        assert stub.running is False
        stub.start()
        await stub.stop()


class TestSchedulePlaceholders:
    async def test_cron_placeholder_never_claimed(self, daemon):
        # task.submit trigger=cron registers a CronJob and leaves a pending
        # placeholder task on the board — the executor must not run it
        # directly (the job fire lands its own executable task).
        store = daemon._get_task_store()
        runner = FakeRunner()
        _fake_runner(daemon, runner)
        real = _submit(store, agent_id="governance-steward")
        placeholder = _submit(
            store,
            agent_id="governance-steward",
            trigger="cron",
            cron_expression="*/5 * * * *",
        )
        done = await _wait_status(store, real.task_id, {"completed"})
        assert done is not None and done.status == "completed"
        await asyncio.sleep(2)
        ph = store.get(placeholder.task_id)
        assert ph.status == "pending" and ph.owner_agent == ""
        assert len(runner.calls) == 1
