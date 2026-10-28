"""#499: Confirmation policy tiers — AlwaysConfirm / NeverConfirm / ConfirmRisk.

OpenHands-parity policy layered on the safety approval flow:
- tier "always": every gated action asks (escalates auto-passed ones)
- tier "never":  nothing asks — pending verdicts auto-approve
- tier "risk":   deterministic risk score vs risk_threshold decides
- per-tool overrides; policy decision visible in event metadata
- graph-level config: AgentGraph.confirmation_policy -> ctx.metadata

Runner: pytest tests/test_issue_499_confirmation_policy.py
"""

from __future__ import annotations

import pytest

from agent_runtime.confirmation_policy import (
    ConfirmationPolicy,
    policy_from_ctx,
    score_tool_risk,
)
from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.safety import SafetyAction, SafetyVerdict
from tools.base import BaseTool
from tools.registry import ToolRegistry


class WriteTool(BaseTool):
    name = "file_write"
    description = "writes a file (risk-band: write)"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "written"


class ReadTool(BaseTool):
    name = "list_dir"
    description = "readonly listing"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "[]"


class MockMLXClient:
    async def chat(self, *a, **k):
        return None


def _ask_verdict(reason: str = "L2 ask") -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.PREVIEW,
        reason=reason,
        requires_approval=True,
        metadata={"level": "L2"},
    )


def _pass_verdict() -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.ALLOW,
        reason="auto-approved",
        requires_approval=False,
        metadata={},
    )


class TestPolicyObject:
    def test_invalid_tier_rejected(self):
        with pytest.raises(ValueError):
            ConfirmationPolicy(tier="sometimes")

    def test_invalid_override_tier_rejected(self):
        with pytest.raises(ValueError):
            ConfirmationPolicy(tier="risk", tool_overrides={"file_write": "maybe"})

    def test_roundtrip(self):
        policy = ConfirmationPolicy(
            tier="risk",
            risk_threshold=0.4,
            tool_overrides={"file_write": "always"},
        )
        assert ConfirmationPolicy.from_dict(policy.to_dict()) == policy

    def test_from_dict_none_on_empty_or_bad(self):
        assert ConfirmationPolicy.from_dict(None) is None
        assert ConfirmationPolicy.from_dict({}) is None
        assert ConfirmationPolicy.from_dict("nope") is None
        assert ConfirmationPolicy.from_dict({"tier": "bogus"}) is None

    def test_effective_tier_override_wins(self):
        policy = ConfirmationPolicy(tier="risk", tool_overrides={"file_write": "never"})
        assert policy.effective_tier("file_write") == "never"
        assert policy.effective_tier("other") == "risk"

    def test_decide_always(self):
        policy = ConfirmationPolicy(tier="always")
        assert policy.decide(False, 0.0, "list_dir")[0] == "confirm"

    def test_decide_never(self):
        policy = ConfirmationPolicy(tier="never")
        assert policy.decide(True, 0.9, "file_write")[0] == "auto"

    def test_decide_risk_threshold(self):
        policy = ConfirmationPolicy(tier="risk", risk_threshold=0.5)
        assert policy.decide(True, 0.3, "file_write")[0] == "auto"
        assert policy.decide(True, 0.6, "file_write")[0] == "confirm"
        # at-threshold confirms (>=)
        assert policy.decide(True, 0.5, "file_write")[0] == "confirm"


class TestRiskScorer:
    def test_destructive_command_scores_highest(self):
        assert score_tool_risk("terminal", {"command": "rm -rf /tmp/x"}) == 0.9

    def test_destructive_text_in_benign_tool(self):
        assert score_tool_risk("list_dir", {"path": "x; drop table users"}) == 0.9

    def test_network_scores_above_write(self):
        assert score_tool_risk("http_request", {"url": "https://example.com"}) == 0.6

    def test_write_tool_name_band(self):
        assert score_tool_risk("file_write", {"path": "/tmp/a"}) == 0.3

    def test_readonly_zero(self):
        assert score_tool_risk("list_dir", {}, readonly=True) == 0.0

    def test_readonly_false_floor(self):
        assert score_tool_risk("list_dir", {}, readonly=False) == 0.3

    def test_unknown_neutral(self):
        assert score_tool_risk("list_dir", {}) == 0.0


class TestGraphField:
    def test_roundtrip(self):
        graph = AgentGraph(name="P")
        graph.confirmation_policy = {"tier": "risk", "risk_threshold": 0.3}
        graph.add_node("start", NodeConfig(type="start"))
        d = graph.to_dict()
        assert d["confirmation_policy"] == {"tier": "risk", "risk_threshold": 0.3}
        restored = AgentGraph.from_dict(d)
        assert restored.confirmation_policy == {"tier": "risk", "risk_threshold": 0.3}

    def test_default_none(self):
        graph = AgentGraph.from_dict({"name": "x"})
        assert graph.confirmation_policy is None

    def test_downstream_subgraph_propagates(self):
        graph = AgentGraph(name="P")
        graph.confirmation_policy = {"tier": "never"}
        graph.add_node("start", NodeConfig(type="start"))
        graph.add_node("end", NodeConfig(type="end"))
        graph.add_edge("start", "end")
        sub = graph.downstream_subgraph("end")
        assert sub.confirmation_policy == {"tier": "never"}


# === Part 2: chokepoint + graph execution ===


def _runtime() -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(WriteTool())
    registry.register(ReadTool())
    return AgentRuntime(MockMLXClient(), registry)


def _ctx(policy: dict | None = None) -> AgentContext:
    ctx = AgentContext()
    if policy:
        ctx.metadata["confirmation_policy"] = policy
    return ctx


