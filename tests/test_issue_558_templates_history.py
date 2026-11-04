"""#558: template library + run history archive/diff.

ACs:
1. FlowStore v1→v2 migration (kind column) preserves existing rows.
2. 7 plan §7.3 builtin templates register + validate clean.
3. template.save/list/instantiate/delete RPC roundtrip, tenant-scoped.
4. RunHistoryStore: save/list/load, event cap, diff, lazy daemon store.
5. Managed-run archive hook wired into _execute_managed_run finally block.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.flow_store import FlowStore
from agent_runtime.graph import AgentGraph
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.run_history import MAX_EVENT_FRAMES, RunHistoryStore
from agent_runtime.templates import (
    TemplateManager,
    _plan_template_graphs,
    register_plan_templates,
)
from server.fusion_mlx_client import LLMResponse
from tools.registry import ToolRegistry


class MockClient:
    async def chat(self, model, messages, tools=None, **kwargs):
        return LLMResponse(
            content="ok",
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )

    async def chat_stream(self, messages, model="", **kwargs):
        raise NotImplementedError

    async def embeddings(self, model, input, **kwargs):
        return [[0.1] * 8]


def _gateway() -> LLMGateway:
    gw = LLMGateway()
    client = MockClient()
    gw.set_default_client(client)
    for name in ("m", "qwen3.5-9b"):
        gw.register_model(ModelConfig(name=name, provider="local", context_length=1024))
    return gw


def _graph_dict(name="g") -> dict:
    return {
        "name": name,
        "nodes": [
            {"id": "s", "type": "start", "label": "s"},
            {"id": "e", "type": "end", "label": "e"},
        ],
        "edges": [{"source_id": "s", "target_id": "e"}],
        "start_node_id": "s",
    }


def _frames(*pairs) -> list[dict]:
    return [{"type": t, "content": c, "node_id": n} for t, c, n in pairs]


# ---------------- FlowStore kind + migration ----------------


def test_v1_database_migrates_kind_column(tmp_path):
    db = tmp_path / "flows.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE flows (
            flow_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            graph_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO flows (flow_id, tenant_id, name, description, graph_json, version, created_at, updated_at) VALUES ('legacy', '', 'old', '', ?, 1, 0, 0)",
        (json.dumps(_graph_dict("old")),),
    )
    conn.commit()
    conn.close()
    s = FlowStore(db_path=db)
    try:
        rows = s.list_flows()
        assert len(rows) == 1 and rows[0]["flow_id"] == "legacy"
        out = s.save_flow(_graph_dict("new"), name="new")
        assert out["created"] is True
        assert s.list_flows(kind="template") == []
        assert len(s.list_flows()) == 2
    finally:
        s.close()


def test_kind_filter_and_default(store_tmp=None):
    s = FlowStore(db_path=":memory:")
    try:
        s.save_flow(_graph_dict("f"), name="flow-one", kind="flow")
        s.save_flow(_graph_dict("t"), name="tpl-one", kind="template")
        assert [f["name"] for f in s.list_flows()] == ["flow-one"]
        assert [f["name"] for f in s.list_flows(kind="template")] == ["tpl-one"]
        assert len(s.list_flows(kind="")) == 2
        for row in s.list_flows(kind=""):
            assert row["kind"] in ("flow", "template")
    finally:
        s.close()


# ---------------- builtin templates ----------------


def test_plan_templates_register_and_validate():
    n = register_plan_templates()
    assert n == 7
    assert TemplateManager.count() >= 7
    for data in _plan_template_graphs():
        g = AgentGraph.from_dict(data)
        assert g.validate() == [], (data["name"], g.validate())
    names = {t["name"] for t in TemplateManager.list()}
    assert {
        "basic-llm-prompt",
        "rag-qa",
        "agent-with-tools",
        "supervisor-worker",
        "human-approval-flow",
        "document-pipeline",
        "iteration-map",
    } <= names


def test_template_list_rpc_reports_builtin_source():
    from agent_runtime.dispatchers.flow import FlowDispatcher

    class _Daemon:
        pass

    d = _Daemon()
    fd = FlowDispatcher(d)
    d._flow_store = None

    async def run():
        return await fd._handle_template_list({})

    out = asyncio.run(run())
    src = {t["name"]: t["source"] for t in out["templates"]}
    assert src["basic-llm-prompt"] == "builtin"
    assert out["count"] >= 7


@pytest.mark.asyncio
async def test_basic_llm_prompt_template_runs_to_end():
    from agent_runtime.runtime import AgentRuntime

    register_plan_templates()
    g = TemplateManager.get("basic-llm-prompt")
    rt = AgentRuntime(llm_gateway=_gateway(), tool_registry=ToolRegistry())
    ctx = AgentContext(session_id="s-tpl-basic")
    events = []
    async for ev in rt.execute_graph(g, "hello", context=ctx):
        events.append(ev)
    types = [ev.type for ev in events]
    assert AgentEventType.END in types
    assert not [ev for ev in events if ev.type == AgentEventType.ERROR]
    thinks = [ev for ev in events if ev.type == AgentEventType.THINK and ev.content]
    assert thinks and thinks[-1].content == "ok"


@pytest.mark.asyncio
async def test_iteration_map_template_runs():
    from agent_runtime.runtime import AgentRuntime

    register_plan_templates()
    g = TemplateManager.get("iteration-map")
    rt = AgentRuntime(llm_gateway=_gateway(), tool_registry=ToolRegistry())
    ctx = AgentContext(session_id="s-tpl-iter")
    rt.variables.load_from({"items": ["a", "b", "c"]})
    events = []
    async for ev in rt.execute_graph(g, "", context=ctx):
        events.append(ev)
    types = [ev.type for ev in events]
    assert AgentEventType.END in types
    assert not [ev for ev in events if ev.type == AgentEventType.ERROR]
    assert ctx.variables.get("results") == ["ok", "ok", "ok"]
    assert ctx.variables.get("final") == ["ok", "ok", "ok"]


@pytest.mark.asyncio
async def test_human_approval_template_pauses():
    from agent_runtime.runtime import AgentRuntime

    register_plan_templates()
    g = TemplateManager.get("human-approval-flow")
    g.get_node("approve").tool_params["timeout_seconds"] = 1
    rt = AgentRuntime(llm_gateway=_gateway(), tool_registry=ToolRegistry())
    ctx = AgentContext(session_id="s-tpl-hitl")
    events = []

    async def run():
        async for ev in rt.execute_graph(g, "draft me", context=ctx):
            events.append(ev)

    await asyncio.wait_for(run(), timeout=10)
    kinds = [ev.type for ev in events]
    assert AgentEventType.SAFETY_APPROVAL in kinds
    assert AgentEventType.CHECKPOINT in kinds


# ---------------- template.* RPC roundtrip ----------------


@pytest.fixture
def daemon(tmp_path):
    import tempfile

    from agent_runtime.daemon_server import DaemonServer

    async def _make():
        short = tempfile.mkdtemp(prefix="fas558-")
        d = DaemonServer(
            socket_path=str(Path(short) / "s.sock"),
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "store.db"),
        )
        await d.start()
        d._flow_store = FlowStore(db_path=tmp_path / "flows.db")
        return d

    loop = asyncio.new_event_loop()
    d = loop.run_until_complete(_make())

    async def rpc(method, params):
        resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in resp:
            return {"error": resp["error"]}
        return resp.get("result", {})

    d.rpc = rpc
    yield d
    loop.run_until_complete(d.stop())
    loop.close()


@pytest.mark.asyncio
async def test_template_save_instantiate_delete_roundtrip(daemon):
    rpc = daemon.rpc
    lst = await rpc("flow.template_list", {})
    assert lst["count"] >= 7
    saved = await rpc(
        "flow.template_save",
        {"name": "my-tpl", "description": "d", "graph": _graph_dict("my-tpl")},
    )
    assert "error" not in saved and saved["kind"] == "template"
    fid = saved["flow_id"]
    lst2 = await rpc("flow.template_list", {})
    mine = [t for t in lst2["templates"] if t.get("flow_id") == fid]
    assert mine and mine[0]["source"] == "tenant"
    inst = await rpc("flow.template_instantiate", {"flow_id": fid})
    assert "error" not in inst
    g = AgentGraph.from_dict(inst["graph"])
    assert g.id != fid and g.name == "my-tpl-instance"
    assert g.validate() == []
    inst2 = await rpc("flow.template_instantiate", {"name": "basic-llm-prompt"})
    assert "error" not in inst2
    assert inst2["graph_id"] != inst["graph_id"]
    deleted = await rpc("flow.template_delete", {"flow_id": fid})
    assert deleted["deleted"] is True
    again = await rpc("flow.template_instantiate", {"flow_id": fid})
    assert "error" in again


@pytest.mark.asyncio
async def test_template_tenant_isolation(daemon, monkeypatch):
    import agent_runtime.dispatchers.flow as flow_mod

    monkeypatch.setattr(flow_mod, "resolve_tenant_id", lambda: "tenant-a")
    saved = await daemon.rpc(
        "flow.template_save", {"name": "a-tpl", "graph": _graph_dict("a-tpl")}
    )
    assert "error" not in saved
    monkeypatch.setattr(flow_mod, "resolve_tenant_id", lambda: "tenant-b")
    lst = await daemon.rpc("flow.template_list", {})
    assert not [t for t in lst["templates"] if t.get("flow_id") == saved["flow_id"]]
    inst = await daemon.rpc("flow.template_instantiate", {"flow_id": saved["flow_id"]})
    assert "error" in inst


@pytest.mark.asyncio
async def test_template_save_requires_name(daemon):
    out = await daemon.rpc("flow.template_save", {"graph": _graph_dict()})
    assert "error" in out


# ---------------- run history store ----------------


def _hist(tmp_path) -> RunHistoryStore:
    return RunHistoryStore(db_path=tmp_path / "run_history.db")


def test_summarize_terminal_status():
    from agent_runtime.run_history import _summarize

    frames = _frames(("start", "", "s"), ("result", "ok", "llm"), ("end", "", "e"))
    status, nodes = _summarize(frames)
    assert status == "completed"
    assert nodes == {"s": "done", "llm": "done", "e": "done"}
    status, nodes = _summarize(_frames(("start", "", "s"), ("error", "boom", "llm")))
    assert status == "failed" and nodes["llm"] == "error"
    status, _ = _summarize(_frames(("start", "", "s"), ("error", "run canceled by user", "llm")))
    assert status == "cancelled"


def test_save_list_load_roundtrip(tmp_path):
    h = _hist(tmp_path)
    try:
        h.save_run("r1", "g1", "Graph One", _frames(("start", "", "s"), ("end", "", "e")), started_at=1.0)
        h.save_run("r2", "g2", "Graph Two", _frames(("error", "boom", "llm")), started_at=2.0)
        runs = h.list_runs()
        assert [r["run_id"] for r in runs] == ["r2", "r1"]
        assert runs[0]["status"] == "failed"
        g1 = h.list_runs(graph_id="g1")
        assert [r["run_id"] for r in g1] == ["r1"]
        run = h.load_run("r1")
        assert run["graph_name"] == "Graph One"
        assert [e["type"] for e in run["events"]] == ["start", "end"]
        assert h.load_run("missing") is None
    finally:
        h.close()


def test_event_frame_cap(tmp_path):
    h = _hist(tmp_path)
    try:
        frames = [{"type": "token", "content": str(i), "node_id": "n"} for i in range(700)]
        out = h.save_run("big", "g", "G", frames)
        assert out["frames"] == MAX_EVENT_FRAMES == 500
        run = h.load_run("big")
        assert len(run["events"]) == 500
    finally:
        h.close()


def test_diff_runs(tmp_path):
    h = _hist(tmp_path)
    try:
        h.save_run("ra", "g", "G", _frames(("start", "", "s"), ("result", "ok", "n2"), ("end", "", "e")))
        h.save_run("rb", "g", "G", _frames(("start", "", "s"), ("error", "boom", "n2"), ("end", "", "e")))
        diff = h.diff_runs("ra", "rb")
        assert diff["changed_count"] == 1
        row = [r for r in diff["nodes"] if r["node_id"] == "n2"][0]
        assert row["a"] == "done" and row["b"] == "error" and row["changed"] is True
        assert h.diff_runs("ra", "missing") is None
    finally:
        h.close()


@pytest.mark.asyncio
async def test_history_dispatcher_roundtrip(tmp_path, monkeypatch):
    from agent_runtime.daemon_server import DaemonServer
    from agent_runtime.run_history import RunHistoryStore as RHS

    real_init = RHS.__init__
    created = []

    def spy_init(self, db_path=""):
        created.append(self)
        real_init(self, db_path=db_path or tmp_path / "spy.db")

    monkeypatch.setattr(RHS, "__init__", spy_init)
    import tempfile

    short = tempfile.mkdtemp(prefix="fas558h-")
    d = DaemonServer(
        socket_path=str(Path(short) / "s.sock"),
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "store.db"),
    )
    try:
        await d.start()

        async def rpc(method, params):
            resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if "error" in resp:
                return {"error": resp["error"]}
            return resp.get("result", {})

        assert not created, "store must be lazy"
        out = await rpc("run.history_list", {})
        assert out["runs"] == []
        assert len(created) == 1, "first call lazily creates the store"
        created[0].save_run("r9", "g9", "G9", _frames(("end", "", "e")))
        got = await rpc("run.history_get", {"run_id": "r9"})
        assert got["run"]["graph_name"] == "G9"
        missing = await rpc("run.history_get", {"run_id": "nope"})
        assert "error" in missing
        created[0].save_run("r8", "g9", "G9", _frames(("error", "x", "e")))
        diff = await rpc("run.diff", {"run_a": "r9", "run_b": "r8"})
        assert diff["diff"]["changed_count"] == 1
    finally:
        await d.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
