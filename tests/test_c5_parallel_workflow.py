"""Tests for C5 parallel graph node fan-out/gather + workflow SQLite persistence
(P1-2, issue #190).

Two capabilities:
1. parallel node — real fan-out via asyncio.gather, edge-order merge, single-branch
   + plan_mode fallback, merge-node discovery.
2. workflow persistence — WorkflowEngine with AgentStore: create/get/list/delete
   + run execute/pause/resume/cancel/status survive store round-trip.
"""

from __future__ import annotations

import pytest

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.persistence import AgentStore
from agent_runtime.runtime import AgentRuntime
from agent_runtime.workflow_engine import (
    WorkflowEngine,
    WorkflowStatus,
)
from server.fusion_mlx_client import LLMResponse, StreamChunk


def _llm_response(content="", tool_calls=None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
        finish_reason="stop",
    )


class ScriptedClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.calls = 0

    async def chat(self, model, messages, tools=None, **kwargs):
        self.calls += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return _llm_response(content="done")

    async def chat_stream(self, messages, model="", **kwargs):
        self.calls += 1
        content = ""
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            content = resp.content or ""
        yield StreamChunk(delta_content=content, delta_tool_calls=[], finish_reason="stop")

    async def embeddings(self, model, input, **kwargs):
        if isinstance(input, str):
            return [[0.1] * 8]
        return [[0.1] * 8 for _ in input]


def _make_runtime(responses):
    from tools.registry import ToolRegistry

    client = ScriptedClient(responses)
    gw = LLMGateway()
    gw.set_default_client(client)
    gw.register_model(ModelConfig(name="test-model", provider="local", context_length=4096))
    rt = AgentRuntime(llm_gateway=gw, tool_registry=ToolRegistry())
    return rt, client


async def _collect(rt, g, prompt, stream=False):
    events = []
    gen = rt.execute_graph_stream(g, prompt) if stream else rt.execute_graph(g, prompt)
    async for e in gen:
        events.append(e)
    return events


def _parallel_graph(num_branches=2, branch_model="test-model"):
    # start -> parallel -> [b0, b1, ...] -> merge -> end
    # each branch is an llm node; merge is llm node; end.
    g = AgentGraph(name="c5_parallel_test", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node(
        "parallel",
        NodeConfig(type="parallel", label="fanout"),
    )
    g.add_node("merge", NodeConfig(type="llm", label="merge", model=branch_model))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "parallel")
    g.add_edge("merge", "end")
    for i in range(num_branches):
        bid = f"b{i}"
        g.add_node(bid, NodeConfig(type="llm", label=f"branch{i}", model=branch_model))
        g.add_edge("parallel", bid, label=f"branch{i}")
        g.add_edge(bid, "merge")
    return g


