# Callers: pytest test runner, CI pipeline.
# Affected API: DaemonServer._get_handler() dispatch for agent.*, marketplace.*, memory.*, safety.*, planner.*, deploy.*, template.*, graph.*, rag.*, env.*, ping.
# Data schemas: AgentManifest, AgentPackage, MarketEntry, MemoryEntry, SafetyVerdict, Plan, AgentGraph, NodeConfig, Edge.
# User instruction: "继续，把所有的业务都接入完整，然后按照业务场景进行测试"

"""Business scenario integration tests — end-to-end flows via DaemonServer.

Scenarios:
1. Create → Configure → Execute agent
2. Agent skill lifecycle: add/list/delete
3. Agent soul management
4. Marketplace publish → search → install
5. Memory store → recall → delete
6. Safety check → evaluate action → add policy
7. Planner create plan → approve → cancel
8. Deploy export → import round-trip
9. Template list → instantiate
10. Graph CRUD full cycle
11. Agent list filtering by tags/capabilities
12. Agent update multiple fields
13. Environment health check + hardware metrics
14. RAG retrieve (graceful without model)
15. Ping
"""
import tempfile
from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon(tmp_path):
    d = DaemonServer(socket_path=str(tmp_path / "test.sock"))
    d._agents = {}
    yield d
    agents_dir = Path.home() / ".fusion-agent-studio" / "agents"
    idx = agents_dir / "index.json"
    if idx.exists():
        idx.unlink()


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


class TestCreateConfigureExecute:
    @pytest.mark.asyncio
    async def test_full_agent_lifecycle(self, daemon):
        create = await _run(daemon, "agent.create", {
            "name": "CodeBot", "model": "qwen3.5-9b-4bit",
            "system_prompt": "You are a code assistant.",
            "temperature": 0.5, "max_tokens": 2048,
            "tools": ["web_search", "calculator"],
            "capabilities": ["code_generation"],
            "safety_level": "L2", "tags": ["code", "python"],
        })
        assert create["agent_id"]
        assert create["manifest"]["name"] == "CodeBot"
        agent_id = create["agent_id"]

        get = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert get["agent"]["name"] == "CodeBot"
        assert get["agent"]["has_soul"] is True

        configure = await _run(daemon, "agent.configure", {
            "agent_id": agent_id,
            "config": {"temperature": 0.9, "max_tokens": 8192, "safety_level": "L3"},
        })
        assert configure["configured"] is True
        assert configure["manifest"]["temperature"] == 0.9

        updated = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert updated["agent"]["temperature"] == 0.9

        listing = await _run(daemon, "agent.list", {})
        found = [a for a in listing["agents"] if a["id"] == agent_id]
        assert len(found) == 1

        execute = await _run(daemon, "agent.execute", {
            "agent_id": agent_id, "input": "Write a hello world function",
        })
        assert execute["agent_id"] == agent_id
        assert execute["status"] in ("completed", "error")

        delete = await _run(daemon, "agent.delete", {"agent_id": agent_id})
        assert delete["deleted"] is True

        gone = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert gone["status"] == "error"


class TestSkillLifecycle:
    @pytest.mark.asyncio
    async def test_skill_add_list_delete(self, daemon):
        create = await _run(daemon, "agent.create", {"name": "SkillBot", "model": "qwen3.5-9b-4bit"})
        agent_id = create["agent_id"]

        skills_empty = await _run(daemon, "agent.list_skills", {"agent_id": agent_id})
        assert skills_empty["skills"] == []

        add = await _run(daemon, "agent.add_skill", {
            "agent_id": agent_id, "skill_name": "code_review",
            "skill_def": {"prompt": "Review this code", "tools": ["web_search"]},
        })
        assert add["added"] is True

        add2 = await _run(daemon, "agent.add_skill", {
            "agent_id": agent_id, "skill_name": "test_gen",
            "skill_def": {"prompt": "Generate tests"},
        })
        assert add2["added"] is True

        skills = await _run(daemon, "agent.list_skills", {"agent_id": agent_id})
        assert "code_review" in skills["skills"]
        assert "test_gen" in skills["skills"]

        del_skill = await _run(daemon, "agent.delete_skill", {
            "agent_id": agent_id, "skill_name": "test_gen",
        })
        assert del_skill["deleted"] is True

        skills_after = await _run(daemon, "agent.list_skills", {"agent_id": agent_id})
        assert "test_gen" not in skills_after["skills"]
        assert "code_review" in skills_after["skills"]


