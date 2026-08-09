"""Tests for agent.* and marketplace.* daemon handlers."""

from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    d = DaemonServer(socket_path=str(tmp_path / "test.sock"))
    d._agents = {}
    yield d


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


class TestAgentCreate:
    @pytest.mark.asyncio
    async def test_create_basic(self, daemon):
        result = await _run(daemon, "agent.create", {"name": "TestBot"})
        assert result["agent_id"]
        assert result["manifest"]["name"] == "TestBot"
        assert result["manifest"]["system_prompt"] == "You are TestBot."

    @pytest.mark.asyncio
    async def test_create_with_full_config(self, daemon):
        result = await _run(
            daemon,
            "agent.create",
            {
                "name": "FullBot",
                "model": "llama-3.2",
                "system_prompt": "You are a helpful assistant.",
                "temperature": 0.5,
                "max_tokens": 2048,
                "tools": ["web_search", "code_run"],
                "capabilities": ["reasoning", "code"],
                "safety_level": "L2",
                "tags": ["assistant", "code"],
                "description": "A full-featured bot",
            },
        )
        assert result["manifest"]["model"] == "llama-3.2"
        assert result["manifest"]["tools"] == ["web_search", "code_run"]
        assert result["manifest"]["temperature"] == 0.5
        assert result["manifest"]["safety_level"] == "L2"

    @pytest.mark.asyncio
    async def test_create_missing_name(self, daemon):
        result = await _run(daemon, "agent.create", {})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_create_with_soul(self, daemon):
        result = await _run(
            daemon,
            "agent.create",
            {
                "name": "SoulBot",
                "soul": "# Soul\nYou are a creative writer.",
            },
        )
        assert result["agent_id"]
        agent_id = result["agent_id"]
        soul_result = await _run(daemon, "agent.get_soul", {"agent_id": agent_id})
        assert "creative writer" in soul_result["soul"]


class TestAgentGet:
    @pytest.mark.asyncio
    async def test_get_existing(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "GetBot"})
        agent_id = created["agent_id"]
        result = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert result["agent"]["name"] == "GetBot"
        assert result["agent"]["id"] == agent_id

    @pytest.mark.asyncio
    async def test_get_not_found(self, daemon):
        result = await _run(daemon, "agent.get", {"agent_id": "nonexistent"})
        assert result["status"] == "error"


class TestAgentList:
    @pytest.mark.asyncio
    async def test_list_empty(self, daemon):
        daemon._agents = {}
        result = await _run(daemon, "agent.list")
        assert result["agents"] == []

    @pytest.mark.asyncio
    async def test_list_with_agents(self, daemon):
        await _run(daemon, "agent.create", {"name": "Bot1"})
        await _run(daemon, "agent.create", {"name": "Bot2"})
        result = await _run(daemon, "agent.list")
        assert len(result["agents"]) >= 2

    @pytest.mark.asyncio
    async def test_list_filter_by_tags(self, daemon):
        await _run(
            daemon, "agent.create", {"name": "TagBot", "tags": ["code", "assistant"]}
        )
        await _run(daemon, "agent.create", {"name": "NoTagBot", "tags": []})
        result = await _run(daemon, "agent.list", {"tags": ["code"]})
        names = [a["name"] for a in result["agents"]]
        assert "TagBot" in names


class TestAgentUpdate:
    @pytest.mark.asyncio
    async def test_update_name(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "OldName"})
        agent_id = created["agent_id"]
        result = await _run(
            daemon, "agent.update", {"agent_id": agent_id, "name": "NewName"}
        )
        assert result["updated"] is True
        assert result["manifest"]["name"] == "NewName"

    @pytest.mark.asyncio
    async def test_update_tools(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "ToolBot"})
        agent_id = created["agent_id"]
        result = await _run(
            daemon,
            "agent.update",
            {
                "agent_id": agent_id,
                "tools": ["web_search", "calculator"],
            },
        )
        assert result["manifest"]["tools"] == ["web_search", "calculator"]

    @pytest.mark.asyncio
    async def test_update_not_found(self, daemon):
        result = await _run(daemon, "agent.update", {"agent_id": "nope", "name": "X"})
        assert result["status"] == "error"


class TestAgentDelete:
    @pytest.mark.asyncio
    async def test_delete_existing(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "DeleteMe"})
        agent_id = created["agent_id"]
        result = await _run(daemon, "agent.delete", {"agent_id": agent_id})
        assert result["deleted"] is True
        get_result = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert get_result["status"] == "error"

    @pytest.mark.asyncio
    async def test_delete_not_found(self, daemon):
        result = await _run(daemon, "agent.delete", {"agent_id": "nope"})
        assert result["deleted"] is True


