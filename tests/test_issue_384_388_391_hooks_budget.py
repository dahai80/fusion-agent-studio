import json

import pytest

import agent_runtime._permissions_local as perms_mod
from agent_runtime._funnels_local import FunnelOutcome, LLMFunnel, ToolFunnel
from agent_runtime._hooks_local import HookConfig, HookEngine
from agent_runtime._permissions_local import PermissionEngine, PermissionMode, PermissionRule
from agent_runtime.context import AgentContext
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from agent_runtime.token_budget import TokenBudget
from tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def isolated_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(perms_mod, "PERMISSIONS_FILE", tmp_path / "permissions.json")
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_PASSTHROUGH", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_A3", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)


# ---------------- #384 permission engine ----------------


class TestIssue384Permissions:
    def test_plan_mode_denies_write_allows_readonly(self):
        engine = PermissionEngine(mode=PermissionMode.PLAN)
        assert engine.evaluate("write_file", {"path": "/tmp/x"}, readonly_hint=False).decision == "deny"
        assert engine.evaluate("read_file", {"path": "/tmp/x"}, readonly_hint=True).decision == "allow"
        assert engine.stats()["denies"] >= 1

    def test_three_layer_rules_persist_roundtrip(self, tmp_path):
        engine = PermissionEngine()
        engine.add_rule(PermissionRule(action="deny", tool="shell_exec", layer="global"))
        engine.add_rule(PermissionRule(action="ask", tool="write_file", layer="graph"))
        engine.add_rule(PermissionRule(action="allow", tool="read_file", layer="node"))
        path = tmp_path / "perms.json"
        assert engine.save_to_file(path) is True

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert len(payload["rules"]) == 1
        assert len(payload["graph_rules"]) == 1
        assert len(payload["node_rules"]) == 1

        loaded = PermissionEngine()
        assert loaded.load_file_rules(path) == 3
        assert loaded.rules_count() >= 3
        layers = {r["tool"]: r["layer"] for r in loaded.list_rules()}
        assert layers["shell_exec"] == "global"
        assert layers["write_file"] == "graph"
        assert layers["read_file"] == "node"

    def test_path_scope_limits_rule(self):
        engine = PermissionEngine(
            rules=[
                PermissionRule(
                    action="allow",
                    tool="write_file",
                    path_scope=["/tmp/ok"],
                )
            ]
        )
        inside = engine.evaluate("write_file", {"path": "/tmp/ok/a.txt"})
        assert inside.decision == "allow"
        assert inside.rule_source.startswith("rule:allow")
        outside = engine.evaluate("write_file", {"path": "/etc/passwd"})
        assert outside.rule_source != "rule:allow:global"

    def test_loopback_heuristic_metered(self):
        engine = PermissionEngine(mode=PermissionMode.DEFAULT)
        decision = engine.evaluate("http_get", {"url": "http://127.0.0.1:11434/v1/models"})
        assert decision.decision == "allow"
        assert decision.rule_source == "heuristic_loopback"
        assert engine.stats()["auto_allow_loopback"] == 1

    def test_loopback_heuristic_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_PERMISSION_A3", "0")
        engine = PermissionEngine(mode=PermissionMode.DEFAULT)
        decision = engine.evaluate("http_get", {"url": "http://127.0.0.1:11434/v1/models"})
        assert decision.decision != "allow" or decision.rule_source != "heuristic_loopback"

    def test_passthrough_env_turns_ask(self, monkeypatch):
        monkeypatch.setenv("FUSION_PERMISSION_PASSTHROUGH", "0")
        assert PermissionEngine.passthrough_enabled() is False
        engine = PermissionEngine(mode=PermissionMode.DEFAULT)
        assert engine.evaluate("shell_exec", {"cmd": "ls"}).decision == "ask"


# ---------------- #388 MCP funnel admission ----------------


class _FunnelStub:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def admit(self, **kwargs):
        self.calls += 1
        return self.outcome


class _RuntimeStub:
    def __init__(self, funnel):
        self.tool_funnel = funnel
        self.tool_registry = ToolRegistry()
        self._mcp_registry = None


class _DaemonStub:
    def __init__(self, runtime):
        self._runtime = runtime

    def _get_runtime(self):
        return self._runtime


class TestIssue388McpAdmission:
    @pytest.mark.asyncio
    async def test_mcp_rpc_denied_by_permission(self):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        funnel = _FunnelStub(FunnelOutcome(allowed=False, blocked_by="permission", reason="denied"))
        disp = McpDispatcher(_DaemonStub(_RuntimeStub(funnel)))
        result = await disp._handle_list_resources({})
        assert "error" in result
        assert funnel.calls == 1

    @pytest.mark.asyncio
    async def test_mcp_rpc_allowed_reaches_transport(self):
        from agent_runtime.dispatchers.mcp import McpDispatcher

        funnel = _FunnelStub(FunnelOutcome(allowed=True))
        disp = McpDispatcher(_DaemonStub(_RuntimeStub(funnel)))
        result = await disp._handle_list_prompts({})
        assert "error" not in result
        assert funnel.calls == 1

    @pytest.mark.asyncio
    async def test_daemon_status_reports_capabilities(self, monkeypatch, tmp_path):
        from agent_runtime.daemon_server import DaemonServer

        monkeypatch.setenv("FUSION_TOOLSEARCH", "1")
        monkeypatch.setenv("FUSION_SUBAGENT_TOOLS", "1")
        d = DaemonServer(
            socket_path=str(tmp_path / "d.sock"),
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "store.db"),
        )
        handler = d._get_handler("daemon.status")
        status = await handler({})
        caps = status["capabilities"]
        assert caps["toolsearch"] is True
        assert caps["subagent_tools"] is True
        assert caps["mcp_outbound"] is True
        assert isinstance(caps["runtime_review"], bool)


