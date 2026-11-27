"""Audit 0825 Bundle5 tests — P2 infrastructure + maintainability.

3M-6: schema version management (PRAGMA user_version gates ordered migrations).
E-18: mlx.reconnect RPC (runtime env hot-swap of client).
Verify E-17/E-20/E-21/A-7 landed (regression assertions).
"""

from __future__ import annotations

import sqlite3

from agent_runtime.memory_engine import MemoryEngine
from agent_runtime.persistence import AgentStore
from agent_runtime.task_store import TaskStore
from agent_runtime.triggers import CronManager


class Test3M6SchemaVersioning:
    """3M-6: PRAGMA user_version gates ordered migrations; safe for old/new/rerun DBs."""

    def test_persistence_new_db_sets_user_version(self, tmp_path):
        store = AgentStore(str(tmp_path / "store.db"))
        conn = store._get_conn()
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        store.close()

    def test_persistence_old_db_without_columns_migrates(self, tmp_path):
        # Simulate an old DB: hand-create the checkpoints table without
        # graph_id/state_json columns.
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE graphs (id TEXT PRIMARY KEY, name TEXT, description TEXT,
                data TEXT, version TEXT, created_at REAL, updated_at REAL);
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, graph_id TEXT,
                name TEXT, status TEXT, created_at REAL, finished_at REAL);
            CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, context_json TEXT, current_node_id TEXT,
                iteration_count INTEGER, created_at REAL);
            CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, title TEXT, mode TEXT,
                messages_json TEXT, active_branch TEXT, graph_id TEXT,
                metadata_json TEXT, created_at REAL, updated_at REAL);
            CREATE TABLE workflows (id TEXT PRIMARY KEY, name TEXT, data TEXT, created_at REAL);
            CREATE TABLE workflow_runs (id TEXT PRIMARY KEY, workflow_id TEXT,
                data TEXT, status TEXT, created_at REAL, updated_at REAL);
        """)
        conn.commit()
        conn.close()
        store = AgentStore(str(db))
        c = store._get_conn()
        cols = {row[1] for row in c.execute("PRAGMA table_info(checkpoints)")}
        assert "graph_id" in cols
        assert "state_json" in cols
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        store.close()

    def test_persistence_reinit_is_idempotent(self, tmp_path):
        db = str(tmp_path / "store.db")
        s1 = AgentStore(db)
        v1 = s1._get_conn().execute("PRAGMA user_version").fetchone()[0]
        s1.close()
        s2 = AgentStore(db)
        v2 = s2._get_conn().execute("PRAGMA user_version").fetchone()[0]
        assert v1 == v2
        s2.close()

    def test_task_store_new_db_sets_user_version(self, tmp_path):
        ts = TaskStore(str(tmp_path / "tasks.db"))
        ver = ts._conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        ts.close()

    def test_task_store_old_db_migrates_project_id(self, tmp_path):
        db = tmp_path / "old_tasks.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT, description TEXT,
                agent_id TEXT, graph_id TEXT, trigger TEXT, cron_expression TEXT,
                run_at REAL, cron_job_id TEXT, input TEXT, status TEXT, priority INTEGER,
                artifact_ids TEXT, last_result TEXT, last_error TEXT,
                retry_count INTEGER, max_retries INTEGER,
                created_at REAL, updated_at REAL, last_run_at REAL);
        """)
        conn.commit()
        conn.close()
        ts = TaskStore(str(db))
        cols = {row[1] for row in ts._conn.execute("PRAGMA table_info(tasks)")}
        assert "project_id" in cols
        ver = ts._conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        ts.close()

    def test_triggers_new_db_sets_user_version(self, tmp_path):
        cm = CronManager(db_path=str(tmp_path / "cron.db"))
        ver = cm._conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        cm.close()

    def test_triggers_old_db_migrates_one_shot(self, tmp_path):
        db = tmp_path / "old_cron.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE cron_jobs (id TEXT PRIMARY KEY, name TEXT, expression TEXT,
                graph_id TEXT, enabled INTEGER, last_run REAL, next_run REAL,
                created_at REAL, input_data TEXT, max_retries INTEGER, retry_count INTEGER);
            CREATE TABLE cron_executions (id TEXT PRIMARY KEY, job_id TEXT,
                started_at REAL, finished_at REAL, status TEXT, error TEXT,
                result_preview TEXT);
        """)
        conn.commit()
        conn.close()
        cm = CronManager(db_path=str(db))
        cols = {row[1] for row in cm._conn.execute("PRAGMA table_info(cron_jobs)")}
        assert "one_shot" in cols
        ver = cm._conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
        cm.close()

    def test_memory_engine_sets_user_version(self, tmp_path):
        me = MemoryEngine(str(tmp_path / "mem.db"))
        ver = me.conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 2
        cols = {row[1] for row in me.conn.execute("PRAGMA table_info(memories)")}
        assert "tier" in cols
        assert "memory_type" in cols
        me.close()


class TestE18MlxReconnect:
    """E-18: mlx.reconnect RPC re-attaches the client at runtime; env hot-swap works."""

    def test_mlx_reconnect_handler_registered(self):
        # Skip building a full DaemonServer (needs socket/port); verify the method
        # exists + dict registration in source.
        import inspect

        from agent_runtime import daemon_server

        assert hasattr(daemon_server.DaemonServer, "_handle_mlx_reconnect")
        src = inspect.getsource(daemon_server.DaemonServer._core_handlers)
        assert '"mlx.reconnect"' in src
        assert "_handle_mlx_reconnect" in src

    def test_mlx_reconnect_reattaches_with_new_env(self, tmp_path, monkeypatch):
        # After switching env, reconnect should re-resolve base_url and re-attach.
        from agent_runtime.daemon_server import DaemonServer

        ds = DaemonServer.__new__(DaemonServer)
        ds._mlx_process = None

        class FakeGateway:
            def __init__(self):
                self._default_client = None
                self._default_model = "test-model"

            def set_default_client(self, c):
                self._default_client = c

            def set_mlx_direct_client(self, c):
                pass

            def register_default_local(self, **kw):
                pass

        ds._gateway = FakeGateway()
        ds._is_gateway_path = lambda: False
        ds._attach_mlx_api_key = lambda: "local"
        ds._discover_mlx_model_id = lambda key: "test-model"

        monkeypatch.setenv("FUSION_GATEWAY_URL", "http://localhost:99999/v1")
        result = asyncio_run(ds._handle_mlx_reconnect({}))
        assert result["status"] == "reconnected"
        assert "base_url" in result
        assert result["default_model"] == "test-model"


class TestBundle5PriorFixesRegression:
    """Verify prior-bundle P2 fixes are still in place (E-17/E-20/E-21/A-7)."""

    def test_e17_core_handlers_single_source(self):
        # E-17: _core_handlers is the single source of truth; no second duplicate dict.
        import inspect

        from agent_runtime import daemon_server

        src = inspect.getsource(daemon_server.DaemonServer._core_handlers)
        assert '"daemon.ping"' in src
        assert '"graph.resume"' in src
        # _get_handler / _handle_rpc_discover should reuse _core_handlers, not build
        # their own dict.
        for method in ("_get_handler",):
            if hasattr(daemon_server.DaemonServer, method):
                msrc = inspect.getsource(getattr(daemon_server.DaemonServer, method))
                assert "_core_handlers" in msrc, f"{method} not using single source"

    def test_e21_interpolate_complex_types_json(self):
        # E-21: dict/list/None/bool interpolation produces valid JSON, not Python repr.
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("data", {"a": 1, "b": True})
        vm.set("lst", [1, "x", None])
        vm.set("flag", False)
        vm.set("nil", None)
        out = vm.interpolate("{{data}}|{{lst}}|{{flag}}|{{nil}}")
        assert '"a": 1' in out
        assert "true" in out
        assert "null" in out
        assert "false" in out
        # No Python repr single quotes.
        assert "'" not in out

    def test_a7_error_sentinel_checked_in_callers(self):
        # A-7/E-12: planner/workflow/chat/verifier all check finish_reason=="error".
        import inspect

        from agent_runtime import chat_engine, planner, verifier, workflow_engine

        targets = {
            planner: "_plan_with_llm",
            workflow_engine: "_run_agent",
            chat_engine: "_run_simple_turn",
            verifier: "verify",
        }
        for mod, name in targets.items():
            fn = getattr(mod, name, None)
            if fn is None:
                # Method names may differ; fall back to scanning module source for the
                # sentinel check.
                src = inspect.getsource(mod)
                assert "finish_reason" in src and '"error"' in src, (
                    f"{mod.__name__} missing error sentinel check"
                )
                continue
            src = inspect.getsource(fn)
            assert "finish_reason" in src, f"{mod.__name__}.{name} missing sentinel"


def asyncio_run(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
