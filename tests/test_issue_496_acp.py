"""#496: ACP client + acp graph node — external agent CLI over stdio JSON-RPC.

AC: an ACP node runs an external agent session end-to-end inside a graph;
external-agent events are visible on the graph event stream; missing CLI /
dead session / timeout surface as graceful errors, not crashes.
"""

from __future__ import annotations

import sys

import pytest

from agent_runtime.acp_client import run_acp_prompt
from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.registry import ToolRegistry


def _make_runtime():
    return AgentRuntime(tool_registry=ToolRegistry())

FAKE_AGENT = r'''
import json, os, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

mode = os.environ.get("FAKE_ACP_MODE", "normal")

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req["params"]["protocolVersion"],
            "agentCapabilities": {},
            "authMethods": [],
            "agentInfo": {"name": "fake-acp", "version": "0.1"},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "sess-1"}})
    elif method == "session/prompt":
        if mode == "die":
            sys.exit(1)
        if mode == "slow":
            import time
            time.sleep(30)
        sid = req["params"]["sessionId"]

        def notify(update):
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": update}})

        notify({"sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "q"}})
        notify({"sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "thinking..."}})
        notify({"sessionUpdate": "tool_call", "toolCallId": "tc-1",
                "title": "list files", "kind": "execute"})
        notify({"sessionUpdate": "tool_call_update", "toolCallId": "tc-1",
                "title": "list files", "kind": "execute", "status": "completed"})
        for chunk in ("Hello", " from", " fake", " ACP"):
            notify({"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk}})
        notify({"sessionUpdate": "usage_update", "used": 42, "size": 42})
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "stopReason": "end_turn",
            "usage": {"totalTokens": 42, "inputTokens": 10, "outputTokens": 32},
        }})
    elif method == "session/close":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    else:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "method not found"}})
'''


@pytest.fixture
def fake_agent_path(tmp_path):
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    return script


class TestRunAcpPrompt:
    async def test_success_end_to_end(self, fake_agent_path):
        result = await run_acp_prompt(
            "say hi",
            command=sys.executable,
            args=[str(fake_agent_path)],
            cwd=str(fake_agent_path.parent),
            timeout=30,
        )
        assert result.success is True, result.error
        assert result.stop_reason == "end_turn"
        assert result.message == "Hello from fake ACP"
        assert result.session_id == "sess-1"
        assert result.usage == {"total_tokens": 42, "input_tokens": 10, "output_tokens": 32}
        kinds = [ev["type"] for ev in result.events]
        assert "agent_message_chunk" in kinds
        assert "tool_call" in kinds
        assert "usage_update" in kinds

    async def test_events_stream_in_order(self, fake_agent_path):
        seen = []
        result = await run_acp_prompt(
            "say hi",
            command=sys.executable,
            args=[str(fake_agent_path)],
            cwd=str(fake_agent_path.parent),
            timeout=30,
            on_event=seen.append,
        )
        assert result.success is True
        message_chunks = [e["text"] for e in seen if e["type"] == "agent_message_chunk"]
        assert message_chunks == ["Hello", " from", " fake", " ACP"]
        # thought arrives before any message chunk
        idx_thought = next(i for i, e in enumerate(seen) if e["type"] == "agent_thought_chunk")
        idx_msg = next(i for i, e in enumerate(seen) if e["type"] == "agent_message_chunk")
        assert idx_thought < idx_msg

    async def test_missing_cli_graceful(self):
        result = await run_acp_prompt("hi", command="/nonexistent/acp-xyz-496", timeout=10)
        assert result.success is False
        assert "ACP CLI not found" in result.error

    async def test_dead_session_graceful(self, fake_agent_path):
        result = await run_acp_prompt(
            "hi",
            command=sys.executable,
            args=[str(fake_agent_path)],
            cwd=str(fake_agent_path.parent),
            timeout=30,
            env={"FAKE_ACP_MODE": "die"},
        )
        assert result.success is False
        assert "ACP run failed" in result.error

    async def test_timeout_graceful(self, fake_agent_path):
        result = await run_acp_prompt(
            "hi",
            command=sys.executable,
            args=[str(fake_agent_path)],
            cwd=str(fake_agent_path.parent),
            timeout=2,
            env={"FAKE_ACP_MODE": "slow"},
        )
        assert result.success is False
        assert "timed out" in result.error


def _make_acp_graph(script_path, workdir, **tool_params):
    g = AgentGraph(name="acp_graph_496", start_node_id="start")
    g.add_node("start", NodeConfig(type="start", label="start"))
    params = {
        "command": sys.executable,
        "args": [str(script_path)],
        "cwd": str(workdir),
        "timeout": 30,
    }
    params.update(tool_params)
    g.add_node("acp1", NodeConfig(type="acp", label="acp1", tool_params=params))
    g.add_node("end", NodeConfig(type="end", label="end"))
    g.add_edge("start", "acp1")
    g.add_edge("acp1", "end")
    return g


