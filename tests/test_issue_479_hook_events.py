from dataclasses import dataclass, field
from pathlib import Path

import pytest

import agent_runtime.permissions as perms_mod
from agent_runtime.audit_logger import AuditLogger
from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.hooks import HookConfig, HookEngine
from agent_runtime.permissions import PermissionRule
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(perms_mod, "PERMISSIONS_FILE", tmp_path / "permissions.json")
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_PASSTHROUGH", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_A3", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)


class ProbeTool(BaseTool):
    name = "probe_tool"
    description = "records execution"
    parameters = {}
    readonly = True

    def __init__(self):
        self.executions = 0

    async def execute(self, **kwargs) -> str:
        self.executions += 1
        return "probe-ok"


def _runtime():
    rt = AgentRuntime(llm_gateway=None, tool_registry=ToolRegistry())
    return rt


def _simple_graph(name="g479"):
    graph = AgentGraph(name=name)
    graph.add_node("s", NodeConfig(type="start", label="s"))
    graph.add_node("e", NodeConfig(type="end", label="e"))
    graph.add_edge("s", "e")
    return graph


def _hook_recorder(rt, events):
    rt.hooks = HookEngine(auto_register_decorators=False)
    seen = []

    def recorder(payload):
        seen.append(payload)

    for event in events:
        rt.hooks.register(HookConfig(event=event, callback=recorder))
    return seen


class TestIssue479GraphNodeHooks:
    @pytest.mark.asyncio
    async def test_graph_before_after_fire(self):
        rt = _runtime()
        seen = _hook_recorder(rt, ["GRAPH_BEFORE", "GRAPH_AFTER"])
        events = []
        async for evt in rt.execute_graph(_simple_graph(), "hello"):
            events.append(evt)
        graph_events = [p for p in seen if "graph_name" in p]
        assert len(graph_events) == 2
        assert graph_events[0]["graph_name"] == "g479"
        assert "status" not in graph_events[0]
        assert graph_events[1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_node_before_after_fire_in_order(self):
        rt = _runtime()
        seen = _hook_recorder(rt, ["NODE_BEFORE", "NODE_AFTER"])
        async for _ in rt.execute_graph(_simple_graph("g479n"), "hello"):
            pass
        befores = [p["node_id"] for p in seen if "node_id" in p][:2]
        assert befores == ["s", "e"]
        node_afters = [p for p in seen if "node_type" in p and "iteration" in p]
        assert len(node_afters) == 4


class TestIssue479ToolDenyHook:
    @pytest.mark.asyncio
    async def test_tool_deny_fires_on_tool_node_deny(self):
        rt = _runtime()
        probe = ProbeTool()
        rt.tools.register(probe)
        rt.permission_engine.add_rule(PermissionRule(action="deny", tool="probe_tool"))
        seen = _hook_recorder(rt, ["TOOL_DENY"])
        graph = AgentGraph(name="g479d")
        graph.add_node("s", NodeConfig(type="start", label="s"))
        graph.add_node("t", NodeConfig(type="tool", label="t", tool_name="probe_tool"))
        graph.add_node("e", NodeConfig(type="end", label="e"))
        graph.add_edge("s", "t")
        graph.add_edge("t", "e")
        async for _ in rt.execute_graph(graph, "go"):
            pass
        assert probe.executions == 0
        denies = [p for p in seen if p.get("tool_name") == "probe_tool"]
        assert denies and denies[0]["gate"] == "permission"
        assert denies[0]["path"] == "tool_node"

    @pytest.mark.asyncio
    async def test_permission_ask_emits_lifecycle_event(self):
        rt = _runtime()
        probe = ProbeTool()
        rt.tools.register(probe)
        rt.permission_engine.add_rule(PermissionRule(action="ask", tool="probe_tool"))
        graph = AgentGraph(name="g479a")
        graph.add_node("s", NodeConfig(type="start", label="s"))
        graph.add_node("t", NodeConfig(type="tool", label="t", tool_name="probe_tool"))
        graph.add_node("e", NodeConfig(type="end", label="e"))
        graph.add_edge("s", "t")
        graph.add_edge("t", "e")
        async for _ in rt.execute_graph(graph, "go"):
            pass
        asks = [
            ev
            for ev in rt.tool_funnel.lifecycle.replay()
            if ev.event == "permission_ask"
        ]
        assert asks and asks[0].payload["tool_name"] == "probe_tool"
        assert probe.executions == 0


@dataclass
class _FakeSafetyResult:
    requires_approval: bool = True
    reason: str = "mutating action needs review"
    metadata: dict = field(default_factory=lambda: {"action_id": "", "level": "L2"})
    diff_preview: object = None


class TestIssue479ApprovalHooks:
    @pytest.mark.asyncio
    async def test_approval_request_then_resolve_approved(self):
        rt = _runtime()
        seen = _hook_recorder(rt, ["APPROVAL_REQUEST", "APPROVAL_RESOLVE"])
        ctx = AgentContext()
        gen = rt._await_safety_approval(
            ctx, _FakeSafetyResult(), "tool_call", "n1"
        )
        events = []
        async for evt in gen:
            events.append(evt)
            if evt.metadata and evt.metadata.get("action") == "pending_approval":
                rt.approve_action(evt.metadata["action_id"])
        requests = [p for p in seen if "decision" not in p]
        assert requests and requests[0]["category"] == "tool_call"
        resolves = [p for p in seen if "decision" in p]
        assert resolves and resolves[0]["decision"] == "approved"

    @pytest.mark.asyncio
    async def test_approval_resolve_rejected(self):
        rt = _runtime()
        seen = _hook_recorder(rt, ["APPROVAL_REQUEST", "APPROVAL_RESOLVE"])
        ctx = AgentContext()
        async for evt in rt._await_safety_approval(
            ctx, _FakeSafetyResult(), "tool_call", "n1"
        ):
            if evt.metadata and evt.metadata.get("action") == "pending_approval":
                rt.reject_action(evt.metadata["action_id"])
        resolves = [p for p in seen if "decision" in p]
        assert resolves and resolves[0]["decision"] == "rejected"


class TestIssue479FunnelAudit:
    @pytest.mark.asyncio
    async def test_funnel_deny_writes_audit_row(self, tmp_path):
        rt = _runtime()
        al = AuditLogger(db_path=str(tmp_path / "audit.db"))
        rt.set_audit_logger(al)
        rt.permission_engine.add_rule(PermissionRule(action="deny", tool="probe_tool"))
        outcome = await rt.tool_funnel.admit(
            ctx=AgentContext(), tool_name="probe_tool", args={}
        )
        assert not outcome.allowed
        rows = al.query_logs(action="funnel.deny.permission", resource_id="probe_tool")
        logs = rows.get("data", [])
        assert logs and logs[0]["result"] == "deny"
        assert logs[0]["actor_id"] == "runtime"
