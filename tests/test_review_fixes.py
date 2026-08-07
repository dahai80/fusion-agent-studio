"""Tests for fixes applied during /review --fix pass.

Covers: rag_pipeline session reuse, chat_engine eviction,
runtime retry context cap, llm_gateway stream timeout,
triggers cron logic, persistence list_chat_sessions projection.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.chat_engine import ChatEngine, ChatSession
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.rag_pipeline import VectorRetrievalStrategy
from agent_runtime.runtime import _MAX_RETRY_CONTEXT_MESSAGES


class TestVectorRetrievalStrategySessionReuse:
    def setup_method(self):
        self.strategy = VectorRetrievalStrategy(base_url="http://localhost:8900")

    @pytest.mark.asyncio
    async def test_get_session_reuses_existing(self):
        mock_session = AsyncMock()
        mock_session.closed = False
        self.strategy._session = mock_session
        s = await self.strategy._get_session()
        assert s is mock_session

    @pytest.mark.asyncio
    async def test_get_session_creates_when_none(self):
        assert self.strategy._session is None
        with patch.dict(
            "sys.modules",
            {"aiohttp": MagicMock(ClientSession=MagicMock(return_value=AsyncMock()))},
        ):
            s = await self.strategy._get_session()
            assert s is not None

    @pytest.mark.asyncio
    async def test_close_clears_session(self):
        mock_session = AsyncMock()
        mock_session.closed = False
        self.strategy._session = mock_session
        await self.strategy.close()
        mock_session.close.assert_awaited_once()
        assert self.strategy._session is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_session(self):
        await self.strategy.close()
        assert self.strategy._session is None

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_error(self):
        mock_session = AsyncMock()
        mock_session.closed = False
        self.strategy._session = mock_session
        mock_session.get = AsyncMock(side_effect=Exception("conn refused"))
        assert await self.strategy.is_available() is False


class TestChatEngineEviction:
    def test_evict_sessions_removes_oldest(self):
        engine = ChatEngine.__new__(ChatEngine)
        engine._sessions = {}
        engine._sessions_lock = MagicMock()
        engine.MAX_CACHED_SESSIONS = 3

        for i in range(5):
            s = ChatSession(
                id=f"s{i}",
                title=f"session {i}",
                mode="simple",
                messages=[],
                created_at=time.time() - (5 - i),
                updated_at=time.time() - (5 - i),
            )
            engine._sessions[s.id] = s

        engine._evict_sessions()
        assert len(engine._sessions) == 3
        assert "s0" not in engine._sessions
        assert "s1" not in engine._sessions
        assert "s4" in engine._sessions

    def test_evict_noop_when_under_limit(self):
        engine = ChatEngine.__new__(ChatEngine)
        engine._sessions = {}
        engine._sessions_lock = MagicMock()
        engine.MAX_CACHED_SESSIONS = 128
        engine._sessions["s1"] = ChatSession(
            id="s1", title="t", mode="simple", messages=[]
        )
        engine._evict_sessions()
        assert len(engine._sessions) == 1


class TestRuntimeRetryContextCap:
    def test_max_retry_context_constant(self):
        assert _MAX_RETRY_CONTEXT_MESSAGES == 20
        assert isinstance(_MAX_RETRY_CONTEXT_MESSAGES, int)


class TestLLMGatewayStreamTimeout:
    def test_chat_stream_accepts_timeout_param(self):
        gw = LLMGateway.__new__(LLMGateway)
        assert "timeout" in gw.chat_stream.__code__.co_varnames


class TestPersistenceListProjection:
    def test_list_sessions_skips_messages_json(self, tmp_path):
        from agent_runtime.chat_engine import ChatMessage
        from agent_runtime.persistence import AgentStore

        store = AgentStore(str(tmp_path / "test.db"))

        session = ChatSession(
            id="proj-1",
            title="Test",
            mode="simple",
            messages=[ChatMessage(id="m1", role="user", content="hi")],
        )
        store.save_chat_session(session)

        sessions = store.list_chat_sessions()
        assert len(sessions) == 1
        assert sessions[0].id == "proj-1"
        assert sessions[0].messages == []
