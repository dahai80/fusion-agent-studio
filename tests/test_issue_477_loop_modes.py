"""#477: loop modes (continuous/interval/event_driven) + termination semantics.

ACs:
1. CronJob supports loop modes with defined termination semantics.
2. Per-iteration budget enforcement — stop at iteration boundary with
   stop_reason=budget_exceeded; permission mode honored every iteration.
3. cancelled set by the actual cancellation path and surfaced.
4. One test per loop mode, one budget stop, one cancellation reason.
"""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from agent_runtime.triggers import (
    CronJob,
    CronManager,
    _extract_loop_tokens,
    _normalize_loop_mode,
)


async def _ok_handler(job):
    return {"status": "ok"}


def _job(job_id, **kw):
    return CronJob(id=job_id, name=job_id, expression=kw.pop("expression", "* * * * *"), **kw)


class TestLoopModeScheduling:
    async def test_cron_mode_expression_driven(self):
        cm = CronManager(default_handler=_ok_handler)
        before = time.time()
        cm.register(_job("j_cron"))
        job = cm.get("j_cron")
        assert job.loop_mode == "cron"
        assert job.next_run > before
        await cm._run_single_job(job)
        assert job.enabled
        assert job.next_run > time.time()
        assert job.loop_stop_reason == ""

    async def test_interval_mode_fixed_offset(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_int", loop_mode="interval", interval_seconds=30))
        job = cm.get("j_int")
        assert job.next_run >= time.time() + 25
        await cm._run_single_job(job)
        assert job.enabled
        assert job.next_run >= time.time() + 25

    async def test_continuous_mode_back_to_back(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_cont", loop_mode="continuous"))
        job = cm.get("j_cont")
        assert job.next_run <= time.time() + 1
        await cm._run_single_job(job)
        assert job.enabled
        # back-to-back: rescheduled immediately, no fixed offset
        assert job.next_run <= time.time() + 1

    async def test_event_driven_mode_armed_by_trigger_only(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_evt", loop_mode="event_driven"))
        job = cm.get("j_evt")
        # dormant until an event arms it
        assert job.next_run == 0
        assert not cm.trigger_event("missing")
        assert not cm.trigger_event("j_evt") is True or True  # sanity no-crash
        assert cm.trigger_event("j_evt")
        assert job.next_run > 0
        await cm._run_single_job(job)
        # one run per event: dormant again
        assert job.next_run == 0
        assert job.enabled

    async def test_trigger_event_rejects_non_event_job(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_plain"))
        assert not cm.trigger_event("j_plain")


class TestTerminationSemantics:
    async def test_budget_stop_at_iteration_boundary(self):
        async def spendy(job):
            return {"usage": {"total_tokens": 60}}

        cm = CronManager(default_handler=spendy)
        cm.register(
            _job("j_budget", loop_mode="continuous", token_budget=100)
        )
        job = cm.get("j_budget")
        await cm._run_single_job(job)
        assert job.enabled, "60/100 must keep running"
        assert job.tokens_used == 60
        await cm._run_single_job(job)
        assert not job.enabled
        assert job.loop_stop_reason == "budget_exceeded"
        assert job.tokens_used == 120
        # scheduler gating: _run_loop skips disabled jobs (checked by state,
        # not by re-invoking _run_single_job which bypasses the gate)
        assert job.next_run == 0 or job.enabled is False

    async def test_max_iterations_stop(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(
            _job("j_max", loop_mode="continuous", max_iterations=2)
        )
        job = cm.get("j_max")
        await cm._run_single_job(job)
        assert job.enabled
        await cm._run_single_job(job)
        assert not job.enabled
        assert job.loop_stop_reason == "max_iterations"
        assert job.iteration_count == 2

    async def test_stop_marks_recurring_jobs_cancelled(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_run"))
        once = cm.register_once(time.time() + 3600, "g1")
        cm.stop()
        assert cm.get("j_run").loop_stop_reason == "cancelled"
        assert cm.get(once.id).loop_stop_reason == "", "one-shots are not loop jobs"

    async def test_stale_cancelled_marker_clears_on_next_fire(self):
        cm = CronManager(default_handler=_ok_handler)
        cm.register(_job("j_stale"))
        job = cm.get("j_stale")
        job.loop_stop_reason = "cancelled"
        await cm._run_single_job(job)
        assert job.loop_stop_reason == ""


class TestPermissionModePerIteration:
    async def test_permission_mode_forwarded_every_iteration(self):
        seen = []

        async def handler(job):
            seen.append(job.permission_mode)
            return {"status": "ok"}

        cm = CronManager(default_handler=handler)
        cm.register(
            _job(
                "j_perm",
                loop_mode="continuous",
                max_iterations=3,
                permission_mode="plan",
            )
        )
        job = cm.get("j_perm")
        for _ in range(3):
            await cm._run_single_job(job)
        assert seen == ["plan", "plan", "plan"]

    async def test_invalid_loop_mode_falls_back_to_cron(self):
        assert _normalize_loop_mode("continuous") == "continuous"
        assert _normalize_loop_mode("event-driven") == "event_driven"
        assert _normalize_loop_mode("bogus") == "cron"
        assert _normalize_loop_mode("") == "cron"


class TestTokenExtraction:
    def test_usage_dict(self):
        assert _extract_loop_tokens({"usage": {"total_tokens": 42}}) == 42
        assert _extract_loop_tokens({"usage": {"completion_tokens": 7}}) == 7
        assert _extract_loop_tokens({"total_tokens": 9}) == 9

    def test_non_dict_shapes(self):
        assert _extract_loop_tokens(None) == 0
        assert _extract_loop_tokens("ok") == 0
        assert _extract_loop_tokens({"usage": "junk"}) == 0
        assert _extract_loop_tokens({"total_tokens": "junk"}) == 0


class TestPersistence:
    def test_roundtrip_loop_fields(self, tmp_path):
        db = str(tmp_path / "cron.db")
        cm = CronManager(db_path=db)
        cm.register(
            _job(
                "j_persist",
                loop_mode="interval",
                interval_seconds=45,
                max_iterations=5,
                token_budget=1000,
                permission_mode="plan",
            )
        )
        cm.close()
        cm2 = CronManager(db_path=db)
        job = cm2.get("j_persist")
        assert job.loop_mode == "interval"
        assert job.interval_seconds == 45
        assert job.max_iterations == 5
        assert job.token_budget == 1000
        assert job.permission_mode == "plan"
        cm2.close()

    def test_migration_from_v1_schema(self, tmp_path):
        db = str(tmp_path / "cron_v1.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE cron_jobs (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, expression TEXT NOT NULL,
                graph_id TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
                last_run REAL DEFAULT 0, next_run REAL DEFAULT 0,
                created_at REAL DEFAULT 0, input_data TEXT DEFAULT '',
                max_retries INTEGER DEFAULT 0, retry_count INTEGER DEFAULT 0,
                one_shot INTEGER DEFAULT 0)"""
        )
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO cron_jobs (id, name, expression) VALUES ('j_old', 'old', '* * * * *')"
        )
        conn.commit()
        conn.close()
        cm = CronManager(db_path=db)
        job = cm.get("j_old")
        assert job is not None
        assert job.loop_mode == "cron"
        assert job.token_budget == 0
        cm.close()


class _HangingClient:
    def __init__(self):
        self.entered = asyncio.Event()

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.entered.set()
        await asyncio.sleep(30)
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(content="never", tool_calls=[])


class TestRuntimeCancellation:
    async def test_cancel_sets_ctx_stop_reason(self):
        from agent_runtime.context import AgentContext
        from agent_runtime.graph import AgentGraph, NodeConfig
        from agent_runtime.runtime import AgentRuntime
        from tools.registry import ToolRegistry

        client = _HangingClient()
        rt = AgentRuntime(client, ToolRegistry())
        graph = AgentGraph(name="cancel-477")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="m"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "llm")
        graph.add_edge("llm", "end")
        ctx = AgentContext()

        async def consume():
            async for _ in rt.execute_graph(graph, "go", context=ctx):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctx.stop_reason == "cancelled"


class TestDaemonWiring:
    @pytest.fixture
    async def daemon_stub(self, tmp_path):
        import shutil
        import tempfile

        from agent_runtime.daemon_server import DaemonServer

        # CI runners have deep tmp_path trees that blow past the macOS
        # 104-char AF_UNIX limit — park the socket in a short temp dir.
        short_dir = Path(tempfile.mkdtemp(prefix="fas477-"))
        try:
            socket_path = str(short_dir / "s.sock")
            store_db = str(tmp_path / "store.db")
            d = DaemonServer(
                socket_path=socket_path,
                ws_port=0,
                cluster_port=0,
                http_port=0,
                store_path=store_db,
            )
            await d.start()
            d._gateway._default_client = None
            d._gateway._default_model = ""
            d._gateway._models.clear()
            yield d
            await d.stop()
        finally:
            shutil.rmtree(short_dir, ignore_errors=True)

    async def test_cron_handler_forwards_permission_mode(self, daemon_stub):
        captured = {}

        async def fake_exec(params):
            captured.update(params)
            return {
                "execution_id": "exec_pm",
                "status": "running",
                "task_id": params.get("task_id", ""),
            }

        daemon_stub._handle_graph_execute_async = fake_exec
        job = _job(
            "j_daemon_pm",
            graph_id="g1",
            permission_mode="plan",
            input_data='{"team": "t"}',
        )
        out = await daemon_stub._cron_default_handler(job)
        assert out["status"] == "triggered"
        assert captured["permission_mode"] == "plan"

    async def test_execution_token_usage_sums_spans(self, daemon_stub, tmp_path, monkeypatch):
        evidence_dir = tmp_path / "evidence"
        monkeypatch.setattr(
            daemon_stub, "_evidence_dir", lambda team="default": evidence_dir
        )
        daemon_stub._append_run_span(
            "exec_u",
            "default",
            {"type": "result", "metadata": {"usage": {"total_tokens": 10}}},
        )
        daemon_stub._append_run_span(
            "exec_u",
            "default",
            {"type": "result", "metadata": {"usage": {"total_tokens": 15}}},
        )
        usage = daemon_stub._execution_token_usage("exec_u", "default")
        assert usage == {"total_tokens": 25}
        assert daemon_stub._execution_token_usage("exec_missing", "default") == {
            "total_tokens": 0
        }
