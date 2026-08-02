"""Tests for fusion_code_bridge, api_server, agent_templates."""

from __future__ import annotations

import asyncio
import pytest

from agent_runtime.fusion_code_bridge import FusionCodeBridge, CodeTask, CodeResult
from agent_runtime.agent_templates import (
    AgentTemplate,
    TEMPLATES,
    list_templates,
    get_template,
    instantiate_template,
)
from agent_runtime.api_server import app


# ── FusionCodeBridge ──────────────────────────────────────


class TestFusionCodeBridge:
    def test_default_binary_path(self):
        bridge = FusionCodeBridge()
        assert "fusion-code" in bridge.binary_path

    def test_custom_binary_path(self):
        bridge = FusionCodeBridge(binary_path="/usr/local/bin/fc")
        assert bridge.binary_path == "/usr/local/bin/fc"

    def test_is_available_nonexistent(self):
        bridge = FusionCodeBridge(binary_path="/nonexistent/binary")
        assert bridge.is_available() is False

    def test_build_command_basic(self):
        bridge = FusionCodeBridge()
        task = CodeTask(prompt="hello world")
        cmd = bridge._build_command(task)
        assert "--print" in cmd
        assert "hello world" in cmd

    def test_build_command_with_model(self):
        bridge = FusionCodeBridge()
        task = CodeTask(prompt="test", model="qwen3-0.6b")
        cmd = bridge._build_command(task)
        assert "--model" in cmd
        assert "qwen3-0.6b" in cmd

    def test_build_command_with_extra_args(self):
        bridge = FusionCodeBridge()
        task = CodeTask(prompt="test", extra_args=["--verbose", "--no-confirm"])
        cmd = bridge._build_command(task)
        assert "--verbose" in cmd
        assert "--no-confirm" in cmd

    @pytest.mark.asyncio
    async def test_execute_nonexistent_binary(self):
        bridge = FusionCodeBridge(binary_path="/nonexistent/binary")
        task = CodeTask(prompt="test", timeout=5.0)
        result = await bridge.execute(task)
        assert result.exit_code != 0
        assert result.error

    @pytest.mark.asyncio
    async def test_cancel_no_process(self):
        bridge = FusionCodeBridge()
        await bridge.cancel()  # Should not raise

    @pytest.mark.asyncio
    async def test_execute_echo_binary(self):
        bridge = FusionCodeBridge(binary_path="/bin/echo")
        task = CodeTask(prompt="hello", timeout=5.0)
        result = await bridge.execute(task)
        assert result.exit_code == 0
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        bridge = FusionCodeBridge(binary_path="/bin/sleep")
        task = CodeTask(prompt="30", timeout=0.5, extra_args=[])
        bridge._build_command = lambda t: ["/bin/sleep", "30"]
        result = await bridge.execute(task)
        assert result.exit_code == -1
        assert "Timeout" in result.error

    @pytest.mark.asyncio
    async def test_execute_stream_echo(self):
        bridge = FusionCodeBridge(binary_path="/bin/echo")
        task = CodeTask(prompt="stream-test", timeout=5.0)
        lines = []
        async for line in bridge.execute_stream(task):
            lines.append(line)
        assert len(lines) >= 1
        assert any("stream-test" in line for line in lines)

    @pytest.mark.asyncio
    async def test_cancel_running_process(self):
        bridge = FusionCodeBridge(binary_path="/bin/sleep")
        task = CodeTask(prompt="10", timeout=30.0)

        async def _run_and_cancel():
            execute_task = asyncio.create_task(bridge.execute(task))
            await asyncio.sleep(0.3)
            await bridge.cancel()
            return await execute_task

        result = await _run_and_cancel()
        assert result is not None

    @pytest.mark.asyncio
    async def test_stream_nonexistent_binary(self):
        bridge = FusionCodeBridge(binary_path="/nonexistent/binary")
        task = CodeTask(prompt="test", timeout=5.0)
        lines = []
        async for line in bridge.execute_stream(task):
            lines.append(line)
        assert len(lines) == 0

    def test_is_available_echo(self):
        bridge = FusionCodeBridge(binary_path="/bin/echo")
        assert bridge.is_available() is True

    @pytest.mark.asyncio
    async def test_execute_with_working_dir(self):
        bridge = FusionCodeBridge(binary_path="/bin/echo")
        task = CodeTask(prompt="test", working_dir="/tmp", timeout=5.0)
        result = await bridge.execute(task)
        assert result.exit_code == 0


class TestCodeTask:
    def test_defaults(self):
        task = CodeTask(prompt="hello")
        assert task.working_dir == ""
        assert task.timeout == 300.0
        assert task.model == ""
        assert task.extra_args == []


class TestCodeResult:
    def test_defaults(self):
        result = CodeResult(output="ok", exit_code=0)
        assert result.output == "ok"
        assert result.exit_code == 0
        assert result.duration == 0.0
        assert result.error == ""


# ── Agent Templates ───────────────────────────────────────