# ---------------- #391 hooks + budget ----------------


class TestIssue391Hooks:
    @pytest.mark.asyncio
    async def test_new_hook_events_dispatch(self):
        eng = HookEngine(auto_register_decorators=False)
        seen = []
        for event in ("ERROR", "CHECKPOINT", "TEAM_MESSAGE"):
            eng.register(HookConfig(event=event, callback=lambda p, e=event: seen.append((e, p))))
        await eng.fire("ERROR", {"node": "n1", "content": "boom"})
        await eng.fire("CHECKPOINT", {"node": "n1"})
        await eng.fire("TEAM_MESSAGE", {"kind": "broadcast"})
        assert [e for e, _ in seen] == ["ERROR", "CHECKPOINT", "TEAM_MESSAGE"]

    @pytest.mark.asyncio
    async def test_runtime_fires_error_and_checkpoint_hooks(self):
        eng = HookEngine(auto_register_decorators=False)
        seen = []
        eng.register(HookConfig(event="ERROR", callback=lambda p: seen.append(("ERROR", p))))
        eng.register(
            HookConfig(event="CHECKPOINT", callback=lambda p: seen.append(("CHECKPOINT", p)))
        )
        rt = AgentRuntime(llm_gateway=None, tool_registry=ToolRegistry())
        rt.hooks = eng
        graph = AgentGraph(name="g")
        graph.add_node("s", NodeConfig(type="start", label="s"))
        graph.add_node("e", NodeConfig(type="end", label="e"))
        graph.add_edge("s", "e")
        events = [e async for e in rt.execute_graph(graph, "hi")]
        assert any(e.type.value == "end" for e in events)
        assert all(e[0] != "ERROR" for e in seen)

    @pytest.mark.asyncio
    async def test_team_message_hook_fires_on_broadcast(self):
        from agent_runtime.dispatchers.team import TeamDispatcher

        eng = HookEngine(auto_register_decorators=False)
        seen = []
        eng.register(HookConfig(event="TEAM_MESSAGE", callback=lambda p: seen.append(p)))

        class _D:
            def _get_hooks(self):
                return eng

            def _serialize(self, obj):
                return {"ok": True}

            def _get_plaza(self):
                class _P:
                    def broadcast(self, *a, **kw):
                        return object()

                return _P()

        disp = TeamDispatcher(_D())
        await disp._handle_team_plaza_broadcast(
            {"channel": "ops", "sender": "a", "content": "hello"}
        )
        assert seen and seen[0]["kind"] == "broadcast"
        assert seen[0]["channel"] == "ops"


class TestIssue391Budget:
    def test_day_cap(self, monkeypatch):
        budget = TokenBudget(day_max_tokens=100)
        budget.record_usage(40, 20)
        assert budget.is_exceeded() is False
        budget.record_usage(40, 10)
        assert budget.is_exceeded() is True
        assert budget.status()["day_spent_tokens"] == 110

    def test_graph_cap_independent(self):
        budget = TokenBudget(graph_max_tokens=100)
        budget.record_usage(60, 50, graph_id="g1")
        assert budget.is_exceeded("g1") is True
        assert budget.is_exceeded("g2") is False
        assert budget.remaining("g2") == 100
        assert budget.remaining("g1") == 0

    def test_graph_budget_roundtrip(self):
        budget = TokenBudget(graph_max_tokens=100)
        budget.record_usage(10, 5, graph_id="g1")
        clone = TokenBudget.from_dict(budget.to_dict())
        assert clone.graph_max_tokens == 100
        assert clone.graph_spent_tokens["g1"] == 15

    @pytest.mark.asyncio
    async def test_llm_funnel_charges_graph_budget(self):
        budget = TokenBudget(graph_max_tokens=100)
        funnel = LLMFunnel(budget_ref=budget)
        ctx = AgentContext(graph_id="g1")
        funnel.post_llm(ctx, "n", "m", usage={"prompt_tokens": 60, "completion_tokens": 60})
        assert budget.graph_spent_tokens["g1"] == 120
        proceed, reason, _ = await funnel.pre_llm(ctx, "n", "m", [])
        assert proceed is False
        assert "budget" in reason

    @pytest.mark.asyncio
    async def test_runtime_sets_ctx_graph_id(self):
        rt = AgentRuntime(llm_gateway=None, tool_registry=ToolRegistry())
        graph = AgentGraph(name="g")
        graph.add_node("s", NodeConfig(type="start", label="s"))
        graph.add_node("e", NodeConfig(type="end", label="e"))
        graph.add_edge("s", "e")
        ctx = AgentContext()
        async for _ in rt.execute_graph(graph, "hi", context=ctx):
            pass
        assert ctx.graph_id == graph.id


class TestIssue391PermissionsNone:
    @pytest.mark.asyncio
    async def test_missing_engine_warns_and_passes_by_default(self, caplog):
        funnel = ToolFunnel()
        outcome = await funnel.admit(ctx=AgentContext(), tool_name="shell_exec", args={})
        assert outcome.allowed is True
        assert any(e["stage"] == "permission" and e["verdict"] == "skip" for e in outcome.decision_log)
        assert any("permission engine absent" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_engine_fails_closed_when_required(self, monkeypatch):
        monkeypatch.setenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", "1")
        funnel = ToolFunnel()
        outcome = await funnel.admit(ctx=AgentContext(), tool_name="shell_exec", args={})
        assert outcome.allowed is False
        assert outcome.blocked_by == "permission"
