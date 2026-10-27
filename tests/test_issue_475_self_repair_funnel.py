import json
import re
from pathlib import Path

import pytest

import agent_runtime.permissions as perms_mod
from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.llm_gateway import GatewayResponse
from agent_runtime.permissions import PermissionRule
from agent_runtime.runtime import AgentRuntime
from agent_runtime.token_budget import TokenBudget
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


class BoomTool(BaseTool):
    name = "boom_tool"
    description = "always fails"
    parameters = {}
    readonly = True

    async def execute(self, **kwargs) -> str:
        raise RuntimeError("boom")


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


def _tool_call(name, args=None, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


class _SeqGateway:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, **kwargs):
        return self.responses.pop(0)


def _resp(tool_calls, prompt=10, completion=10):
    return GatewayResponse(
        content="",
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        usage={"prompt_tokens": prompt, "completion_tokens": completion},
        model="fake",
    )


def _self_repair_setup(deny_probe=False, probe_usage=None):
    rt = AgentRuntime(llm_gateway=None, tool_registry=ToolRegistry())
    boom = BoomTool()
    probe = ProbeTool()
    rt.tools.register(boom)
    rt.tools.register(probe)
    rt.permission_engine.add_rule(PermissionRule(action="allow", tool="boom_tool"))
    if deny_probe:
        rt.permission_engine.add_rule(PermissionRule(action="deny", tool="probe_tool"))
    else:
        rt.permission_engine.add_rule(PermissionRule(action="allow", tool="probe_tool"))
    retry_resp = _resp(
        [_tool_call("probe_tool", call_id="call_2")], **(probe_usage or {})
    )
    rt.llm_gateway = _SeqGateway([_resp([_tool_call("boom_tool")]), retry_resp])
    graph = AgentGraph(name="g475")
    node = NodeConfig(
        type="llm", label="n1", retry_on_error=True, max_retries=1
    )
    graph.add_node("s", NodeConfig(type="start", label="s"))
    graph.add_node("n1", node)
    graph.add_node("e", NodeConfig(type="end", label="e"))
    graph.add_edge("s", "n1")
    graph.add_edge("n1", "e")
    return rt, graph, node, probe


class TestIssue475SelfRepairFunnel:
    @pytest.mark.asyncio
    async def test_self_repair_allowed_tool_executes(self):
        rt, graph, node, probe = _self_repair_setup()
        ctx = AgentContext()
        events = []
        async for evt in rt._execute_llm_node(
            ctx, node, graph, "fake-model", [rt.tools.get("boom_tool").openai_schema()], ""
        ):
            events.append(evt)
        assert probe.executions == 1
        assert any(
            e.type.value == "retry_success"
            for e in events
            if hasattr(e.type, "value")
        ) or "Self-repair succeeded" in [getattr(e, "content", "") for e in events]

    @pytest.mark.asyncio
    async def test_self_repair_deny_rule_blocks_tool(self, caplog):
        rt, graph, node, probe = _self_repair_setup(deny_probe=True)
        ctx = AgentContext()
        tool_results = []
        async for evt in rt._execute_llm_node(
            ctx, node, graph, "fake-model", [rt.tools.get("boom_tool").openai_schema()], ""
        ):
            if evt.type.value == "tool_result" and evt.name == "probe_tool":
                tool_results.append(evt.content)
        assert probe.executions == 0
        assert tool_results and "Permission denied tool call" in tool_results[0]
        assert any(
            "funnel blocked self_repair" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_self_repair_pre_tool_use_hook_fires(self):
        rt, graph, node, probe = _self_repair_setup()
        seen = []
        from agent_runtime.hooks import HookConfig, HookEngine

        def recorder(payload):
            seen.append(payload)
            return None

        rt.hooks = HookEngine(auto_register_decorators=False)
        rt.hooks.register(HookConfig(event="PRE_TOOL_USE", callback=recorder))
        ctx = AgentContext()
        async for _ in rt._execute_llm_node(
            ctx, node, graph, "fake-model", [rt.tools.get("boom_tool").openai_schema()], ""
        ):
            pass
        probe_fires = [p for p in seen if p.get("tool_name") == "probe_tool"]
        assert probe_fires, "PRE_TOOL_USE hook did not fire for self-repair probe tool"

    @pytest.mark.asyncio
    async def test_self_repair_budget_exhaustion_blocks_tool(self):
        rt, graph, node, probe = _self_repair_setup()
        budget = TokenBudget(graph_max_tokens=100)
        rt.tool_funnel.set_budget_ref(budget)
        rt.llm_funnel.set_budget_ref(budget)
        rt.llm_gateway.responses[1] = _resp(
            [_tool_call("probe_tool", call_id="call_2")], prompt=200, completion=200
        )
        ctx = AgentContext()
        ctx.graph_id = graph.id
        tool_results = []
        async for evt in rt._execute_llm_node(
            ctx, node, graph, "fake-model", [rt.tools.get("boom_tool").openai_schema()], ""
        ):
            if evt.type.value == "tool_result" and evt.name == "probe_tool":
                tool_results.append(evt.content)
        assert probe.executions == 0
        assert tool_results and "Blocked by budget" in tool_results[0]


class TestIssue475GrepAssert:
    def test_no_tool_call_evaluate_action_outside_funnel(self):
        pattern = re.compile(r'evaluate_action\(\s*category="tool_call"')
        allowlist = {"_runtime_dynamic_tools.py"}
        offenders = []
        for pkg in ("agent_runtime", "tools", "server"):
            for path in (REPO_ROOT / pkg).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if pattern.search(text) and path.name not in allowlist:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], (
            "evaluate_action(category=\"tool_call\") outside funnel chain: "
            + ", ".join(offenders)
        )

    def test_self_repair_path_uses_funnel_admit(self):
        text = (REPO_ROOT / "agent_runtime" / "_runtime_nodes.py").read_text(
            encoding="utf-8"
        )
        assert "funnel blocked self_repair" in text
        self_repair_seg = text[text.index("Self-repair retry %d/%d") :]
        assert "tool_funnel.admit" in self_repair_seg
        assert 'category="tool_call"' not in self_repair_seg