class TestAgentTemplates:
    def test_all_8_templates_registered(self):
        assert len(TEMPLATES) == 8

    def test_template_ids(self):
        expected = {
            "simple-chat",
            "code-reviewer",
            "research-assistant",
            "tool-agent",
            "pipeline",
            "code-generator",
            "data-analyst",
            "multi-agent-handoff",
        }
        assert set(TEMPLATES.keys()) == expected

    def test_list_templates_all(self):
        templates = list_templates()
        assert len(templates) == 8

    def test_list_templates_by_category(self):
        basic = list_templates(category="basic")
        assert len(basic) == 1
        assert basic[0].id == "simple-chat"

    def test_get_template(self):
        tmpl = get_template("simple-chat")
        assert tmpl is not None
        assert tmpl.name == "Simple Chat"

    def test_get_template_not_found(self):
        assert get_template("nonexistent") is None

    def test_instantiate_simple_chat(self):
        graph = instantiate_template("simple-chat", {"system_prompt": "Be concise."})
        assert "nodes" in graph
        for node in graph["nodes"]:
            if node["type"] == "llm":
                assert "Be concise." in node["config"]["prompt"]

    def test_instantiate_with_default_vars(self):
        graph = instantiate_template("simple-chat")
        assert "nodes" in graph

    def test_instantiate_not_found(self):
        result = instantiate_template("nonexistent")
        assert result == {}

    def test_template_categories(self):
        categories = {t.category for t in TEMPLATES.values()}
        assert "basic" in categories
        assert "development" in categories
        assert "research" in categories
        assert "advanced" in categories
        assert "multi-agent" in categories

    def test_template_to_dict(self):
        tmpl = get_template("simple-chat")
        d = tmpl.to_dict()
        assert d["id"] == "simple-chat"
        assert "graph_data" in d

    def test_template_from_dict(self):
        tmpl = get_template("simple-chat")
        d = tmpl.to_dict()
        restored = AgentTemplate.from_dict(d)
        assert restored.id == tmpl.id
        assert restored.name == tmpl.name

    def test_instantiate_code_generator(self):
        graph = instantiate_template(
            "code-generator", {"language": "rust", "style_guide": "Rust API Guidelines"}
        )
        assert "nodes" in graph
        llm_nodes = [n for n in graph["nodes"] if n["type"] == "llm"]
        assert any("rust" in n["config"]["prompt"] for n in llm_nodes)

    def test_instantiate_pipeline(self):
        graph = instantiate_template(
            "pipeline",
            {"analysis_prompt": "Deep analyze", "transform_prompt": "Rewrite"},
        )
        llm_nodes = [n for n in graph["nodes"] if n["type"] == "llm"]
        assert len(llm_nodes) == 3


# ── API Server ────────────────────────────────────────────


class TestAPIServer:
    @pytest.fixture
    def client(self):
        from httpx import AsyncClient, ASGITransport

        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_health(self, client):
        async with client as c:
            resp = await c.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_create_graph(self, client):
        async with client as c:
            resp = await c.post(
                "/graphs",
                json={
                    "name": "test-graph",
                    "description": "A test",
                    "graph_data": {"nodes": [], "edges": []},
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "test-graph"
            assert data["graph_id"]

    @pytest.mark.asyncio
    async def test_list_graphs(self, client):
        async with client as c:
            await c.post(
                "/graphs",
                json={
                    "name": "g1",
                    "description": "",
                    "graph_data": {"nodes": [], "edges": []},
                },
            )
            resp = await c.get("/graphs")
            assert resp.status_code == 200
            assert len(resp.json()) >= 1

    @pytest.mark.asyncio
    async def test_get_graph_not_found(self, client):
        async with client as c:
            resp = await c.get("/graphs/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_graph(self, client):
        async with client as c:
            create_resp = await c.post(
                "/graphs",
                json={
                    "name": "to-delete",
                    "description": "",
                    "graph_data": {"nodes": [], "edges": []},
                },
            )
            gid = create_resp.json()["graph_id"]
            resp = await c.delete(f"/graphs/{gid}")
            assert resp.status_code == 200
            resp2 = await c.get(f"/graphs/{gid}")
            assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_graph_not_found(self, client):
        async with client as c:
            resp = await c.delete("/graphs/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_graph_existing(self, client):
        async with client as c:
            create_resp = await c.post(
                "/graphs",
                json={
                    "name": "fetch-me",
                    "description": "test desc",
                    "graph_data": {"nodes": [], "edges": []},
                },
            )
            gid = create_resp.json()["graph_id"]
            resp = await c.get(f"/graphs/{gid}")
            assert resp.status_code == 200
            assert resp.json()["name"] == "fetch-me"

    @pytest.mark.asyncio
    async def test_execute_graph_not_found(self, client):
        async with client as c:
            resp = await c.post(
                "/graphs/nonexistent/execute",
                json={
                    "graph_id": "nonexistent",
                    "input_text": "test",
                },
            )
            assert resp.status_code == 404
