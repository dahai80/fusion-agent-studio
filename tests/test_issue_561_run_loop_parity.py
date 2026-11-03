"""#561: run-loop parity — stop_reason in run events + tool_use auto-continue.

ACs:
1. THINK events carry stop_reason (tool_use / max_tokens / end_turn);
   pending approvals carry pause_turn; values documented.
2. Auto-continue verified at graph level: loop_mode="agent" llm node
   feeds tool results back without a hand-wired return edge, multi-hop.
3. Non-continue cases surfaced: max_tokens hop, pause hop.
"""

import asyncio

import pytest

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse
from tools.base import BaseTool
from tools.registry import ToolRegistry


class MockTool(BaseTool):
    name = "mock_tool"
    description = "A mock tool for testing"
    parameters = {"input": {"type": "string", "description": "Input"}}

    async def execute(self, **kwargs) -> str:
        return f"Executed: {kwargs.get('input', '')}"


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.call_count += 1
        if not self.responses:
            return LLMResponse(content="done", tool_calls=[])
        r = self.responses.pop(0)
        return LLMResponse(
            content=r.get("content", ""),
            tool_calls=r.get("tool_calls", []),
            finish_reason=r.get("finish_reason", "stop"),
        )


def _tool_call(cid="call_1"):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": "mock_tool", "arguments": '{"input": "x"}'},
    }


def _agent_graph(max_iter=5):
    graph = AgentGraph(name="561")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "llm",
        NodeConfig(
            type="llm",
            label="LLM",
            model="test-model",
            loop_mode="agent",
            max_loop_iterations=max_iter,
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    return graph


def _registry():
    reg = ToolRegistry()
    reg.register(MockTool())
    return reg


async def _collect(rt, graph, text="go"):
    events = []
    async for ev in rt.execute_graph(graph, text):
        events.append(ev)
    return events


async def test_think_carries_tool_use_then_end_turn():
    client = ScriptedClient(
        [
            {"content": "checking", "tool_calls": [_tool_call()]},
            {"content": "final answer", "tool_calls": []},
        ]
    )
    rt = AgentRuntime(client, _registry())
    events = await _collect(rt, _agent_graph())
    thinks = [e for e in events if e.type == AgentEventType.THINK]
    assert [t.metadata.get("stop_reason") for t in thinks] == ["tool_use", "end_turn"]
    ends = [e for e in events if e.type == AgentEventType.END]
    assert ends and ends[-1].metadata.get("stop_reason") == "end_turn"


async def test_auto_continue_multi_hop_no_return_edge():
    """Graph-level auto-continue: 2 tool hops + final, no hand-wired edges."""
    client = ScriptedClient(
        [
            {"content": "hop1", "tool_calls": [_tool_call("c1")]},
            {"content": "hop2", "tool_calls": [_tool_call("c2")]},
            {"content": "final", "tool_calls": []},
        ]
    )
    rt = AgentRuntime(client, _registry())
    events = await _collect(rt, _agent_graph())
    assert client.call_count == 3
    calls = [e for e in events if e.type == AgentEventType.TOOL_CALL]
    results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
    assert len(calls) == 2 and len(results) == 2
    thinks = [e for e in events if e.type == AgentEventType.THINK]
    assert [t.metadata.get("stop_reason") for t in thinks] == [
        "tool_use",
        "tool_use",
        "end_turn",
    ]
    assert any(e.type == AgentEventType.END for e in events)


async def test_non_agent_node_tool_use_surfaced_then_run_continues():
    graph = AgentGraph(name="561-single")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="m"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    client = ScriptedClient(
        [
            {"content": "using tool", "tool_calls": [_tool_call()]},
        ]
    )
    rt = AgentRuntime(client, _registry())
    events = await _collect(rt, graph)
    thinks = [e for e in events if e.type == AgentEventType.THINK]
    assert thinks[0].metadata.get("stop_reason") == "tool_use"
    assert any(e.type == AgentEventType.TOOL_RESULT for e in events)
    ends = [e for e in events if e.type == AgentEventType.END]
    assert ends and ends[-1].metadata.get("stop_reason") == "end_turn"


async def test_think_max_tokens_surfaced():
    graph = AgentGraph(name="561-len")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("llm", NodeConfig(type="llm", label="LLM", model="m"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")
    client = ScriptedClient(
        [{"content": "truncated outp", "finish_reason": "length"}]
    )
    rt = AgentRuntime(client, _registry())
    events = await _collect(rt, graph)
    thinks = [e for e in events if e.type == AgentEventType.THINK]
    assert thinks[0].metadata.get("stop_reason") == "max_tokens"


async def test_pending_approval_carries_pause_turn():
    rt = AgentRuntime(ScriptedClient([]), _registry())
    rt._safety_timeout = 5.0

    class FakeVerdict:
        requires_approval = True
        reason = "needs approval"
        metadata = {"level": "L2"}
        diff_preview = None

    async def run():
        evts = []
        async for evt in rt._await_safety_approval(
            AgentContext(), FakeVerdict(), "tool_call", "n1", tool_name="mock_tool"
        ):
            evts.append(evt)
            if evt.metadata.get("action") == "pending_approval":
                aid = evt.metadata["action_id"]
                rt._safety_futures[aid].set_result(True)
        return evts

    evts = await asyncio.wait_for(run(), timeout=5)
    pending = [
        e
        for e in evts
        if e.type == AgentEventType.SAFETY_APPROVAL
        and e.metadata.get("action") == "pending_approval"
    ]
    assert pending and pending[0].metadata.get("stop_reason") == "pause_turn"


def test_stop_reason_semantics_documented():
    import agent_runtime._runtime_nodes as rn

    src = open(rn.__file__).read()
    assert "tool_use" in src and "max_tokens" in src and "end_turn" in src
    assert "pause_turn" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
