"""#4 AC4 (fusion-harness): durable session event log adopted end-to-end.

Reproduction evidence for closing fusion-harness issue #4:
- a tool-node graph run writes a JSONL session log under
  FUSION_SESSION_EVENT_LOG_DIR: session_start first, session_end last,
  tool_call/tool_result paired by call_id, messages recorded
- SessionEventLog.load() replays it into a SessionState with
  completed_tool_calls
- FUSION_SESSION_EVENT_LOG=off disables persistence (no file)

Runner: pytest tests/test_issue_4_session_event_log.py
"""

from __future__ import annotations

from pathlib import Path

from fusion_harness import SessionEventLog

from agent_runtime.context import AgentEventType
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime
from tools.base import BaseTool
from tools.registry import ToolRegistry


class ReadTool(BaseTool):
    name = "list_dir"
    description = "readonly listing"
    parameters = {}

    async def execute(self, **kwargs) -> str:
        return "[]"


class MockMLXClient:
    async def chat(self, *a, **k):
        return None


def _tool_graph() -> AgentGraph:
    graph = AgentGraph(name="SessionLogGraph")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node("t1", NodeConfig(type="tool", label="T1", tool_name="list_dir"))
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "t1")
    graph.add_edge("t1", "end")
    return graph


def _runtime() -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(ReadTool())
    return AgentRuntime(MockMLXClient(), registry)


class TestSessionEventLogEndToEnd:
    async def test_tool_node_run_writes_replayable_session_log(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FUSION_SESSION_EVENT_LOG_DIR", str(tmp_path))
        rt = _runtime()
        events = []
        async for event in rt.execute_graph(_tool_graph(), "go"):
            events.append(event)
        assert any(e.type == AgentEventType.TOOL_RESULT for e in events), (
            "precondition: the tool actually executed"
        )

        files = list(tmp_path.glob("*.jsonl"))
        assert files, f"no session event log written under {tmp_path}"
        import json

        records = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        types = [r["type"] for r in records]
        assert types[0] == "session_start", types
        assert types[-1] == "session_end", types
        assert "tool_call" in types and "tool_result" in types, types
        call_ids = [r["payload"]["call_id"] for r in records if r["type"] == "tool_call"]
        assert call_ids == ["tool_list_dir"], call_ids

        state = SessionEventLog.load(files[0]).replay()
        assert state.ended, "session_end must mark the replayed state ended"
        assert not state.incomplete_tool_calls, state.incomplete_tool_calls
        assert state.completed_tool_calls, "reducer must pair tool_call/tool_result"
        assert state.messages, "user/assistant messages must be replayed"

    async def test_session_log_disabled_writes_no_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FUSION_SESSION_EVENT_LOG_DIR", str(tmp_path))
        monkeypatch.setenv("FUSION_SESSION_EVENT_LOG", "off")
        rt = _runtime()
        async for _ in rt.execute_graph(_tool_graph(), "go"):
            pass
        assert not list(tmp_path.glob("*.jsonl")), "off must disable persistence"