class TestAcpNodeInGraph:
    async def test_graph_runs_acp_node_end_to_end(self, fake_agent_path, tmp_path):
        rt = _make_runtime()
        g = _make_acp_graph(fake_agent_path, tmp_path)
        errors = g.validate()
        assert errors == []
        events = []
        async for ev in rt.execute_graph(g, "say hi"):
            events.append(ev)
        types = [e.type for e in events]
        assert AgentEventType.ERROR not in types
        tokens = [e.content for e in events if e.type == AgentEventType.TOKEN]
        assert "".join(tokens) == "Hello from fake ACP"
        # external-agent tool activity visible on the stream
        starts = [e for e in events if e.type == AgentEventType.TOOL_CALL_START]
        assert any(e.metadata.get("acp") and e.name == "list files" for e in starts)
        thinking = [e for e in events if e.type == AgentEventType.THINKING_TOKEN]
        assert thinking and thinking[0].content == "thinking..."
        assert types[0] == AgentEventType.START
        assert types[-1] == AgentEventType.END
        # final assistant message lands in ctx messages via the THINK summary
        summary = [e for e in events if e.type == AgentEventType.THINK and e.metadata]
        assert any(e.metadata.get("acp") and e.metadata.get("stop_reason") == "end_turn" for e in summary)

    async def test_graph_missing_cli_yields_error_event(self, tmp_path):
        rt = _make_runtime()
        g = _make_acp_graph("no-such-script.py", tmp_path, command="/nonexistent/acp-xyz-496")
        events = []
        async for ev in rt.execute_graph(g, "say hi"):
            events.append(ev)
        errs = [e for e in events if e.type == AgentEventType.ERROR]
        assert errs and "ACP CLI not found" in errs[0].content

    async def test_graph_dead_session_yields_error_event(self, fake_agent_path, tmp_path):
        rt = _make_runtime()
        # dead session: FAKE_ACP_MODE reaches the child via the node env param
        g = _make_acp_graph(fake_agent_path, tmp_path, env={"FAKE_ACP_MODE": "die"})
        events = []
        async for ev in rt.execute_graph(g, "say hi"):
            events.append(ev)
        errs = [e for e in events if e.type == AgentEventType.ERROR]
        assert errs and "ACP run failed" in errs[0].content

    async def test_graph_node_without_prompt_errors(self, fake_agent_path, tmp_path):
        rt = _make_runtime()
        g = _make_acp_graph(fake_agent_path, tmp_path)
        events = []
        async for ev in rt.execute_graph(g, ""):
            events.append(ev)
        errs = [e for e in events if e.type == AgentEventType.ERROR]
        assert errs and "no prompt" in errs[0].content

    def test_acp_node_type_validated(self, fake_agent_path, tmp_path):
        g = _make_acp_graph(fake_agent_path, tmp_path)
        errors = g.validate()
        assert errors == []
        bad = _make_acp_graph(fake_agent_path, tmp_path)
        bad.nodes["acp1"].type = "acp_typo"
        errors = bad.validate()
        assert any("Invalid node type" in e or "unknown" in e.lower() for e in errors)


class TestAcpEventMapping:
    def _node(self):
        return NodeConfig(type="acp", label="acp1")

    def test_message_chunk_maps_to_token(self):
        rt = _make_runtime()
        evs = rt._acp_event_to_agent_event({"type": "agent_message_chunk", "text": "hi"}, self._node())
        assert len(evs) == 1
        assert evs[0].type == AgentEventType.TOKEN
        assert evs[0].content == "hi"
        assert evs[0].metadata.get("acp") is True

    def test_thought_chunk_maps_to_thinking_token(self):
        rt = _make_runtime()
        evs = rt._acp_event_to_agent_event({"type": "agent_thought_chunk", "text": "hm"}, self._node())
        assert evs[0].type == AgentEventType.THINKING_TOKEN

    def test_tool_call_maps_to_start(self):
        rt = _make_runtime()
        evs = rt._acp_event_to_agent_event(
            {"type": "tool_call", "tool_call_id": "tc-1", "title": "list files"}, self._node()
        )
        assert evs[0].type == AgentEventType.TOOL_CALL_START
        assert evs[0].name == "list files"
        assert evs[0].metadata["tool_call_id"] == "tc-1"

    def test_tool_call_update_maps_to_tool_call(self):
        rt = _make_runtime()
        evs = rt._acp_event_to_agent_event(
            {"type": "tool_call_update", "tool_call_id": "tc-1", "title": "list files", "status": "completed"},
            self._node(),
        )
        assert evs[0].type == AgentEventType.TOOL_CALL
        assert evs[0].metadata["status"] == "completed"

    def test_unmapped_kinds_ignored(self):
        rt = _make_runtime()
        assert rt._acp_event_to_agent_event({"type": "user_message_chunk", "text": "q"}, self._node()) == []
        assert rt._acp_event_to_agent_event({"type": "usage_update"}, self._node()) == []
