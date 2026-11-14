# Issue #701: dispatch-level subagent spawns route through the fusion-harness
# kernel orchestrator with SpawnBrief contract enforcement; in-tree runner
# stays as fail-open fallback.
from __future__ import annotations

import json

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from tests.test_issue_341_subagent import _llm_response, _make_parent


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


def _runtime_with_kernel(**kwargs):
    rt, client = _make_parent(**kwargs)
    orch = build_kernel_orchestrator(rt)
    rt.kernel = {"orchestrator": orch}
    return rt, client, orch


class TestKernelSpawnRoute:
    @pytest.mark.asyncio
    async def test_spawn_executes_via_kernel_runner(self, daemon, tmp_path):
        rt, _client, orch = _runtime_with_kernel(responses=[_llm_response(content="kernel report body")])
        daemon._get_runtime = lambda: rt
        kernel_runner = orch["runner"]
        assert kernel_runner._counter == 0
        res = await _run(daemon, "subagent.run", {"spec": {"name": "w1", "task": "do it"}})
        assert res["status"] == "ok"
        assert res["report"]["via"] == "kernel-orchestrator"
        assert res["report"]["ok"] is True
        assert res["report"]["report"] == "kernel report body"
        # Spawn actually flowed through the kernel SubagentRunner, not only
        # the in-tree one driven by agent_fn.
        assert kernel_runner._counter == 1

    @pytest.mark.asyncio
    async def test_incomplete_caller_brief_rejected_before_worker(self, daemon):
        rt, _client, orch = _runtime_with_kernel(responses=[_llm_response(content="x")])
        daemon._get_runtime = lambda: rt
        before = orch["runner"]._counter
        res = await _run(
            daemon, "subagent.run",
            {"spec": {"name": "w2", "task": "do it"}, "brief": {"objective": "half-baked"}},
        )
        assert res["status"] == "error"
        assert "brief payload missing fields" in res["message"]
        assert orch["runner"]._counter == before, "no worker may run on a bad brief"

    @pytest.mark.asyncio
    async def test_derived_brief_invalid_spec_rejected(self, daemon):
        rt, _client, orch = _runtime_with_kernel(responses=[_llm_response(content="x")])
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "subagent.run", {"spec": {"name": "w3", "task": "  "}})
        assert res["status"] == "error"
        assert "spawn brief" in res["message"]
        assert "objective" in res["message"]

    @pytest.mark.asyncio
    async def test_complete_caller_brief_accepted(self, daemon):
        rt, _client, orch = _runtime_with_kernel(responses=[_llm_response(content="done")])
        daemon._get_runtime = lambda: rt
        brief = {
            "objective": "summarize the file",
            "output_format": "report",
            "tool_guidance": "no tools",
            "boundaries": "read-only",
            "token_budget": 1000,
            "concurrency": 1,
            "depth": 1,
        }
        res = await _run(
            daemon, "subagent.run", {"spec": {"name": "w4", "task": "go"}, "brief": brief}
        )
        assert res["status"] == "ok"
        assert res["report"]["via"] == "kernel-orchestrator"

    @pytest.mark.asyncio
    async def test_fail_open_without_kernel_orchestrator(self, daemon):
        rt, _client = _make_parent(responses=[_llm_response(content="in-tree report")])
        rt.kernel = {}
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "subagent.run", {"spec": {"name": "w5", "task": "do it"}})
        assert res["status"] == "ok"
        assert "via" not in res["report"], "fallback path must stay in-tree shaped"

    @pytest.mark.asyncio
    async def test_depth_guard_enforced(self, daemon):
        rt, _client, orch = _runtime_with_kernel(responses=[_llm_response(content="x")])
        daemon._get_runtime = lambda: rt
        brief = {
            "objective": "deep", "output_format": "report", "tool_guidance": "none",
            "boundaries": "none", "token_budget": 100, "concurrency": 1, "depth": 4,
        }
        res = await _run(
            daemon, "subagent.run", {"spec": {"name": "w6", "task": "go"}, "brief": brief}
        )
        assert res["status"] == "error"
        assert "depth" in res["message"]


class TestJsonRoundtrip:
    @pytest.mark.asyncio
    async def test_kernel_report_serializable(self, daemon):
        rt, _client, _orch = _runtime_with_kernel(responses=[_llm_response(content="r")])
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "subagent.run", {"spec": {"name": "w7", "task": "t"}})
        json.dumps(res)
