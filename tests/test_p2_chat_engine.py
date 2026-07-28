"""Tests for P2-1: Unified ChatEngine."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_runtime.chat_engine import (
    ChatEngine, ChatSession, ChatMessage, ChatEvent,
    ChatEventType, ChatMode,
)
from agent_runtime.persistence import AgentStore


class MockStreamClient:
    def __init__(self, tokens=None):
        self._tokens = tokens or ["hel", "lo", " world"]

    async def chat_stream(self, **kwargs):
        from server.fusion_mlx_client import StreamChunk
        for t in self._tokens:
            yield StreamChunk(delta_content=t, delta_tool_calls=None, finish_reason=None)
        yield StreamChunk(delta_content="", delta_tool_calls=None, finish_reason="stop")


@pytest.fixture
def store(tmp_path):
    return AgentStore(db_path=tmp_path / "test_chat.db")


@pytest.fixture
def engine(store):
    return ChatEngine(store=store)


def test_chat_mode_enum():
    assert ChatMode.SIMPLE.value == "simple"
    assert ChatMode.AGENT.value == "agent"
    assert ChatMode.CODE.value == "code"
    assert ChatMode.DESIGN.value == "design"
    assert ChatMode.RAG.value == "rag"


def test_chat_event_type_enum():
    assert ChatEventType.TOKEN.value == "token"
    assert ChatEventType.TOOL_CALL.value == "tool_call"
    assert ChatEventType.TOOL_RESULT.value == "tool_result"
    assert ChatEventType.DONE.value == "done"
    assert ChatEventType.ERROR.value == "error"


def test_chat_message_serialization():
    msg = ChatMessage(role="user", content="hello")
    d = msg.to_dict()
    assert d["role"] == "user"
    assert d["content"] == "hello"
    assert d["id"]

    restored = ChatMessage.from_dict(d)
    assert restored.role == "user"
    assert restored.content == "hello"
    assert restored.id == msg.id


def test_chat_session_branching():
    session = ChatSession(mode=ChatMode.SIMPLE.value)
    m1 = ChatMessage(role="user", content="hi")
    session.add_message(m1)
    m2 = ChatMessage(role="assistant", content="hello")
    session.add_message(m2, parent_id=m1.id)

    branch = session.get_linear_branch()
    assert len(branch) == 2
    assert branch[0].content == "hi"
    assert branch[1].content == "hello"

    m3 = ChatMessage(role="user", content="alt question", parent_id=m1.id)
    session.add_message(m3)
    branch2 = session.get_linear_branch(leaf_id=m3.id)
    assert len(branch2) == 2
    assert branch2[1].content == "alt question"


def test_create_session(engine):
    s = engine.create_session(mode=ChatMode.AGENT.value, title="Test")
    assert s.mode == "agent"
    assert s.title == "Test"
    assert s.id

    loaded = engine.get_session(s.id)
    assert loaded is not None
    assert loaded.mode == "agent"


def test_list_sessions(engine):
    s1 = engine.create_session(title="First")
    s2 = engine.create_session(title="Second")
    sessions = engine.list_sessions()
    assert len(sessions) >= 2
    ids = [s.id for s in sessions]
    assert s1.id in ids
    assert s2.id in ids


def test_delete_session(engine):
    s = engine.create_session()
    assert engine.delete_session(s.id) is True
    assert engine.get_session(s.id) is None
    assert engine.delete_session("nonexistent") is False


def test_send_simple_mode(engine):
    from agent_runtime.runtime import AgentRuntime
    from agent_runtime.llm_gateway import LLMGateway, ModelConfig
    from server.fusion_mlx_client import FusionMLXClient, StreamChunk

    class FakeStreamClient:
        async def chat_stream(self, **kwargs):
            for tok in ["Hello", " from", " chat"]:
                yield StreamChunk(delta_content=tok, delta_tool_calls=None, finish_reason=None)
            yield StreamChunk(delta_content="", delta_tool_calls=None, finish_reason="stop")

    fake_client = FakeStreamClient()

    gw = LLMGateway()
    gw.register_model(ModelConfig(name="default", provider="local", context_length=4096))
    gw.set_default_client(fake_client)

    mlx = FusionMLXClient.__new__(FusionMLXClient)
    mlx.base_url = "http://localhost:11434"

    runtime = AgentRuntime(mlx_client=mlx, llm_gateway=gw, store=engine.store)
    engine.runtime = runtime

    session = engine.create_session(mode=ChatMode.SIMPLE.value)
    tokens = []

    async def collect():
        async for ev in engine.send(session.id, "hi"):
            if ev.type == ChatEventType.TOKEN:
                tokens.append(ev.content)

    asyncio.run(collect())
    assert "".join(tokens) == "Hello from chat"


def test_send_no_runtime():
    eng = ChatEngine(store=None)
    s = eng.create_session(mode=ChatMode.SIMPLE.value)
    tokens = []

    async def collect():
        async for ev in eng.send(s.id, "hi"):
            if ev.type == ChatEventType.TOKEN:
                tokens.append(ev.content)

    asyncio.run(collect())
    assert tokens == ["[no LLM available]"]


def test_branch_session(engine):
    s = engine.create_session(mode=ChatMode.SIMPLE.value, title="Original")
    m1 = ChatMessage(role="user", content="first")
    s.add_message(m1)
    m2 = ChatMessage(role="assistant", content="response")
    s.add_message(m2, parent_id=m1.id)

    branched = engine.branch(s.id, m1.id)
    assert branched is not None
    assert branched.title == "Original (branch)"
    branch_msgs = branched.get_linear_branch()
    assert len(branch_msgs) == 1
    assert branch_msgs[0].content == "first"
    assert branched.id != s.id


def test_edit_message(engine):
    s = engine.create_session(mode=ChatMode.SIMPLE.value)
    m1 = ChatMessage(role="user", content="original")
    s.add_message(m1)

    edited = engine.edit(s.id, m1.id, "edited content")
    assert edited is not None
    assert edited.content == "edited content"
    assert edited.parent_id == m1.parent_id

    branch = s.get_linear_branch(leaf_id=edited.id)
    assert branch[-1].content == "edited content"


def test_edit_non_user_message(engine):
    s = engine.create_session()
    m1 = ChatMessage(role="assistant", content="bot says")
    s.add_message(m1)
    result = engine.edit(s.id, m1.id, "new")
    assert result is None


def test_persistence_round_trip(store):
    s = ChatSession(mode=ChatMode.CODE.value, title="Code Chat")
    m1 = ChatMessage(role="user", content="write a function")
    s.add_message(m1)
    m2 = ChatMessage(role="assistant", content="def foo(): pass")
    s.add_message(m2, parent_id=m1.id)

    store.save_chat_session(s)

    loaded = store.load_chat_session(s.id)
    assert loaded is not None
    assert loaded.mode == "code"
    assert loaded.title == "Code Chat"
    assert len(loaded.messages) == 2

    sessions = store.list_chat_sessions()
    assert any(x.id == s.id for x in sessions)

    assert store.delete_chat_session(s.id) is True
    assert store.load_chat_session(s.id) is None


def test_chat_event_serialization():
    ev = ChatEvent(type=ChatEventType.TOKEN, content="hi")
    d = ev.to_dict()
    assert d["type"] == "token"
    assert d["content"] == "hi"

    restored = ChatEvent.from_dict(d)
    assert restored.type == ChatEventType.TOKEN
    assert restored.content == "hi"


def test_send_session_not_found(engine):
    results = []

    async def collect():
        async for ev in engine.send("nonexistent", "hi"):
            results.append(ev)

    asyncio.run(collect())
    assert len(results) == 1
    assert results[0].type == ChatEventType.ERROR


def test_get_linear_branch_empty():
    s = ChatSession()
    assert s.get_linear_branch() == []