async def _drain(rt: AgentRuntime, ctx: AgentContext, verdict: SafetyVerdict, **kw):
    events = []
    async for e in rt._await_safety_approval(ctx, verdict, "tool_call", "n1", **kw):
        events.append(e)
        if e.metadata.get("action") == "pending_approval":
            # approve_action resolves the future synchronously
            assert rt.approve_action(e.metadata["action_id"]) is True
    return events


class TestPolicyFromCtx:
    def test_none_without_policy(self):
        assert policy_from_ctx(AgentContext()) is None

    def test_reads_metadata(self):
        ctx = _ctx({"tier": "never"})
        assert policy_from_ctx(ctx).tier == "never"


class TestChokepointTiers:
    async def test_never_auto_approves_ask_verdict(self):
        rt = _runtime()
        ctx = _ctx({"tier": "never"})
        events = await _drain(rt, ctx, _ask_verdict(), tool_name="file_write")
        assert events[0].metadata["action"] == "approved"
        assert events[0].metadata["confirmation_policy"]["tier"] == "never"
        assert ctx.error == ""

    async def test_risk_below_threshold_auto_approves(self):
        rt = _runtime()
        ctx = _ctx({"tier": "risk", "risk_threshold": 0.5})
        events = await _drain(
            rt, ctx, _ask_verdict(), tool_name="file_write", params={"path": "/tmp/a"}
        )
        assert events[0].metadata["action"] == "approved"
        assert events[0].metadata["confirmation_policy"]["risk_score"] == 0.3

    async def test_risk_at_threshold_confirms(self):
        rt = _runtime()
        ctx = _ctx({"tier": "risk", "risk_threshold": 0.3})
        events = await _drain(
            rt, ctx, _ask_verdict(), tool_name="file_write", params={"path": "/tmp/a"}
        )
        actions = [e.metadata.get("action") for e in events]
        assert actions == ["pending_approval", "approved"]
        assert events[0].metadata["confirmation_policy"]["tier"] == "risk"
        assert events[0].metadata["confirmation_policy"]["risk_score"] == 0.3

    async def test_always_escalates_auto_passed_verdict(self):
        rt = _runtime()
        ctx = _ctx({"tier": "always"})
        events = await _drain(
            rt, ctx, _pass_verdict(), tool_name="file_write", params={"path": "/tmp/a"}
        )
        actions = [e.metadata.get("action") for e in events]
        assert actions == ["pending_approval", "approved"]
        assert "confirmation policy" in events[0].content

    async def test_tool_override_never_wins_over_graph_risk(self):
        rt = _runtime()
        ctx = _ctx(
            {"tier": "risk", "risk_threshold": 0.3, "tool_overrides": {"file_write": "never"}}
        )
        events = await _drain(
            rt, ctx, _ask_verdict(), tool_name="file_write", params={"path": "/tmp/a"}
        )
        assert events[0].metadata["action"] == "approved"
        assert events[0].metadata["confirmation_policy"]["tier"] == "never"

    async def test_no_policy_unchanged_behavior(self):
        rt = _runtime()
        ctx = _ctx()
        events = await _drain(rt, ctx, _ask_verdict(), tool_name="file_write")
        actions = [e.metadata.get("action") for e in events]
        assert actions == ["pending_approval", "approved"]
        assert all("confirmation_policy" not in e.metadata for e in events)

    async def test_destructive_params_confirm_even_above_tool_band(self):
        rt = _runtime()
        ctx = _ctx({"tier": "risk", "risk_threshold": 0.5})
        events = await _drain(
            rt,
            ctx,
            _pass_verdict(),
            tool_name="terminal",
            params={"command": "rm -rf /tmp/scratch"},
        )
        actions = [e.metadata.get("action") for e in events]
        assert actions == ["pending_approval", "approved"]
        assert events[0].metadata["confirmation_policy"]["risk_score"] == 0.9


def _tool_graph(tool_name: str, policy: dict | None) -> AgentGraph:
    graph = AgentGraph(name="ConfirmPolicyGraph")
    graph.confirmation_policy = policy
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("t1", NodeConfig(type="tool", label="T1", tool_name=tool_name))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "t1")
    graph.add_edge("t1", "end")
    return graph


class TestGraphExecution:
    async def test_risk_threshold_escalates_write_tool_node(self):
        rt = _runtime()
        graph = _tool_graph("file_write", {"tier": "risk", "risk_threshold": 0.2})
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
            if event.metadata.get("action") == "pending_approval":
                assert rt.approve_action(event.metadata["action_id"]) is True
        actions = [
            e.metadata.get("action")
            for e in events
            if e.type == AgentEventType.SAFETY_APPROVAL
        ]
        assert "pending_approval" in actions
        pending = next(
            e for e in events if e.metadata.get("action") == "pending_approval"
        )
        assert pending.metadata["confirmation_policy"]["tier"] == "risk"
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert results and results[0].name == "file_write"

    async def test_risk_threshold_passes_readonly_tool_node(self):
        rt = _runtime()
        graph = _tool_graph("list_dir", {"tier": "risk", "risk_threshold": 0.5})
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
        assert not [
            e for e in events if e.metadata.get("action") == "pending_approval"
        ]
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert results and results[0].name == "list_dir"

    async def test_no_policy_graph_unchanged(self):
        rt = _runtime()
        graph = _tool_graph("file_write", None)
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
        assert not [
            e for e in events if e.metadata.get("action") == "pending_approval"
        ]
        assert [e for e in events if e.type == AgentEventType.TOOL_RESULT]