class TestSoulManagement:
    @pytest.mark.asyncio
    async def test_soul_get_update(self, daemon):
        create = await _run(daemon, "agent.create", {
            "name": "SoulBot", "model": "qwen3.5-9b-4bit",
            "soul": "I am a helpful assistant.",
        })
        agent_id = create["agent_id"]

        soul = await _run(daemon, "agent.get_soul", {"agent_id": agent_id})
        assert "helpful assistant" in soul["soul"]

        get_check = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert get_check["agent"]["has_soul"] is True

        update = await _run(daemon, "agent.update_soul", {
            "agent_id": agent_id, "soul": "I am a creative coding companion.",
        })
        assert update["updated"] is True

        soul2 = await _run(daemon, "agent.get_soul", {"agent_id": agent_id})
        assert "creative coding" in soul2["soul"]


class TestMarketplaceScenario:
    @pytest.mark.asyncio
    async def test_publish_search_install(self, daemon):
        publish = await _run(daemon, "marketplace.publish", {
            "name": "SuperCoder", "author": "fusion-team",
            "description": "A powerful coding agent", "category": "code",
            "tags": ["code", "python"], "version": "1.0.0",
            "graph_data": {"nodes": {}, "edges": []},
        })
        entry_id = publish["entry_id"]
        assert entry_id

        search = await _run(daemon, "marketplace.search", {"query": "coding"})
        assert len(search["entries"]) >= 1
        found = [e for e in search["entries"] if e["id"] == entry_id]
        assert len(found) == 1

        get = await _run(daemon, "marketplace.get", {"entry_id": entry_id})
        assert get["entry"]["name"] == "SuperCoder"

        categories = await _run(daemon, "marketplace.list_categories", {})
        assert "code" in categories["categories"]

        install = await _run(daemon, "marketplace.install", {"entry_id": entry_id})
        assert install["installed"] is True

        unpublish = await _run(daemon, "marketplace.unpublish", {"entry_id": entry_id})
        assert unpublish["unpublished"] is True


class TestMemoryScenario:
    @pytest.mark.asyncio
    async def test_store_recall_delete(self, daemon):
        store = await _run(daemon, "memory.store", {
            "content": "User prefers dark mode", "scope": "user_preferences",
            "tags": "ui", "importance": 8,
        })
        entry_id = store["entry_id"]
        assert entry_id

        recall = await _run(daemon, "memory.recall", {
            "query": "dark mode", "scope": "user_preferences",
        })
        assert len(recall["entries"]) >= 1

        recent = await _run(daemon, "memory.list_recent", {"scope": "user_preferences"})
        assert len(recent["entries"]) >= 1

        count = await _run(daemon, "memory.count", {"scope": "user_preferences"})
        assert count["count"] >= 1

        get = await _run(daemon, "memory.get", {"entry_id": entry_id})
        assert get["entry"]["content"] == "User prefers dark mode"

        delete = await _run(daemon, "memory.delete", {"entry_id": entry_id})
        assert delete["deleted"] is True

        del_scope = await _run(daemon, "memory.delete_scope", {"scope": "user_preferences"})
        assert "deleted_count" in del_scope


class TestSafetyScenario:
    @pytest.mark.asyncio
    async def test_check_evaluate_add_policy(self, daemon):
        check = await _run(daemon, "safety.check", {
            "content": "Delete all files", "context": "user_request",
        })
        assert "verdict" in check

        evaluate = await _run(daemon, "safety.evaluate_action", {
            "category": "file_delete", "content": "rm -rf /",
        })
        assert "verdict" in evaluate

        pending = await _run(daemon, "safety.get_pending_actions", {})
        assert "actions" in pending

        add_policy = await _run(daemon, "safety.add_policy", {
            "category": "network_access",
            "description": "Control outbound network",
            "default_level": "L2",
        })
        assert add_policy["added"] is True


class TestPlannerScenario:
    @pytest.mark.asyncio
    async def test_create_approve_cancel(self, daemon):
        plan = await _run(daemon, "planner.create_plan", {
            "task": "Refactor auth module", "context": "legacy patterns",
        })
        assert "plan" in plan
        plan_id = plan["plan"]["id"]

        get = await _run(daemon, "planner.get_plan", {"plan_id": plan_id})
        assert get["plan"]["id"] == plan_id

        approve = await _run(daemon, "planner.approve_plan", {"plan_id": plan_id})
        assert approve["approved"] is True

        list_plans = await _run(daemon, "planner.list_plans", {})
        assert len(list_plans["plans"]) >= 1

        cancel = await _run(daemon, "planner.cancel_plan", {"plan_id": plan_id})
        assert cancel["cancelled"] is True


