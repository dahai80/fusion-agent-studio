"""Tests for agent context."""
from __future__ import annotations

import time


from agent_runtime.context import AgentContext, AgentEvent, AgentEventType


class TestAgentEvent:
    def test_create_event(self):
        event = AgentEvent(type=AgentEventType.THINK, content="Thinking...")
        assert event.type == AgentEventType.THINK
        assert event.content == "Thinking..."
        assert event.timestamp > 0

    def test_event_timestamp(self):
        before = time.time()
        event = AgentEvent(type=AgentEventType.THINK, content="test")
        after = time.time()
        assert before <= event.timestamp <= after

    def test_to_dict(self):
        event = AgentEvent(type=AgentEventType.TOOL_CALL, name="read_file", args={"path": "/tmp"})
        d = event.to_dict()
        assert d["type"] == "tool_call"
        assert d["name"] == "read_file"
        assert d["args"]["path"] == "/tmp"

    def test_from_dict(self):
        event = AgentEvent.from_dict({
            "type": "result", "content": "done", "name": "", "args": {},
            "timestamp": 100.0, "metadata": {},
        })
        assert event.type == AgentEventType.RESULT
        assert event.content == "done"

    def test_all_event_types(self):
        for etype in AgentEventType:
            event = AgentEvent(type=etype)
            assert event.type == etype


class TestAgentContext:
    def test_create_context(self):
        ctx = AgentContext()
        assert ctx.session_id
        assert len(ctx.session_id) == 16
        assert ctx.messages == []
        assert ctx.events == []

    def test_create_with_session_id(self):
        ctx = AgentContext(session_id="custom-id")
        assert ctx.session_id == "custom-id"

    def test_add_user_message(self):
        ctx = AgentContext()
        ctx.add_message("user", "Hello")
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "user"
        assert ctx.messages[0]["content"] == "Hello"

    def test_add_assistant_with_tool_calls(self):
        ctx = AgentContext()
        ctx.add_message("assistant", "Let me check", tool_calls=[{"id": "call_1"}])
        assert ctx.messages[0]["tool_calls"] == [{"id": "call_1"}]

    def test_add_tool_result(self):
        ctx = AgentContext()
        ctx.add_message("tool", "result data", tool_call_id="call_1")
        assert ctx.messages[0]["tool_call_id"] == "call_1"

    def test_add_event(self):
        ctx = AgentContext()
        event = AgentEvent(type=AgentEventType.START, content="Starting")
        ctx.add_event(event)
        assert len(ctx.events) == 1
        assert ctx.events[0].content == "Starting"

    def test_is_complete_finished(self):
        ctx = AgentContext()
        assert not ctx.is_complete()
        ctx.finished_at = time.time()
        assert ctx.is_complete()

    def test_is_complete_error(self):
        ctx = AgentContext()
        ctx.error = "Something went wrong"
        assert ctx.is_complete()

    def test_is_max_iterations_reached(self):
        ctx = AgentContext(max_iterations=5)
        assert not ctx.is_max_iterations_reached()
        ctx.iteration_count = 5
        assert ctx.is_max_iterations_reached()

    def test_elapsed_seconds(self):
        ctx = AgentContext()
        assert ctx.elapsed_seconds() == 0.0
        ctx.started_at = time.time() - 10
        elapsed = ctx.elapsed_seconds()
        assert 9.0 <= elapsed <= 11.0

    def test_elapsed_seconds_finished(self):
        ctx = AgentContext()
        ctx.started_at = 100.0
        ctx.finished_at = 110.0
        assert ctx.elapsed_seconds() == 10.0

    def test_token_usage_empty(self):
        ctx = AgentContext()
        usage = ctx.token_usage()
        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0
        assert usage["total"] == 0

    def test_token_usage_with_messages(self):
        ctx = AgentContext()
        ctx.messages = [
            {"role": "user", "content": "hi", "usage": {"prompt_tokens": 10, "completion_tokens": 0}},
            {"role": "assistant", "content": "hello", "usage": {"prompt_tokens": 0, "completion_tokens": 20}},
        ]
        usage = ctx.token_usage()
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 20
        assert usage["total"] == 30

    def test_to_dict_roundtrip(self):
        ctx = AgentContext(session_id="test-session")
        ctx.add_message("user", "Hello")
        ctx.add_event(AgentEvent(type=AgentEventType.START, content="Begin"))
        ctx.iteration_count = 3
        ctx.current_node_id = "node_1"

        d = ctx.to_dict()
        assert d["session_id"] == "test-session"
        assert len(d["messages"]) == 1
        assert len(d["events"]) == 1
        assert d["iteration_count"] == 3
        assert d["current_node_id"] == "node_1"

    def test_from_dict(self):
        data = {
            "session_id": "s1",
            "messages": [{"role": "user", "content": "hi"}],
            "events": [{"type": "start", "content": "Begin", "name": "", "args": {},
                        "timestamp": 0.0, "metadata": {}}],
            "metadata": {},
            "current_node_id": "n1",
            "iteration_count": 2,
            "max_iterations": 25,
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": "",
        }
        ctx = AgentContext.from_dict(data)
        assert ctx.session_id == "s1"
        assert len(ctx.messages) == 1
        assert len(ctx.events) == 1
        assert ctx.iteration_count == 2
        assert ctx.current_node_id == "n1"