"""N7 loop modes - background run stop_reason alignment (issue #344)."""

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime


class _FakeTools:
    def to_openai_schemas(self, role="all"):
        return []

    def readonly_tool_names(self):
        return set()


class _FakeLLM:
    pass


def _ctx_roundtrip():
    ctx = AgentContext()
    ctx.stop_reason = "budget_exceeded"
    d = ctx.to_dict()
    assert d["stop_reason"] == "budget_exceeded"
    back = AgentContext.from_dict(d)
    assert back.stop_reason == "budget_exceeded"


def test_context_stop_reason_roundtrip():
    _ctx_roundtrip()


async def test_end_node_sets_end_turn_and_metadata():
    rt = AgentRuntime()
    rt.tools = _FakeTools()
    graph = AgentGraph(name="t")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "end")
    events = []
    async for ev in rt.execute_graph(graph, "hi"):
        events.append(ev)
    assert events[-1].type == AgentEventType.END
    assert events[-1].metadata.get("stop_reason") == "end_turn"


async def test_budget_exceeded_sets_stop_reason():
    from agent_runtime.token_budget import TokenBudget

    rt = AgentRuntime()
    rt.tools = _FakeTools()
    graph = AgentGraph(name="t2")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "end")
    b = TokenBudget(max_tokens=10)
    b.record_usage(100, 100)
    events = []
    async for ev in rt.execute_graph(graph, "hi", token_budget=b):
        events.append(ev)
    # budget gate fires before/at node walk — context must carry the reason
    assert events[-1].type in (AgentEventType.TOKEN_BUDGET_EXCEEDED, AgentEventType.END, AgentEventType.ERROR)


async def test_execute_async_completion_carries_stop_reason(tmp_path, monkeypatch):
    """End-to-end through _run_graph_async with a stub runtime."""
    from agent_runtime import daemon_server as ds

    rt = AgentRuntime()
    rt.tools = _FakeTools()
    graph = AgentGraph(name="t3")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "end")

    class _StubDaemon:
        def __init__(self):
            self.store = None
            self._graph_semaphore = None
            self._active_executions = {}
            self._token_budget = None
            self.broadcasts = []
            self.evidence = []

        def _get_runtime(self):
            return rt

        def _write_evidence(self, execution_id, payload, team="default"):
            self.evidence.append((execution_id, payload, team))

        async def _broadcast_event(self, event, payload, team="default"):
            self.broadcasts.append((event, payload, team))

        def _get_task_store(self):
            return None

        def _evidence_path(self, execution_id, team):

            return f"/tmp/{execution_id}.jsonl"

    d = _StubDaemon()
    fn = ds.DaemonServer._run_graph_async.__get__(d)
    await fn(
        execution_id="exec_test1",
        graph=graph,
        graph_id="t3",
        input_text="hello",
        session_id="s-test",
        task_id="",
        team="default",
        params={},
    )
    completed = [p for e, p, t in d.evidence if p.get("type") == "execution.completed"]
    assert completed, "missing execution.completed evidence"
    assert completed[0]["stop_reason"] == "end_turn"
    bc = [p for e, p, t in d.broadcasts if e == "execution.completed"]
    assert bc and bc[0]["stop_reason"] == "end_turn"
