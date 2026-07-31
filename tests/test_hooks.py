from __future__ import annotations

import json
from pathlib import Path


from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.hooks import HookConfig, HookEngine, HookResult
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry


class CountingTool(BaseTool):
    name = "counting_tool"
    description = "counts calls"
    parameters = {"input": {"type": "string", "description": "in"}}

    def __init__(self):
        self.calls = 0

    async def execute(self, **kwargs) -> str:
        self.calls += 1
        return f"ran {kwargs.get('input', '')}"


class MockMLXClient:
    def __init__(self):
        self.call_count = 0
        self.responses = []

    def add_response(self, content, tool_calls=None):
        self.responses.append({"content": content, "tool_calls": tool_calls or []})

    async def chat(self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.call_count += 1
        from server.fusion_mlx_client import LLMResponse
        if not self.responses:
            return LLMResponse(content="done", tool_calls=[])
        resp = self.responses.pop(0)
        return LLMResponse(content=resp["content"], tool_calls=resp["tool_calls"])


async def test_callback_approve_default():
    eng = HookEngine()
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "approve"}))
    res = await eng.fire("PRE_TOOL_USE", {"tool_name": "x"}, tool_name="x")
    assert res.decision == "approve"


async def test_callback_block_aggregates():
    eng = HookEngine()
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "block", "reason": "no"}))
    res = await eng.fire("PRE_TOOL_USE", {"tool_name": "x"}, tool_name="x")
    assert res.decision == "block"
    assert res.reason == "no"


async def test_matcher_filters():
    eng = HookEngine()
    eng.register(HookConfig(event="PRE_TOOL_USE", matcher="bad_.*", callback=lambda p: {"decision": "block"}))
    assert (await eng.fire("PRE_TOOL_USE", {}, tool_name="good_tool")).decision == "approve"
    assert (await eng.fire("PRE_TOOL_USE", {}, tool_name="bad_tool")).decision == "block"


async def test_command_hook_approves():
    eng = HookEngine()
    cmd = "echo {\"decision\":\"approve\"}"
    eng.register(HookConfig(event="POST_TOOL_USE", type="command", command=cmd, timeout=5.0))
    res = await eng.fire("POST_TOOL_USE", {"tool_name": "x"}, tool_name="x")
    assert res.decision == "approve"


def test_coerce_variants():
    eng = HookEngine()
    assert eng._coerce(True).decision == "approve"
    assert eng._coerce(False).decision == "block"
    assert eng._coerce("block").decision == "block"
    assert eng._coerce({"decision": "block", "reason": "r"}).reason == "r"
    assert isinstance(eng._coerce(HookResult()), HookResult)


def test_load_from_config(tmp_path: Path):
    cfg = tmp_path / "hooks.json"
    cfg.write_text(json.dumps({"hooks": [
        {"event": "PRE_TOOL_USE", "matcher": ".*", "type": "command", "command": "echo {}", "timeout": 5.0},
    ]}))
    eng = HookEngine()
    eng.load_from_config(cfg)
    assert len(eng.list_hooks()) == 1


async def test_pre_tool_use_block_skips_execution():
    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    client = MockMLXClient()
    client.add_response("go", tool_calls=[{
        "id": "c1", "type": "function",
        "function": {"name": "counting_tool", "arguments": '{"input": "x"}'},
    }])
    client.add_response("done")

    graph = AgentGraph(name="Hook Block")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(
        type="llm", label="LLM", model="m", loop_mode="agent", max_loop_iterations=3,
    ))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    runtime = AgentRuntime(client, registry)
    eng = HookEngine()
    eng.register(HookConfig(event="PRE_TOOL_USE", callback=lambda p: {"decision": "block", "reason": "denied"}))
    runtime.hooks = eng

    events = []
    async for event in runtime.execute_graph(graph, "go"):
        events.append(event)

    assert tool.calls == 0
    results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
    assert any("Blocked by hook" in e.content for e in results)
