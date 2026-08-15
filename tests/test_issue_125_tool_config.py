"""Tests for issue #125 — 声明式智能体包工具级配置 (tool config) 支持."""

from __future__ import annotations

import pytest

from agent_runtime.agent_definition import AgentDefinition, AgentToolConfig
from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _ConfigurableTool(BaseTool):
    name = "cfg_tool"
    description = "A tool that reads injected config defaults"
    parameters = {
        "model": {"type": "string", "description": "model name"},
        "voice": {"type": "string", "description": "tts voice"},
    }

    async def execute(self, **kwargs) -> str:
        return f"model={kwargs.get('model', '')} voice={kwargs.get('voice', '')}"


class TestAgentToolConfigField:
    def test_config_default_empty_dict(self):
        tc = AgentToolConfig(name="mlx_script")
        assert tc.config == {}

    def test_config_roundtrip(self):
        tc = AgentToolConfig(
            name="comfyui_tts",
            config={"backend": "http", "voice": "Cherry"},
        )
        d = tc.to_dict()
        assert d["config"] == {"backend": "http", "voice": "Cherry"}
        tc2 = AgentToolConfig.from_dict(d)
        assert tc2.config == {"backend": "http", "voice": "Cherry"}

    def test_config_null_tolerated(self):
        tc = AgentToolConfig.from_dict({"name": "t", "config": None})
        assert tc.config == {}


class TestFromManifestToolConfig:
    def test_str_tools_backward_compat(self):
        defn = AgentDefinition.from_manifest(
            {"name": "A", "tools": ["search", "calc"]}, agent_id="a1"
        )
        assert len(defn.tools) == 2
        assert defn.tools[0].name == "search"
        assert defn.tools[0].config == {}
        assert defn.tools[1].name == "calc"

    def test_dict_tools_carry_config(self):
        defn = AgentDefinition.from_manifest(
            {
                "name": "A",
                "tools": [
                    {"name": "mlx_script", "config": {"default_model": "Qwen"}},
                    {"name": "publish_scheduler"},
                ],
            },
            agent_id="a1",
        )
        assert len(defn.tools) == 2
        assert defn.tools[0].name == "mlx_script"
        assert defn.tools[0].config == {"default_model": "Qwen"}
        assert defn.tools[1].name == "publish_scheduler"
        assert defn.tools[1].config == {}

    def test_mixed_str_and_dict_tools(self):
        defn = AgentDefinition.from_manifest(
            {
                "name": "A",
                "tools": ["legacy_tool", {"name": "new_tool", "config": {"k": 1}}],
            },
            agent_id="a1",
        )
        assert defn.tools[0].name == "legacy_tool"
        assert defn.tools[0].config == {}
        assert defn.tools[1].name == "new_tool"
        assert defn.tools[1].config == {"k": 1}


class TestRuntimeToolConfigInjection:
    def _make_runtime(self):
        registry = ToolRegistry()
        registry.register(_ConfigurableTool())
        return AgentRuntime(tool_registry=registry)

    def test_set_tool_configs_from_definition(self):
        rt = self._make_runtime()
        defn = AgentDefinition(
            tools=[
                AgentToolConfig(name="cfg_tool", config={"model": "Qwen", "voice": "Cherry"}),
                AgentToolConfig(name="other", config={}),
            ]
        )
        rt.set_tool_configs(defn)
        assert "cfg_tool" in rt.tool_configs
        assert rt.tool_configs["cfg_tool"] == {"model": "Qwen", "voice": "Cherry"}
        assert "other" not in rt.tool_configs

    @pytest.mark.asyncio
    async def test_tool_node_merges_config_defaults(self):
        rt = self._make_runtime()
        defn = AgentDefinition(
            tools=[AgentToolConfig(name="cfg_tool", config={"model": "Qwen", "voice": "Cherry"})]
        )
        rt.set_tool_configs(defn)
        graph = AgentGraph(name="CfgNode")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "t",
            NodeConfig(type="tool", label="T", tool_name="cfg_tool", tool_params={}),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "t")
        graph.add_edge("t", "end")
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert tool_results
        assert "model=Qwen" in tool_results[0].content
        assert "voice=Cherry" in tool_results[0].content

    @pytest.mark.asyncio
    async def test_explicit_args_override_config_defaults(self):
        rt = self._make_runtime()
        defn = AgentDefinition(
            tools=[AgentToolConfig(name="cfg_tool", config={"model": "Qwen", "voice": "Cherry"})]
        )
        rt.set_tool_configs(defn)
        graph = AgentGraph(name="Override")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "t",
            NodeConfig(
                type="tool",
                label="T",
                tool_name="cfg_tool",
                tool_params={"model": "OverrideModel"},
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "t")
        graph.add_edge("t", "end")
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert tool_results
        assert "model=OverrideModel" in tool_results[0].content
        assert "voice=Cherry" in tool_results[0].content

    @pytest.mark.asyncio
    async def test_no_config_backward_compat(self):
        rt = self._make_runtime()
        graph = AgentGraph(name="NoCfg")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node(
            "t",
            NodeConfig(type="tool", label="T", tool_name="cfg_tool", tool_params={}),
        )
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "t")
        graph.add_edge("t", "end")
        events = []
        async for event in rt.execute_graph(graph, "go"):
            events.append(event)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert tool_results
        assert "model=" in tool_results[0].content
        assert "voice=" in tool_results[0].content
