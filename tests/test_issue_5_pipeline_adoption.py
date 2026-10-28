"""#5 AC4 (fusion-harness): ToolPipeline is the single admission path.

Reproduction evidence for closing fusion-harness issue #5 end-to-end:
- every funnel admit runs the kernel ToolPipeline (spy proof)
- interactive approval is two-phase: the ask surfaces fail-closed from
  phase 1, the user approves mid-stream, phase 2 re-admits with the
  recorded decision — execution only on a pipeline allow
  (confirmation-policy escalation end-to-end through a tool node)
- non-interactive asks fail closed with the legacy needs_approval contract
- funnel-chain denies pass through with gate/reason preserved

Runner: pytest tests/test_issue_5_pipeline_adoption.py
"""

from __future__ import annotations

import agent_runtime.tool_pipeline as tool_pipeline_module
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


def _runtime() -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(WriteTool())
    registry.register(ReadTool())
    return AgentRuntime(MockMLXClient(), registry)


def _tool_graph(tool_name: str, policy: dict | None = None) -> AgentGraph:
    graph = AgentGraph(name="PipelineAdoptionGraph")
    graph.confirmation_policy = policy
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("t1", NodeConfig(type="tool", label="T1", tool_name=tool_name))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "t1")
    graph.add_edge("t1", "end")
    return graph


def _ask_verdict(reason: str = "L2 ask") -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.PREVIEW,
        reason=reason,
        requires_approval=True,
        metadata={"level": "L2"},
    )


def _block_verdict() -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.BLOCK,
        reason="dangerous pattern",
        requires_approval=False,
        metadata={"level": "L3"},
    )


class _StubGateway:
    def __init__(self, verdict):
        self._verdict = verdict

    def evaluate_action(self, **kwargs):
        return self._verdict


class TestPipelineIsSingleAdmissionPath:
    async def test_every_admit_runs_the_kernel_pipeline(self, monkeypatch):
        calls = []
        real_pipeline = tool_pipeline_module.ToolPipeline

        class SpyPipeline(real_pipeline):
            async def run(self, *a, **kw):
                calls.append(
                    {
                        "tool_name": kw.get("tool_name", ""),
                        "admission_only": kw.get("admission_only", False),
                        "has_approver": kw.get("approver") is not None,
                    }
                )
                return await super().run(*a, **kw)

        monkeypatch.setattr(tool_pipeline_module, "ToolPipeline", SpyPipeline)
        rt = _runtime()
        events = []
        async for event in rt.execute_graph(_tool_graph("list_dir"), "go"):
            events.append(event)
        assert calls, "tool node admission did not go through ToolPipeline"
        assert calls[0]["tool_name"] == "list_dir"
        assert calls[0]["admission_only"] is True
        assert calls[0]["has_approver"] is False, (
            "phase-1 ask fails closed; the approver only appears on the "
            "phase-2 re-admit after the user approves"
        )
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert results and results[0].name == "list_dir"

    async def test_non_interactive_admit_has_no_approver(self, monkeypatch):
        calls = []
        real_pipeline = tool_pipeline_module.ToolPipeline

        class SpyPipeline(real_pipeline):
            async def run(self, *a, **kw):
                calls.append(kw.get("approver") is not None)
                return await super().run(*a, **kw)

        monkeypatch.setattr(tool_pipeline_module, "ToolPipeline", SpyPipeline)
        rt = _runtime()
        await rt.tool_funnel.admit(ctx=AgentContext(), tool_name="list_dir", args={})
        assert calls == [False], "non-interactive admit must not wire an approver"


class TestTwoPhaseInteractiveApproval:
    async def test_escalation_approval_re_admits_with_recorded_decision(self):
        # #499 escalation -> pipeline "ask" (phase 1, fail-closed, no
        # responder) -> call-site interactive ask -> approve_action
        # resolves -> phase-2 re-admit with the recorded decision -> tool
        # executes only on the pipeline allow. No side door: execution
        # never happens between the two admits.
        rt = _runtime()
        graph = _tool_graph("file_write", {"tier": "always"})
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
            if event.metadata.get("action") == "pending_approval":
                assert rt.approve_action(event.metadata["action_id"]) is True
        actions = [
            e.metadata.get("action") for e in events if e.type == AgentEventType.SAFETY_APPROVAL
        ]
        assert "pending_approval" in actions
        assert "approved" in actions
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert results and results[0].name == "file_write"

    async def test_escalation_decline_blocks_execution(self):
        rt = _runtime()
        graph = _tool_graph("file_write", {"tier": "always"})
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
            if event.metadata.get("action") == "pending_approval":
                assert rt.reject_action(event.metadata["action_id"]) is True
        results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert not results, "declined approval must not execute the tool"
        assert any(e.type == AgentEventType.ERROR for e in events)


class TestLegacyContractsPreserved:
    async def test_non_interactive_ask_fails_closed_with_needs_approval(self):
        rt = _runtime()
        rt.tool_funnel.safety_gateway = _StubGateway(_ask_verdict())
        outcome = await rt.tool_funnel.admit(ctx=AgentContext(), tool_name="file_write", args={})
        assert not outcome.allowed
        assert outcome.needs_approval, (
            "parallel/register paths depend on the needs_approval degrade"
        )
        assert outcome.safety_result is not None

    async def test_interactive_ask_without_backend_keeps_needs_approval(self):
        # Permission-ask with safety_result=None: no verdict to drive (no
        # approval backend), fails closed inside the pipeline, and the
        # funnel maps it back to needs_approval for the legacy no-backend
        # error rendering.
        from fusion_harness import PermissionDecision

        class _AskEngine:
            def evaluate(self, *a, **kw):
                return PermissionDecision(decision="ask", reason="rule: ask")

        rt = _runtime()
        rt.tool_funnel.permissions = _AskEngine()
        ctx = AgentContext()
        outcome = await rt.tool_funnel.admit(
            ctx=ctx, tool_name="file_write", args={}, interactive=True
        )
        assert not outcome.allowed
        assert outcome.needs_approval, (
            "no approval backend wired must surface the legacy no-backend error"
        )

    async def test_safety_block_denies_with_gate_preserved(self):
        rt = _runtime()
        rt.tool_funnel.safety_gateway = _StubGateway(_block_verdict())
        outcome = await rt.tool_funnel.admit(ctx=AgentContext(), tool_name="file_write", args={})
        assert not outcome.allowed
        assert outcome.blocked_by == "safety"
        assert "dangerous pattern" in outcome.reason

    async def test_allowed_admit_returns_funnel_outcome(self):
        rt = _runtime()
        outcome = await rt.tool_funnel.admit(ctx=AgentContext(), tool_name="list_dir", args={})
        assert outcome.allowed
        assert not outcome.needs_approval


class TestConcurrentAdmits:
    async def test_shared_funnel_concurrent_admits_are_call_scoped(self):
        import asyncio

        rt = _runtime()
        ctx = AgentContext()
        outcomes = await asyncio.gather(
            rt.tool_funnel.admit(ctx=ctx, tool_name="list_dir", args={}),
            rt.tool_funnel.admit(ctx=ctx, tool_name="list_dir", args={}),
            rt.tool_funnel.admit(ctx=ctx, tool_name="list_dir", args={}),
        )
        assert all(o.allowed for o in outcomes)
