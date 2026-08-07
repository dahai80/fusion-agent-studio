import asyncio

import pytest

from agent_runtime.session_manager import (
    BackgroundSession,
    SessionManager,
    SessionStatus,
)


@pytest.fixture
def manager():
    return SessionManager()


class TestBackgroundSession:
    def test_session_to_dict(self):
        s = BackgroundSession(forked_from="parent_1", input_text="hello")
        d = s.to_dict()
        assert d["forked_from"] == "parent_1"
        assert d["status"] == "running"
        assert d["id"].startswith("bg_")

    def test_session_from_dict(self):
        s = BackgroundSession.from_dict(
            {
                "id": "bg_abc",
                "forked_from": "parent",
                "status": "completed",
                "input_text": "test",
            }
        )
        assert s.id == "bg_abc"
        assert s.status == SessionStatus.COMPLETED

    def test_session_post_init(self):
        s = BackgroundSession()
        assert s.id.startswith("bg_")
        assert s.created_at > 0


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_background_list_empty(self, manager):
        result = manager.background_list()
        assert result == []

    @pytest.mark.asyncio
    async def test_fork_without_runtime(self, manager):
        bg = await manager.fork("session_1", "test input")
        assert bg.forked_from == "session_1"
        assert bg.input_text == "test input"
        assert bg.id.startswith("bg_")

        await asyncio.sleep(0.1)
        assert bg.status == SessionStatus.FAILED
        assert "No runtime" in bg.error

    @pytest.mark.asyncio
    async def test_background_list(self, manager):
        await manager.fork("s1", "a")
        await manager.fork("s2", "b")
        sessions = manager.background_list()
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_detach(self, manager):
        bg = await manager.fork("s1", "test")
        assert manager.detach(bg.id) is True
        assert manager.detach("nonexistent") is False

    @pytest.mark.asyncio
    async def test_background_kill(self, manager):
        bg = await manager.fork("s1", "test")
        ok = await manager.background_kill(bg.id)
        assert ok is True
        assert bg.status == SessionStatus.KILLED

        ok2 = await manager.background_kill("nonexistent")
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_attach(self, manager):
        bg = await manager.fork("s1", "test")
        events = await manager.attach(bg.id)
        assert isinstance(events, list)

        with pytest.raises(ValueError, match="not found"):
            await manager.attach("nonexistent")
