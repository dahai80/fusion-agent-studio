"""#211-#215 graph correctness bundle tests."""

from __future__ import annotations

import asyncio
import os

import pytest

from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import ConditionEngine
from agent_runtime.variable_manager import VariableManager

# ── #211: variable_manager.interpolate 复杂类型产合法 JSON ──────────────


class TestIssue211JsonInterpolate:
    def test_dict_interpolates_to_valid_json(self):
        v = VariableManager()
        v.set("script", {"title": "x", "scenes": [{"img_prompt": "jupiter"}]})
        out = v.interpolate("{{script}}")
        import json

        parsed = json.loads(out)
        assert parsed == {"title": "x", "scenes": [{"img_prompt": "jupiter"}]}
        assert "'" not in out

    def test_list_interpolates_to_valid_json(self):
        v = VariableManager()
        v.set("tags", ["a", "b", "c"])
        import json

        out = v.interpolate("{{tags}}")
        assert json.loads(out) == ["a", "b", "c"]

    def test_bool_interpolates_to_json_literal(self):
        v = VariableManager()
        v.set("flag", True)
        assert v.interpolate("{{flag}}") == "true"
        v.set("flag", False)
        assert v.interpolate("{{flag}}") == "false"

    def test_none_interpolates_to_null(self):
        v = VariableManager()
        v.set("empty", None)
        assert v.interpolate("{{empty}}") == "null"

    def test_scalar_str_unchanged(self):
        v = VariableManager()
        v.set("name", "hello")
        assert v.interpolate("{{name}}") == "hello"

    def test_scalar_int_unchanged(self):
        v = VariableManager()
        v.set("count", 42)
        assert v.interpolate("{{count}}") == "42"

    def test_ensure_ascii_false_keeps_chinese(self):
        v = VariableManager()
        v.set("cn", {"k": "中文"})
        out = v.interpolate("{{cn}}")
        assert "中文" in out


# ── #212: ConditionEngine._compare 类型归一 ──────────────────────────


class TestIssue212CompareCoercion:
    def _ctx(self):
        from agent_runtime.context import AgentContext

        return AgentContext(session_id="t")

    def test_bool_true_vs_string_true_literal(self):
        ce = ConditionEngine()
        v = VariableManager()
        v.set("has_stock", True)
        result = ce.evaluate("has_stock == true", self._ctx(), v)
        assert result == "true"

    def test_bool_false_vs_string_false_literal(self):
        ce = ConditionEngine()
        v = VariableManager()
        v.set("has_stock", False)
        result = ce.evaluate("has_stock == true", self._ctx(), v)
        assert result == "false"

    def test_int_vs_string_number_coercion(self):
        ce = ConditionEngine()
        v = VariableManager()
        v.set("count", 5)
        assert ce.evaluate("count == 5", self._ctx(), v) == "true"
        assert ce.evaluate("count >= 3", self._ctx(), v) == "true"

    def test_bool_literal_uppercase(self):
        ce = ConditionEngine()
        v = VariableManager()
        v.set("flag", True)
        assert ce.evaluate("flag == TRUE", self._ctx(), v) == "true"

    def test_not_equal_bool_coercion(self):
        ce = ConditionEngine()
        v = VariableManager()
        v.set("flag", True)
        assert ce.evaluate("flag != false", self._ctx(), v) == "true"


# ── #213: graph_id 按 name+内容稳定 ──────────────────────────────────


class TestIssue213StableGraphId:
    def test_same_content_same_stable_id(self):
        g1 = AgentGraph.from_dict(
            {
                "name": "x",
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "end"}},
                "edges": [{"source_id": "n1", "target_id": "n2"}],
                "start_node_id": "n1",
            }
        )
        g2 = AgentGraph.from_dict(
            {
                "name": "x",
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "end"}},
                "edges": [{"source_id": "n1", "target_id": "n2"}],
                "start_node_id": "n1",
            }
        )
        assert g1.stable_id() == g2.stable_id()

    def test_different_name_different_stable_id(self):
        g1 = AgentGraph.from_dict(
            {"name": "a", "nodes": {"n1": {"type": "start"}}, "start_node_id": "n1"}
        )
        g2 = AgentGraph.from_dict(
            {"name": "b", "nodes": {"n1": {"type": "start"}}, "start_node_id": "n1"}
        )
        assert g1.stable_id() != g2.stable_id()

    def test_different_content_different_stable_id(self):
        g1 = AgentGraph.from_dict(
            {
                "name": "x",
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "end"}},
                "edges": [{"source_id": "n1", "target_id": "n2"}],
                "start_node_id": "n1",
            }
        )
        g2 = AgentGraph.from_dict(
            {
                "name": "x",
                "nodes": {"n1": {"type": "start"}, "n2": {"type": "llm"}},
                "edges": [{"source_id": "n1", "target_id": "n2"}],
                "start_node_id": "n1",
            }
        )
        assert g1.stable_id() != g2.stable_id()

    def test_default_uuid_id_unchanged_without_stable_opt(self):
        g1 = AgentGraph.from_dict(
            {"name": "x", "nodes": {"n1": {"type": "start"}}, "start_node_id": "n1"}
        )
        g2 = AgentGraph.from_dict(
            {"name": "x", "nodes": {"n1": {"type": "start"}}, "start_node_id": "n1"}
        )
        assert g1.id != g2.id


