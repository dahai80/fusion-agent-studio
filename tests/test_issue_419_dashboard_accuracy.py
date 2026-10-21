"""Issue #419 — dashboard accuracy.

The dashboard previously mirrored the raw agents index (258 rows / 15 unique
names after test residue), reported 0 requests / 0 tokens while runs existed
(sessions are a legacy path; runs live in TaskStore + run spans), and showed
"no model status" because mlx.status claimed running=false when the daemon
does not own the MLX process (standalone start.sh deployment).

Pins:
  1. dashboard.overview counts the name-deduped agent view (same tie-break
     as agent.list) — duplicates collapse, totals match the deduped view.
  2. TaskStore tasks touched in the last 24h count as today_requests; token
     totals come from the run-span aggregate of each task's execution_id.
  3. mlx.status probes engine health when the daemon process handle is None —
     a standalone engine yields running=true + a models list.
"""
from __future__ import annotations

import tempfile
import time

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.dispatchers.infra import InfraDispatcher
from agent_runtime.task_store import Task, TaskStore


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    import os

    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def daemon(socket_path, tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_OUT_DIR", str(tmp_path / "out"))
    d = DaemonServer(
        socket_path=socket_path,
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "test_store.db"),
    )
    d._task_store = TaskStore(db_path=str(tmp_path / "tasks.db"))
    await d.start()
    yield d
    await d.stop()


def _seed_dup_index(daemon: DaemonServer) -> None:
    # #419: raw index residue — same name twice with different recency, the
    # newer entry wins exactly like AgentDispatcher._agent_recency tie-break.
    daemon._agents_index_loaded = True
    daemon._agents = {
        "old": {
            "name": "dup-agent",
            "status": "draft",
            "version_int": 1,
            "created_at": "2026-01-01T00:00:00",
        },
        "new": {
            "name": "dup-agent",
            "status": "published",
            "version_int": 2,
            "created_at": "2026-01-02T00:00:00",
        },
        "solo": {
            "name": "solo-agent",
            "status": "draft",
            "version_int": 1,
            "created_at": "2026-01-03T00:00:00",
        },
    }


class TestIssue419DashboardDedupe:
    @pytest.mark.asyncio
    async def test_overview_counts_deduped_agents(self, daemon):
        _seed_dup_index(daemon)
        result = await InfraDispatcher(daemon)._handle_dashboard_overview({})
        assert result["total_agents"] == 2, "duplicate names collapse to one entry"
        assert result["published_agents"] == 1, "newer dup is the published one"
        assert result["active_agents"] == 2
        names = [a["name"] for a in result["recent_agents"]]
        assert len(names) == len(set(names)), "recent_agents carries no duplicates"

    @pytest.mark.asyncio
    async def test_overview_dedupe_prefers_higher_recency(self, daemon):
        _seed_dup_index(daemon)
        result = await InfraDispatcher(daemon)._handle_dashboard_overview({})
        # recent_agents reflects the surviving entry per name — the published
        # newer manifest, not the stale draft.
        recent = {a["name"]: a["status"] for a in result["recent_agents"]}
        assert recent.get("dup-agent") == "published"


class TestIssue419DashboardCounters:
    @pytest.mark.asyncio
    async def test_task_runs_count_as_requests_and_tokens(self, daemon):
        now = time.time()
        exec_id = "exec-419-tokens"
        # Span: 3 token events on one node + a usage-bearing result event.
        daemon._append_run_span(exec_id, "default", {
            "type": "start", "timestamp": now - 10, "node_id": "",
        })
        for _ in range(3):
            daemon._append_run_span(exec_id, "default", {
                "type": "token", "timestamp": now - 5, "node_id": "llm-2", "content": "x",
            })
        daemon._append_run_span(exec_id, "default", {
            "type": "result", "timestamp": now - 4, "node_id": "llm-1",
            "metadata": {"usage": {"total_tokens": 42}},
        })
        daemon._append_run_span(exec_id, "default", {
            "type": "run_end", "timestamp": now - 1, "status": "completed",
        })
        daemon._task_store.submit(Task(
            title="t-419",
            agent_id="solo",
            status="completed",
            created_at=now - 30,
            updated_at=now - 10,
            last_result={"execution_id": exec_id},
        ))
        # Old task outside the 24h window must not count. submit() stamps
        # updated_at=now unconditionally, so freeze the clock during submit.
        import agent_runtime.task_store as ts_mod

        class _FrozenTime:
            @staticmethod
            def time():
                return now - 86400 * 3

        real_time = ts_mod.time
        ts_mod.time = _FrozenTime
        try:
            daemon._task_store.submit(Task(
                title="t-419-old",
                agent_id="solo",
                status="completed",
                created_at=now - 86400 * 3,
                updated_at=now - 86400 * 3,
                last_result={"execution_id": exec_id},
            ))
        finally:
            ts_mod.time = real_time
        result = await InfraDispatcher(daemon)._handle_dashboard_overview({})
        assert result["today_requests"] == 1
        # 3 per-token events + 42 usage tokens = 45.
        assert result["total_tokens"] == 45

    @pytest.mark.asyncio
    async def test_failed_task_counts_as_error(self, daemon):
        now = time.time()
        daemon._task_store.submit(Task(
            title="t-419-fail",
            agent_id="solo",
            status="failed",
            created_at=now - 30,
            updated_at=now - 10,
        ))
        result = await InfraDispatcher(daemon)._handle_dashboard_overview({})
        assert result["today_requests"] == 1
        assert result["error_count"] >= 1


