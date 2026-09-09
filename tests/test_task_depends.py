"""M3-2 issue#324: depends_on + task.run_chain contract tests.

Tests: depends_on field persistence, deps_met helper, dequeue gating,
migration v4, task.run_chain creates dependent tasks + step retry +
artifact passing.
"""

import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_runtime.task_store import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    Task,
    TaskStore,
)


def _temp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return TaskStore(db_path=path), path


class TestDependsOnField:
    def test_default_empty(self):
        t = Task()
        assert t.depends_on == []

    def test_to_dict(self):
        t = Task(depends_on=["t1", "t2"])
        d = t.to_dict()
        assert d["depends_on"] == ["t1", "t2"]

    def test_submit_reload_preserves(self):
        store, path = _temp_store()
        try:
            t0 = store.submit(Task(title="dep0", depends_on=[]))
            t1 = store.submit(Task(title="dep1", depends_on=[t0.task_id]))
            reloaded = store.get(t1.task_id)
            assert reloaded is not None
            assert reloaded.depends_on == [t0.task_id]
        finally:
            os.unlink(path)


class TestDepsMet:
    def test_no_deps(self):
        store, path = _temp_store()
        try:
            t = store.submit(Task(title="free"))
            assert store.deps_met(t.task_id) is True
        finally:
            os.unlink(path)

    def test_all_completed(self):
        store, path = _temp_store()
        try:
            d0 = store.submit(Task(title="d0"))
            d1 = store.submit(Task(title="d1"))
            store.update_status(d0.task_id, TASK_STATUS_COMPLETED)
            store.update_status(d1.task_id, TASK_STATUS_COMPLETED)
            t = store.submit(Task(title="child", depends_on=[d0.task_id, d1.task_id]))
            assert store.deps_met(t.task_id) is True
        finally:
            os.unlink(path)

    def test_one_pending(self):
        store, path = _temp_store()
        try:
            d0 = store.submit(Task(title="d0"))
            t = store.submit(Task(title="child", depends_on=[d0.task_id]))
            assert store.deps_met(t.task_id) is False
        finally:
            os.unlink(path)

    def test_one_missing(self):
        store, path = _temp_store()
        try:
            t = store.submit(Task(title="orphan", depends_on=["nonexistent_task"]))
            assert store.deps_met(t.task_id) is False
        finally:
            os.unlink(path)

    def test_dep_failed_not_met(self):
        store, path = _temp_store()
        try:
            d0 = store.submit(Task(title="d0"))
            store.update_status(d0.task_id, TASK_STATUS_FAILED)
            t = store.submit(Task(title="child", depends_on=[d0.task_id]))
            assert store.deps_met(t.task_id) is False
        finally:
            os.unlink(path)


class TestDequeueGating:
    def test_dequeue_skips_unmet_deps(self):
        store, path = _temp_store()
        try:
            d0 = store.submit(Task(title="d0", team="default"))
            child = store.submit(Task(title="child", depends_on=[d0.task_id], team="default"))
            # d0 claimed by another worker (running), so dequeue tries child
            store.update_status(d0.task_id, TASK_STATUS_RUNNING)
            result = store.dequeue("default", "worker1")
            assert result is None
            # child should still be pending (reverted)
            reloaded = store.get(child.task_id)
            assert reloaded.status == TASK_STATUS_PENDING
        finally:
            os.unlink(path)

    def test_dequeue_returns_met_deps(self):
        store, path = _temp_store()
        try:
            d0 = store.submit(Task(title="d0", team="default"))
            store.update_status(d0.task_id, TASK_STATUS_COMPLETED)
            child = store.submit(Task(title="child", depends_on=[d0.task_id], team="default"))
            result = store.dequeue("default", "worker1")
            assert result is not None
            assert result.task_id == child.task_id
        finally:
            os.unlink(path)