class TestAgentConfigure:
    @pytest.mark.asyncio
    async def test_configure_model_and_temperature(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "ConfigBot"})
        agent_id = created["agent_id"]
        result = await _run(
            daemon,
            "agent.configure",
            {
                "agent_id": agent_id,
                "config": {"model": "gpt-4", "temperature": 0.3},
            },
        )
        assert result["configured"] is True
        assert result["manifest"]["model"] == "gpt-4"
        assert result["manifest"]["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_configure_empty_config(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "ConfigBot2"})
        agent_id = created["agent_id"]
        result = await _run(
            daemon, "agent.configure", {"agent_id": agent_id, "config": {}}
        )
        assert result["status"] == "error"


class TestAgentSkills:
    @pytest.mark.asyncio
    async def test_add_and_list_skills(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "SkillBot"})
        agent_id = created["agent_id"]
        await _run(
            daemon,
            "agent.add_skill",
            {
                "agent_id": agent_id,
                "skill_name": "web_search",
                "skill_def": {"type": "tool", "description": "Search the web"},
            },
        )
        result = await _run(daemon, "agent.list_skills", {"agent_id": agent_id})
        assert "web_search" in result["skills"]

    @pytest.mark.asyncio
    async def test_delete_skill(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "SkillDelBot"})
        agent_id = created["agent_id"]
        await _run(
            daemon,
            "agent.add_skill",
            {
                "agent_id": agent_id,
                "skill_name": "temp_skill",
                "skill_def": {},
            },
        )
        result = await _run(
            daemon,
            "agent.delete_skill",
            {
                "agent_id": agent_id,
                "skill_name": "temp_skill",
            },
        )
        assert result["deleted"] is True


class TestAgentSoul:
    @pytest.mark.asyncio
    async def test_get_and_update_soul(self, daemon):
        created = await _run(daemon, "agent.create", {"name": "SoulBot2"})
        agent_id = created["agent_id"]
        get_result = await _run(daemon, "agent.get_soul", {"agent_id": agent_id})
        assert isinstance(get_result["soul"], str)

        await _run(
            daemon,
            "agent.update_soul",
            {
                "agent_id": agent_id,
                "soul": "# Updated Soul\nYou are now different.",
            },
        )
        result = await _run(daemon, "agent.get_soul", {"agent_id": agent_id})
        assert "different" in result["soul"]


class TestAgentExecute:
    @pytest.mark.asyncio
    async def test_execute_without_mlx(self, daemon):
        created = await _run(
            daemon, "agent.create", {"name": "ExecBot", "model": "test-model"}
        )
        agent_id = created["agent_id"]
        result = await _run(
            daemon, "agent.execute", {"agent_id": agent_id, "input": "hello"}
        )
        assert "events" in result or "status" in result


class TestMarketplaceHandlers:
    @pytest.mark.asyncio
    async def test_search_empty(self, daemon):
        result = await _run(daemon, "marketplace.search")
        assert "entries" in result

    @pytest.mark.asyncio
    async def test_publish_and_get(self, daemon):
        pub = await _run(
            daemon,
            "marketplace.publish",
            {
                "name": "MarketBot",
                "author": "test",
                "category": "assistant",
            },
        )
        entry_id = pub["entry_id"]
        get_result = await _run(daemon, "marketplace.get", {"entry_id": entry_id})
        assert get_result["entry"]["name"] == "MarketBot"

    @pytest.mark.asyncio
    async def test_list_categories(self, daemon):
        await _run(
            daemon,
            "marketplace.publish",
            {
                "name": "CatBot",
                "category": "productivity",
            },
        )
        result = await _run(daemon, "marketplace.list_categories")
        assert "productivity" in result["categories"]

    @pytest.mark.asyncio
    async def test_unpublish(self, daemon):
        pub = await _run(daemon, "marketplace.publish", {"name": "UnpubBot"})
        entry_id = pub["entry_id"]
        result = await _run(daemon, "marketplace.unpublish", {"entry_id": entry_id})
        assert result["unpublished"] is True

    @pytest.mark.asyncio
    async def test_search_with_query(self, daemon):
        await _run(
            daemon,
            "marketplace.publish",
            {
                "name": "QueryTestBot",
                "description": "A bot for search testing",
            },
        )
        result = await _run(daemon, "marketplace.search", {"query": "QueryTest"})
        names = [e["name"] for e in result["entries"]]
        assert "QueryTestBot" in names


class TestDispatchIntegration:
    @pytest.mark.asyncio
    async def test_dispatch_agent_create(self, daemon):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agent.create",
            "params": {"name": "DispatchBot"},
        }
        result = await daemon._dispatch(msg)
        assert "result" in result
        assert result["result"]["agent_id"]

    @pytest.mark.asyncio
    async def test_dispatch_marketplace_search(self, daemon):
        msg = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "marketplace.search",
            "params": {},
        }
        result = await daemon._dispatch(msg)
        assert "result" in result

    @pytest.mark.asyncio
    async def test_dispatch_unknown_method(self, daemon):
        msg = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "agent.nonexistent",
            "params": {},
        }
        result = await daemon._dispatch(msg)
        assert "error" in result