class TestParallelNodeFanOut:
    @pytest.mark.asyncio
    async def test_parallel_branches_concurrent(self):
        # Each branch llm returns after gather; 3 branches each scripted instant.
        # No sleep tool here — concurrency proven by single gather call, not timing.
        # 4 responses: b0, b1, b2, merge.
        resps = [
            _llm_response(content="out0"),
            _llm_response(content="out1"),
            _llm_response(content="out2"),
            _llm_response(content="merged"),
        ]
        rt, client = _make_runtime(resps)
        g = _parallel_graph(num_branches=3)
        events = await _collect(rt, g, "run")
        contents = [e.content for e in events if e.type == AgentEventType.THINK]
        assert any("out0" in c for c in contents)
        assert any("out1" in c for c in contents)
        assert any("out2" in c for c in contents)
        assert any("merged" in c for c in contents)
        # all 4 llm calls happened (3 branches + 1 merge).
        assert client.calls == 4

    @pytest.mark.asyncio
    async def test_parallel_branch_events_tagged(self):
        # Branch events carry [parallel:label] tag.
        resps = [
            _llm_response(content="A"),
            _llm_response(content="B"),
            _llm_response(content="M"),
        ]
        rt, _ = _make_runtime(resps)
        g = _parallel_graph(num_branches=2)
        events = await _collect(rt, g, "run")
        branch_events = [
            e.content
            for e in events
            if e.type == AgentEventType.THINK and e.content.startswith("[parallel:")
        ]
        assert any("branch0" in c and "A" in c for c in branch_events)
        assert any("branch1" in c and "B" in c for c in branch_events)

    @pytest.mark.asyncio
    async def test_parallel_merge_node_discovered(self):
        # merge node = common successor of all branch targets.
        resps = [
            _llm_response(content="x"),
            _llm_response(content="y"),
            _llm_response(content="merged"),
        ]
        rt, _ = _make_runtime(resps)
        g = _parallel_graph(num_branches=2)
        # _find_merge_node should return "merge" (nearest common successor).
        outgoing = g.get_outgoing_edges("parallel")
        merge_id = rt._find_merge_node(g, outgoing)
        assert merge_id == "merge"

    @pytest.mark.asyncio
    async def test_parallel_single_branch_fallback(self):
        # parallel node with 1 outgoing edge -> no fan-out, direct to target.
        resps = [_llm_response(content="only"), _llm_response(content="done")]
        rt, _ = _make_runtime(resps)
        g = AgentGraph(name="c5_single", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("parallel", NodeConfig(type="parallel", label="fanout"))
        g.add_node("llm1", NodeConfig(type="llm", label="only", model="test-model"))
        g.add_node("end", NodeConfig(type="end", label="end"))
        g.add_edge("start", "parallel")
        g.add_edge("parallel", "llm1")
        g.add_edge("llm1", "end")
        events = await _collect(rt, g, "run")
        results_ev = [e.content for e in events if e.type == AgentEventType.RESULT]
        assert any("single branch" in c for c in results_ev)
        contents = [e.content for e in events if e.type == AgentEventType.THINK]
        assert "only" in contents

    @pytest.mark.asyncio
    async def test_parallel_no_outgoing(self):
        # parallel node with no outgoing edges = terminal branch point.
        # graph valid: start + parallel only (no unreachable end node).
        resps = [_llm_response(content="x")]
        rt, _ = _make_runtime(resps)
        g = AgentGraph(name="c5_empty", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="start"))
        g.add_node("parallel", NodeConfig(type="parallel", label="fanout"))
        g.add_edge("start", "parallel")
        # no edge from parallel -> no branches.
        events = await _collect(rt, g, "run")
        results_ev = [e.content for e in events if e.type == AgentEventType.RESULT]
        assert any("no branches" in c for c in results_ev)

    @pytest.mark.asyncio
    async def test_parallel_plan_mode_fallback(self):
        # plan_mode active -> sequential fallback to first edge, no fan-out.
        resps = [_llm_response(content="first_only"), _llm_response(content="done")]
        rt, _ = _make_runtime(resps)
        g = _parallel_graph(num_branches=2)
        g.plan_mode = True  # graph-level flag, read at dispatch entry
        events = await _collect(rt, g, "run")
        results_ev = [e.content for e in events if e.type == AgentEventType.RESULT]
        assert any("plan_mode sequential fallback" in c for c in results_ev)
        # only first branch's llm runs (b0) + merge.
        contents = [e.content for e in events if e.type == AgentEventType.THINK]
        # b0 content "out0" NOT in (we scripted "first_only" for b0).
        assert "first_only" in contents

    @pytest.mark.asyncio
    async def test_parallel_merged_output_in_ctx(self):
        # merged branch outputs land in parent ctx as assistant message.
        resps = [
            _llm_response(content="alpha"),
            _llm_response(content="beta"),
            _llm_response(content="merged"),
        ]
        rt, _ = _make_runtime(resps)
        g = _parallel_graph(num_branches=2)
        # capture ctx via execute_graph with explicit context.
        from agent_runtime.context import AgentContext

        ctx = AgentContext()
        async for _e in rt.execute_graph(g, "run", ctx):
            pass
        assistant_msgs = [
            m for m in ctx.messages if isinstance(m, dict) and m.get("role") == "assistant"
        ]
        merged_text = " ".join(m.get("content", "") for m in assistant_msgs)
        assert "alpha" in merged_text
        assert "beta" in merged_text


class TestWorkflowPersistence:
    @pytest.fixture
    def store(self, tmp_path):
        return AgentStore(str(tmp_path / "wf_test.db"))

    @pytest.fixture
    def engine(self, store):
        return WorkflowEngine(store=store)

    def test_create_get_list_delete_persisted(self, engine, store):
        wf = engine.create_workflow(
            name="Persist WF",
            phases=[{"name": "p1", "pattern": "pipeline"}],
        )
        # fresh engine on same store sees it after restart.
        engine2 = WorkflowEngine(store=store)
        loaded = engine2.get_workflow(wf.id)
        assert loaded is not None
        assert loaded.name == "Persist WF"
        assert len(loaded.phases) == 1

        listed = engine2.list_workflows()
        assert any(w.name == "Persist WF" for w in listed)

        assert engine2.delete_workflow(wf.id) is True
        assert engine2.get_workflow(wf.id) is None

    @pytest.mark.asyncio
    async def test_run_persisted_survives_restart(self, engine, store):
        wf = engine.create_workflow(name="Run WF", phases=[])
        run = await engine.execute_workflow(wf.id, initial_input="hello")
        assert run.status == WorkflowStatus.COMPLETED

        engine2 = WorkflowEngine(store=store)
        restored = engine2.get_run_status(run.id)
        assert restored is not None
        assert restored.status == WorkflowStatus.COMPLETED
        assert restored.workflow_id == wf.id

    @pytest.mark.asyncio
    async def test_list_runs_after_restart(self, engine, store):
        wf = engine.create_workflow(name="ListRuns WF", phases=[])
        await engine.execute_workflow(wf.id, initial_input="a")
        await engine.execute_workflow(wf.id, initial_input="b")

        engine2 = WorkflowEngine(store=store)
        runs = engine2.list_runs(wf.id)
        assert len(runs) == 2
        all_runs = engine2.list_runs()
        assert len(all_runs) >= 2

    def test_memory_engine_no_store_backward_compat(self):
        # no store -> pure in-memory, all ops work (existing tests depend on this).
        eng = WorkflowEngine()
        wf = eng.create_workflow(name="Mem", phases=[])
        assert eng.get_workflow(wf.id) is not None
        assert eng.delete_workflow(wf.id) is True
        assert eng.get_run_status("nonexistent") is None
        assert eng.list_runs() == []

    def test_store_tables_created(self, store):
        # workflows + workflow_runs tables exist after init.
        import sqlite3

        conn = sqlite3.connect(str(store.db_path))
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        assert "workflows" in names
        assert "workflow_runs" in names
        conn.close()