class TestIssue419MlxStatusFallback:
    @pytest.mark.asyncio
    async def test_standalone_engine_reports_running(self, daemon):
        # Daemon owns no MLX process but the engine answers /models —
        # standalone start.sh deployment must not render "no model status".
        async def fake_health():
            return True

        async def fake_models():
            return [{"id": "qwen3-4b"}]

        daemon._check_mlx_health = fake_health
        daemon._list_mlx_models = fake_models
        result = await daemon._handle_mlx_status({})
        assert result["running"] is True
        assert result["models"] == [{"id": "qwen3-4b"}]

    @pytest.mark.asyncio
    async def test_no_engine_stays_not_running(self, daemon):
        async def fake_health():
            return False

        daemon._check_mlx_health = fake_health
        result = await daemon._handle_mlx_status({})
        assert result["running"] is False
        assert result["models"] == []


class TestIssue417AgentUsage:
    @pytest.mark.asyncio
    async def test_agent_usage_aggregates_task_spans(self, daemon):
        # #417: per-agent usage comes from TaskStore + run spans, not the
        # empty legacy MetricsEngine sessions path.
        now = time.time()
        exec_id = "exec-417-usage"
        daemon._append_run_span(exec_id, "default", {
            "type": "token", "timestamp": now - 5, "node_id": "llm-1", "content": "x",
        })
        daemon._append_run_span(exec_id, "default", {
            "type": "run_end", "timestamp": now - 1, "status": "completed",
        })
        daemon._agents_index_loaded = True
        daemon._agents = {"ag-1": {"name": "usage-agent", "status": "draft"}}
        daemon._task_store.submit(Task(
            title="t-417-a",
            agent_id="ag-1",
            status="completed",
            created_at=now - 30,
            updated_at=now - 10,
            last_result={"execution_id": exec_id},
        ))
        daemon._task_store.submit(Task(
            title="t-417-b",
            agent_id="ag-1",
            status="failed",
            created_at=now - 30,
            updated_at=now - 5,
        ))
        result = await InfraDispatcher(daemon)._handle_analytics_agent_usage({})
        rows = result["agents"]
        assert len(rows) == 1
        row = rows[0]
        assert row["agent_id"] == "ag-1"
        assert row["agent_name"] == "usage-agent"
        assert row["runs"] == 2
        assert row["requests"] == 2
        assert row["tokens"] == 1
        assert row["errors"] == 1

    @pytest.mark.asyncio
    async def test_agent_usage_attributes_graph_scoped_runs(self, daemon):
        # GUI tasks carry only graph_id — attribution resolves through the
        # graph's bound agent_id in its stored data.
        from agent_runtime.graph import AgentGraph

        now = time.time()
        exec_id = "exec-417-graph"
        daemon._append_run_span(exec_id, "default", {
            "type": "token", "timestamp": now - 5, "node_id": "llm-1", "content": "x",
        })
        daemon._append_run_span(exec_id, "default", {
            "type": "run_end", "timestamp": now - 1, "status": "completed",
        })
        daemon.store.save_graph(AgentGraph(
            id="g-417",
            name="usage-graph",
            description="",
            agent_id="ag-graph",
        ))
        daemon._task_store.submit(Task(
            title="t-417-graph",
            agent_id="",
            graph_id="g-417",
            status="completed",
            created_at=now - 30,
            updated_at=now - 10,
            last_result={"execution_id": exec_id},
        ))
        result = await InfraDispatcher(daemon)._handle_analytics_agent_usage({})
        row = next((r for r in result["agents"] if r["agent_id"] == "ag-graph"), None)
        assert row is not None, "graph-scoped task attributed to bound agent"
        assert row["runs"] == 1
        assert row["tokens"] == 1

    @pytest.mark.asyncio
    async def test_agent_usage_range_filter(self, daemon):
        now = time.time()
        import agent_runtime.task_store as ts_mod

        class _FrozenTime:
            @staticmethod
            def time():
                return now - 86400 * 3

        real_time = ts_mod.time
        ts_mod.time = _FrozenTime
        try:
            daemon._task_store.submit(Task(
                title="t-417-old",
                agent_id="ag-1",
                status="completed",
                created_at=now - 86400 * 3,
                updated_at=now - 86400 * 3,
            ))
        finally:
            ts_mod.time = real_time
        result = await InfraDispatcher(daemon)._handle_analytics_agent_usage({})
        assert all(r["agent_id"] != "ag-1" for r in result["agents"])