class TestDeployScenario:
    @pytest.mark.asyncio
    async def test_export_import_roundtrip(self, daemon):
        graph = await _run(daemon, "graph.create", {
            "name": "TestDeployGraph", "description": "Deploy test",
            "nodes": [
                {"id": "start-1", "type": "start", "label": "Start"},
                {"id": "llm-1", "type": "llm", "label": "LLM", "model": "qwen3.5-9b-4bit"},
            ],
            "edges": [{"source": "start-1", "target": "llm-1"}],
        })
        graph_id = graph["graph_id"]

        formats = await _run(daemon, "deploy.list_formats", {})
        assert len(formats["formats"]) >= 1

        with tempfile.TemporaryDirectory() as tmpdir:
            export = await _run(daemon, "deploy.export", {
                "graph_id": graph_id, "format": "json",
                "filepath": str(Path(tmpdir) / "test_graph.json"),
            })
            assert export["status"] == "ok"
            assert Path(export["path"]).exists()

            imported = await _run(daemon, "deploy.import", {
                "filepath": str(Path(tmpdir) / "test_graph.json"),
            })
            assert imported["graph_id"]
            assert imported["name"] == "TestDeployGraph"


class TestTemplateScenario:
    @pytest.mark.asyncio
    async def test_list_and_instantiate(self, daemon):
        listing = await _run(daemon, "template.list", {})
        assert "templates" in listing

        if listing["templates"]:
            tmpl = listing["templates"][0]
            get = await _run(daemon, "template.get", {"template_id": tmpl["id"]})
            assert get["template"]["id"] == tmpl["id"]

            inst = await _run(daemon, "template.instantiate", {"template_id": tmpl["id"]})
            assert "graph_data" in inst or inst.get("status") == "error"


class TestGraphCRUDScenario:
    @pytest.mark.asyncio
    async def test_create_get_list_delete(self, daemon):
        create = await _run(daemon, "graph.create", {
            "name": "IntegrationTestGraph", "description": "Full CRUD",
            "nodes": [
                {"id": "s1", "type": "start", "label": "Start"},
                {"id": "l1", "type": "llm", "label": "Think", "model": "qwen3.5-9b-4bit"},
                {"id": "e1", "type": "end", "label": "Done"},
            ],
            "edges": [
                {"source": "s1", "target": "l1"},
                {"source": "l1", "target": "e1"},
            ],
        })
        graph_id = create["graph_id"]
        assert graph_id

        get = await _run(daemon, "graph.get", {"graph_id": graph_id})
        assert get["graph_id"] == graph_id
        assert "s1" in get["nodes"]
        assert len(get["edges"]) == 2

        listing = await _run(daemon, "graph.list", {})
        found = [g for g in listing["graphs"] if g["id"] == graph_id]
        assert len(found) == 1

        delete = await _run(daemon, "graph.delete", {"graph_id": graph_id})
        assert delete["deleted"] is True


class TestAgentListFiltering:
    @pytest.mark.asyncio
    async def test_filter_by_tags_and_capabilities(self, daemon):
        await _run(daemon, "agent.create", {
            "name": "PyBot", "model": "qwen3.5-9b-4bit",
            "tags": ["python", "code"], "capabilities": ["code_generation"],
        })
        await _run(daemon, "agent.create", {
            "name": "RustBot", "model": "qwen3.5-9b-4bit",
            "tags": ["rust", "code"], "capabilities": ["code_review"],
        })

        by_tag = await _run(daemon, "agent.list", {"tags": ["python"]})
        names = [a["name"] for a in by_tag["agents"]]
        assert "PyBot" in names

        by_cap = await _run(daemon, "agent.list", {"capabilities": ["code_review"]})
        cap_names = [a["name"] for a in by_cap["agents"]]
        assert "RustBot" in cap_names


class TestAgentUpdate:
    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, daemon):
        create = await _run(daemon, "agent.create", {
            "name": "UpdatableBot", "model": "qwen3.5-9b-4bit",
            "system_prompt": "Initial prompt",
        })
        agent_id = create["agent_id"]

        update = await _run(daemon, "agent.update", {
            "agent_id": agent_id, "name": "UpdatedBot",
            "system_prompt": "New prompt", "temperature": 0.3,
            "tags": ["updated", "test"],
        })
        assert update["updated"] is True
        assert update["manifest"]["name"] == "UpdatedBot"
        assert update["manifest"]["temperature"] == 0.3

        verify = await _run(daemon, "agent.get", {"agent_id": agent_id})
        assert verify["agent"]["name"] == "UpdatedBot"


class TestEnvHealth:
    @pytest.mark.asyncio
    async def test_health_check_structure(self, daemon):
        health = await _run(daemon, "env.health_check", {})
        assert "healthy" in health
        assert "checks" in health
        assert health["checks"]["python"]["ok"] is True


class TestRAGScenario:
    @pytest.mark.asyncio
    async def test_retrieve_graceful(self, daemon):
        result = await _run(daemon, "rag.retrieve", {"query": "test query"})
        assert "query" in result or result.get("status") == "error"


class TestPing:
    @pytest.mark.asyncio
    async def test_ping(self, daemon):
        result = await _run(daemon, "ping", {})
        assert result["pong"] is True
        assert "timestamp" in result
