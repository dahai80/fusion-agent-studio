"""#353 N14: verify-node resident reviewer on the event stream."""


from agent_runtime.context import AgentContext, AgentEvent, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.resident_reviewer import (
    ResidentReviewer,
    build_resident_reviewer,
    graph_has_resident_verify,
)
from agent_runtime.runtime import AgentRuntime
from tools import ToolRegistry


def _graph(resident: bool) -> AgentGraph:
    g = AgentGraph(name="rv")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("llm1", NodeConfig(type="llm", label="llm1"))
    g.add_node("tool1", NodeConfig(type="tool", label="tool1", tool_name="echo"))
    g.add_node(
        "verify1",
        NodeConfig(type="verify", label="verify1", tool_params={"resident": resident}),
    )
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "llm1")
    g.add_edge("llm1", "tool1")
    g.add_edge("tool1", "verify1")
    g.add_edge("verify1", "end")
    return g


def _ev(etype: AgentEventType, content: str = "", node_id: str = "") -> AgentEvent:
    return AgentEvent(type=etype, content=content, node_id=node_id)


def test_graph_has_resident_verify():
    assert graph_has_resident_verify(_graph(True)) is True
    assert graph_has_resident_verify(_graph(False)) is False


def test_transition_mismatch_flagged():
    r = build_resident_reviewer(_graph(True))
    r.observe(_ev(AgentEventType.START, node_id="llm1"))
    # llm1's only declared out-edge is tool1 — jumping to end is undeclared
    findings = r.observe(_ev(AgentEventType.RESULT, node_id="end"))
    kinds = [f.kind for f in findings]
    assert "graph_mismatch" in kinds
    assert findings[0].detail.startswith("transition 'llm1'")


def test_unknown_node_flagged():
    r = build_resident_reviewer(_graph(True))
    r.observe(_ev(AgentEventType.START, node_id="start"))
    findings = r.observe(_ev(AgentEventType.THINK, node_id="ghost"))
    assert findings[0].kind == "graph_mismatch"
    assert "not present in graph" in findings[0].detail


def test_untraceable_conclusion_flagged():
    r = build_resident_reviewer(_graph(True))
    r.observe(_ev(AgentEventType.START, node_id="start"))
    r.observe(_ev(AgentEventType.TOOL_RESULT, "ls -la /tmp showed files aplenty", node_id="tool1"))
    findings = r.observe(
        _ev(AgentEventType.THINK, "Therefore the zephyr quintessence holds", node_id="tool1")
    )
    kinds = [f.kind for f in findings]
    assert "untraceable_conclusion" in kinds


def test_traceable_conclusion_not_flagged():
    r = build_resident_reviewer(_graph(True))
    r.observe(_ev(AgentEventType.START, node_id="start"))
    r.observe(
        _ev(AgentEventType.TOOL_RESULT, "deployment finished: build 42 artifacts uploaded", "tool1")
    )
    findings = r.observe(
        _ev(AgentEventType.THINK, "Therefore deployment build 42 finished", "tool1")
    )
    assert [f for f in findings if f.kind == "untraceable_conclusion"] == []


def test_no_evidence_no_false_positive():
    r = build_resident_reviewer(_graph(True))
    r.observe(_ev(AgentEventType.START, node_id="start"))
    findings = r.observe(
        _ev(AgentEventType.THINK, "Therefore nothing to compare against yet", "llm1")
    )
    assert findings == []


def test_summary_shape():
    r = ResidentReviewer(_graph(True))
    s = r.summary()
    assert s["resident"] is True
    assert s["counts"] == {"graph_mismatch": 0, "untraceable_conclusion": 0}


async def test_runtime_resident_emits_findings(monkeypatch):
    rt = AgentRuntime(tool_registry=ToolRegistry())
    g = _graph(True)

    async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
        yield _ev(AgentEventType.START, node_id="llm1")
        yield _ev(AgentEventType.RESULT, node_id="end")

    monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
    findings = []
    async for ev in rt.execute_graph(g, "run"):
        if ev.name == "resident_reviewer" and "[resident]" in ev.content:
            findings.append(ev)
    assert len(findings) == 1
    assert findings[0].metadata["kind"] == "graph_mismatch"
    assert findings[0].metadata["resident"] is True


async def test_runtime_non_resident_no_reviewer_events(monkeypatch):
    rt = AgentRuntime(tool_registry=ToolRegistry())
    g = _graph(False)

    async def fake_inner(graph, initial_input, ctx, token_budget, stream=False, max_iterations=None):
        yield _ev(AgentEventType.START, node_id="llm1")
        yield _ev(AgentEventType.RESULT, node_id="end")

    monkeypatch.setattr(rt, "_execute_graph_inner", fake_inner)
    async for ev in rt.execute_graph(g, "run"):
        assert ev.name != "resident_reviewer"


async def test_resident_verify_node_announces_and_skips_oneshot(monkeypatch):
    from agent_runtime import verifier as verifier_mod

    called = {"n": 0}

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def verify(self, *a, **k):
            called["n"] += 1
            raise AssertionError("one-shot verify must not run in resident mode")

    monkeypatch.setattr(verifier_mod, "VerificationEngine", _Boom)
    rt = AgentRuntime(tool_registry=ToolRegistry())
    g = AgentGraph(name="rv-resident")
    g.add_node("start", NodeConfig(type="start", label="start"))
    g.add_node("verify1", NodeConfig(type="verify", label="verify1", tool_params={"resident": True}))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "verify1")
    g.add_edge("verify1", "end")
    saw_announce = False
    async for ev in rt.execute_graph(g, "run"):
        if ev.name == "resident_reviewer" and ev.metadata.get("resident") and "[resident]" not in ev.content:
            saw_announce = True
    assert saw_announce
    assert called["n"] == 0


async def test_review_findings_variables_set():
    rt = AgentRuntime(tool_registry=ToolRegistry())
    g = _graph(True)
    ctx = AgentContext()
    async for _ in rt.execute_graph(g, "run", context=ctx):
        pass
    assert ctx.variables.get("review_findings") is not None
    assert "graph_mismatch" in ctx.variables.get("review_counts", {})