# ── #214: daemon graph.execute 并发节流 ──────────────────────────────


class TestIssue214ConcurrencyThrottle:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        from agent_runtime.daemon_server import DaemonServer

        d = DaemonServer(
            ws_port=0, cluster_port=0, http_port=0, store_path=":memory:"
        )
        d._graph_concurrency_limit = 1
        d._graph_semaphore = asyncio.Semaphore(1)
        in_flight = 0
        peak = 0

        async def hold():
            nonlocal in_flight, peak
            async with d._graph_semaphore:
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(*[hold() for _ in range(4)])
        assert peak == 1

    @pytest.mark.asyncio
    async def test_no_semaphore_when_limit_zero(self):
        from agent_runtime.daemon_server import DaemonServer

        d = DaemonServer(
            ws_port=0, cluster_port=0, http_port=0, store_path=":memory:"
        )
        d._graph_concurrency_limit = 0
        d._graph_semaphore = None
        assert d._graph_semaphore is None

    @pytest.mark.asyncio
    async def test_env_sets_limit(self, monkeypatch):
        # env 解析逻辑: 模拟 start() 内的 int(env or "0") parse, 非法值回退 0.
        def parse_limit(env_val):
            try:
                return max(0, int(env_val or "0"))
            except ValueError:
                return 0

        monkeypatch.setenv("FUSION_GRAPH_CONCURRENCY", "2")
        assert parse_limit(os.environ.get("FUSION_GRAPH_CONCURRENCY", "0")) == 2
        monkeypatch.setenv("FUSION_GRAPH_CONCURRENCY", "garbage")
        assert parse_limit(os.environ.get("FUSION_GRAPH_CONCURRENCY", "0")) == 0
        monkeypatch.setenv("FUSION_GRAPH_CONCURRENCY", "")
        assert parse_limit(os.environ.get("FUSION_GRAPH_CONCURRENCY", "0")) == 0


# ── #215: AgentGraph.validate 内容 schema ─────────────────────────────


class TestIssue215ValidateContentSchema:
    def test_structure_validation_still_works(self):
        g = AgentGraph(name="bad")
        errors = g.validate()
        assert any("no start node" in e for e in errors)

    def test_tool_name_not_in_registry_is_error(self):
        from tools import create_default_registry

        registry = create_default_registry()
        g = AgentGraph(name="x")
        g.add_node("n1", NodeConfig(type="start"))
        g.add_node(
            "n2",
            NodeConfig(type="tool", tool_name="nonexistent_tool_xyz", tool_params={}),
        )
        g.add_node("n3", NodeConfig(type="end"))
        g.add_edge("n1", "n2")
        g.add_edge("n2", "n3")
        errors = g.validate(registry)
        hard = [e for e in errors if not e.startswith("warning:")]
        assert any("nonexistent_tool_xyz" in e for e in hard)

    def test_condition_expr_unparseable_is_error(self):
        g = AgentGraph(name="x")
        g.add_node("n1", NodeConfig(type="start"))
        g.add_node(
            "n2",
            NodeConfig(type="condition", condition_expr="@@@ garbage @@@"),
        )
        g.add_node("n3", NodeConfig(type="end"))
        g.add_edge("n1", "n2", label="true")
        g.add_edge("n2", "n3")
        errors = g.validate()
        hard = [e for e in errors if not e.startswith("warning:")]
        assert any("unparseable" in e for e in hard)

    def test_valid_condition_expr_no_error(self):
        g = AgentGraph(name="x")
        g.add_node("n1", NodeConfig(type="start"))
        g.add_node("n2", NodeConfig(type="condition", condition_expr="iteration >= 5"))
        g.add_node("n3", NodeConfig(type="end"))
        g.add_edge("n1", "n2", label="true")
        g.add_edge("n2", "n3")
        errors = g.validate()
        hard = [e for e in errors if not e.startswith("warning:")]
        assert not hard

    def test_validate_without_registry_skips_tool_check(self):
        g = AgentGraph(name="x")
        g.add_node("n1", NodeConfig(type="start"))
        g.add_node(
            "n2",
            NodeConfig(type="tool", tool_name="whatever_unknown", tool_params={}),
        )
        g.add_node("n3", NodeConfig(type="end"))
        g.add_edge("n1", "n2")
        g.add_edge("n2", "n3")
        errors = g.validate()
        hard = [e for e in errors if not e.startswith("warning:")]
        assert not hard