class TestMigrationV4:
    def test_fresh_db_has_depends_on(self):
        store, path = _temp_store()
        try:
            import sqlite3
            conn = sqlite3.connect(path)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            conn.close()
            assert "depends_on" in cols
            version = store._conn.execute("PRAGMA user_version").fetchone()[0]
            assert version >= 4
        finally:
            os.unlink(path)

    def test_old_db_migrates(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 3")
        # simulate a real v3 DB: full base schema (no depends_on yet)
        conn.execute(
            """CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT DEFAULT '',
                description TEXT DEFAULT '', agent_id TEXT DEFAULT '',
                graph_id TEXT DEFAULT '', trigger TEXT DEFAULT 'immediate',
                cron_expression TEXT DEFAULT '', run_at REAL DEFAULT 0,
                cron_job_id TEXT DEFAULT '', input TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 0,
                project_id TEXT DEFAULT '', artifact_ids TEXT DEFAULT '[]',
                last_result TEXT DEFAULT '{}', last_error TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0,
                last_run_at REAL DEFAULT 0, idempotency_key TEXT DEFAULT '',
                review_state TEXT DEFAULT 'none', attempt_token TEXT DEFAULT '',
                owner_role TEXT DEFAULT '', owner_agent TEXT DEFAULT '',
                resource_lease_id TEXT DEFAULT '', evidence_ref TEXT DEFAULT '',
                team TEXT DEFAULT 'default'
            )"""
        )
        conn.commit()
        conn.close()
        store = TaskStore(db_path=path)
        try:
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            assert "depends_on" in cols
            version = store._conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 4
        finally:
            os.unlink(path)


class TestRunChain:
    def _make_mock_daemon(self, store, runtime, graph_map):
        from agent_runtime.daemon_server import DaemonServer
        daemon = MagicMock(spec=DaemonServer)
        daemon._active_chains = {}
        daemon._get_task_store = MagicMock(return_value=store)
        daemon._get_runtime = MagicMock(return_value=runtime)
        daemon._graph_semaphore = None
        daemon.store = MagicMock()
        daemon.store.load_graph = MagicMock(side_effect=lambda gid: graph_map.get(gid))
        daemon._broadcast_event = AsyncMock()
        # bind real _run_chain_async to the mock
        daemon._run_chain_async = DaemonServer._run_chain_async.__get__(daemon, DaemonServer)
        return daemon

    @pytest.mark.asyncio
    async def test_run_chain_creates_tasks(self):
        store, path = _temp_store()
        try:
            graph = MagicMock()
            graph.agent_id = ""

            async def gen(*a, **kw):
                if False:
                    yield

            runtime = MagicMock()
            runtime.execute_graph = gen
            daemon = self._make_mock_daemon(store, runtime, {"g0": graph, "g1": graph})
            from agent_runtime.dispatchers.infra import InfraDispatcher
            disp = InfraDispatcher(daemon)
            result = await disp._handle_task_run_chain({
                "steps": [
                    {"title": "s0", "graph_id": "g0", "depends_on": [], "role": "writer", "output_keys": []},
                    {"title": "s1", "graph_id": "g1", "depends_on": [0], "role": "imager", "output_keys": []},
                ],
                "team": "default",
            })
            assert result["status"] == "started"
            chain_id = result["chain_id"]
            bg = daemon._active_chains.get(chain_id)
            if bg:
                await asyncio.wait_for(bg, timeout=5)
            tasks = store.list(limit=10)
            assert len(tasks) == 2
            tasks.sort(key=lambda d: d.get("created_at", 0))
            t0 = store.get(tasks[0]["task_id"])
            t1 = store.get(tasks[1]["task_id"])
            assert t1.depends_on == [t0.task_id]
            assert t1.status == TASK_STATUS_COMPLETED
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_run_chain_step_retry(self):
        store, path = _temp_store()
        try:
            graph = MagicMock()
            graph.agent_id = ""
            call_count = {"n": 0}

            async def gen(graph_arg, input_text, context=None, **kw):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    raise RuntimeError("boom")
                if False:
                    yield

            runtime = MagicMock()
            runtime.execute_graph = gen
            daemon = self._make_mock_daemon(store, runtime, {"g0": graph})
            from agent_runtime.dispatchers.infra import InfraDispatcher
            disp = InfraDispatcher(daemon)
            result = await disp._handle_task_run_chain({
                "steps": [
                    {"title": "s0", "graph_id": "g0", "depends_on": [], "role": "writer",
                     "output_keys": [], "max_retries": 2},
                ],
                "team": "default",
            })
            chain_id = result["chain_id"]
            bg = daemon._active_chains.get(chain_id)
            if bg:
                await asyncio.wait_for(bg, timeout=5)
            tasks = store.list(limit=10)
            assert len(tasks) == 1
            t0 = store.get(tasks[0]["task_id"])
            assert t0.status == TASK_STATUS_COMPLETED
            assert call_count["n"] == 3
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_run_chain_blocks_on_exhausted(self):
        store, path = _temp_store()
        try:
            graph = MagicMock()
            graph.agent_id = ""

            async def always_fail(graph_arg, input_text, context=None, **kw):
                raise RuntimeError("always fails")
                yield

            runtime = MagicMock()
            runtime.execute_graph = always_fail
            daemon = self._make_mock_daemon(store, runtime, {"g0": graph, "g1": graph})
            from agent_runtime.dispatchers.infra import InfraDispatcher
            disp = InfraDispatcher(daemon)
            result = await disp._handle_task_run_chain({
                "steps": [
                    {"title": "s0", "graph_id": "g0", "depends_on": [], "role": "writer",
                     "output_keys": [], "max_retries": 1},
                    {"title": "s1", "graph_id": "g1", "depends_on": [0], "role": "imager",
                     "output_keys": [], "max_retries": 1},
                ],
                "team": "default",
            })
            chain_id = result["chain_id"]
            bg = daemon._active_chains.get(chain_id)
            if bg:
                await asyncio.wait_for(bg, timeout=5)
            tasks = store.list(limit=10)
            tasks.sort(key=lambda d: d.get("created_at", 0))
            t0 = store.get(tasks[0]["task_id"])
            t1 = store.get(tasks[1]["task_id"])
            assert t0.status == TASK_STATUS_FAILED
            assert t1.status == TASK_STATUS_PENDING
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_run_chain_artifact_passing(self):
        store, path = _temp_store()
        try:
            g0 = MagicMock()
            g0.agent_id = ""
            g1 = MagicMock()
            g1.agent_id = ""
            passed_vars = {"s1": None}

            async def gen_s0(graph_arg, input_text, context=None, **kw):
                context.variables.set("script_json", "hello_script")
                if False:
                    yield

            async def gen_s1(graph_arg, input_text, context=None, **kw):
                passed_vars["s1"] = context.variables.get("script_json")
                context.variables.set("image_paths", ["img1.png"])
                if False:
                    yield

            def execute_graph(graph_arg, input_text, context=None, **kw):
                if graph_arg is g0:
                    return gen_s0(graph_arg, input_text, context, **kw)
                return gen_s1(graph_arg, input_text, context, **kw)

            runtime = MagicMock()
            runtime.execute_graph = execute_graph
            daemon = self._make_mock_daemon(store, runtime, {"g0": g0, "g1": g1})
            from agent_runtime.dispatchers.infra import InfraDispatcher
            disp = InfraDispatcher(daemon)
            result = await disp._handle_task_run_chain({
                "steps": [
                    {"title": "s0", "graph_id": "g0", "depends_on": [], "role": "writer",
                     "output_keys": ["script_json"], "max_retries": 0},
                    {"title": "s1", "graph_id": "g1", "depends_on": [0], "role": "imager",
                     "output_keys": ["image_paths"], "max_retries": 0},
                ],
                "team": "default",
            })
            chain_id = result["chain_id"]
            bg = daemon._active_chains.get(chain_id)
            if bg:
                await asyncio.wait_for(bg, timeout=5)
            assert passed_vars["s1"] == "hello_script"
            tasks = store.list(limit=10)
            tasks.sort(key=lambda d: d.get("created_at", 0))
            t1 = store.get(tasks[1]["task_id"])
            assert t1.status == TASK_STATUS_COMPLETED
        finally:
            os.unlink(path)


