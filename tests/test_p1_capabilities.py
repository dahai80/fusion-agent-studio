"""Tests for P1 capabilities: plugin, debugger, variable, schema, prompt, subgraph, deploy."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_runtime.variable_manager import VariableManager
from agent_runtime.debugger import StepDebugger, DebuggerState
from agent_runtime.json_schema import JsonSchemaValidator
from agent_runtime.prompt_templates import (
    PromptTemplate,
    PromptTemplateManager,
    register_default_prompt_templates,
)
from agent_runtime.sub_graph import SubGraphNode, SubGraphRegistry
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.deployer import GraphDeployer
from tools.plugin_manager import PluginManager
from tools.registry import ToolRegistry


# ── VariableManager ──


class TestVariableManager:
    def test_set_get(self):
        vm = VariableManager()
        vm.set("name", "hello")
        assert vm.get("name") == "hello"

    def test_default_value(self):
        vm = VariableManager()
        assert vm.get("nonexistent", "default") == "default"

    def test_nested_access(self):
        vm = VariableManager()
        vm.set("data", {"items": [{"name": "test"}]})
        assert vm.get("data.items.0.name") == "test"

    def test_coerce_number(self):
        vm = VariableManager()
        vm.set("val", "42", coerce="number")
        assert vm.get("val") == 42.0

    def test_coerce_integer(self):
        vm = VariableManager()
        vm.set("val", "42", coerce="integer")
        assert isinstance(vm.get("val"), int)

    def test_coerce_boolean(self):
        vm = VariableManager()
        vm.set("flag", "true", coerce="boolean")
        assert vm.get("flag") is True

    def test_coerce_json(self):
        vm = VariableManager()
        vm.set("data", '{"a": 1}', coerce="json")
        assert vm.get("data") == {"a": 1}

    def test_interpolate(self):
        vm = VariableManager()
        vm.set("name", "world")
        result = vm.interpolate("Hello {{ name }}!")
        assert result == "Hello world!"

    def test_delete(self):
        vm = VariableManager()
        vm.set("x", 1)
        vm.delete("x")
        assert vm.get("x") == ""

    def test_clear(self):
        vm = VariableManager()
        vm.set("x", 1)
        vm.set("y", 2)
        vm.clear()
        assert vm.count == 0

    def test_keys(self):
        vm = VariableManager()
        vm.set("a", 1)
        vm.set("b", 2)
        assert sorted(vm.keys()) == ["a", "b"]

    def test_to_dict(self):
        vm = VariableManager()
        vm.set("x", 10)
        assert vm.to_dict() == {"x": 10}

    def test_load_from(self):
        vm = VariableManager()
        vm.load_from({"a": 1, "b": 2})
        assert vm.get("a") == 1
        assert vm.get("b") == 2


# ── StepDebugger ──


class TestStepDebugger:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        d = StepDebugger()
        assert d.state == DebuggerState.RUNNING

    @pytest.mark.asyncio
    async def test_pause_resume(self):
        d = StepDebugger()
        await d.pause()
        assert d.state == DebuggerState.PAUSED
        await d.resume()
        assert d.state == DebuggerState.RUNNING

    @pytest.mark.asyncio
    async def test_breakpoint(self):
        d = StepDebugger()
        d.add_breakpoint("node_1")
        assert d.has_breakpoint("node_1") is True
        d.remove_breakpoint("node_1")
        assert d.has_breakpoint("node_1") is False

    @pytest.mark.asyncio
    async def test_step_over(self):
        d = StepDebugger()
        await d.step_over()
        assert d.state == DebuggerState.STEP_OVER

    @pytest.mark.asyncio
    async def test_stop(self):
        d = StepDebugger()
        d.stop()
        assert d.state == DebuggerState.STOPPED

    @pytest.mark.asyncio
    async def test_next_event(self):
        d = StepDebugger()
        await d.pause()
        event = await d.next_event()
        assert event.type == "pause"


# ── JsonSchemaValidator ──


class TestJsonSchemaValidator:
    def test_empty_schema(self):
        v = JsonSchemaValidator()
        assert v.is_empty is True

    def test_validate_required_fields(self):
        v = JsonSchemaValidator(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        )
        errors = v.validate({"name": "test"})
        assert len(errors) == 0

    def test_validate_missing_required(self):
        v = JsonSchemaValidator(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        )
        errors = v.validate({})
        assert len(errors) >= 1

    def test_validate_type_mismatch(self):
        v = JsonSchemaValidator(
            {
                "type": "object",
                "required": [],
                "properties": {"age": {"type": "integer"}},
            }
        )
        errors = v.validate({"age": "not_a_number"})
        assert len(errors) >= 1

    def test_coerce_types(self):
        v = JsonSchemaValidator(
            {
                "type": "object",
                "properties": {"age": {"type": "integer"}, "name": {"type": "string"}},
            }
        )
        result = v.coerce({"age": "42", "name": "test"})
        assert isinstance(result["age"], int)
        assert isinstance(result["name"], str)

    def test_coerce_defaults(self):
        v = JsonSchemaValidator(
            {"type": "object", "properties": {"x": {"type": "integer", "default": 10}}}
        )
        result = v.coerce({})
        assert result["x"] == 10

    def test_extract_from_code_block(self):
        v = JsonSchemaValidator({})
        result = v.extract_from_text('```json\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_extract_from_plain(self):
        v = JsonSchemaValidator({})
        result = v.extract_from_text('{"a": 1}')
        assert result == {"a": 1}

    def test_extract_none(self):
        v = JsonSchemaValidator({})
        result = v.extract_from_text("just some text")
        assert result is None

    def test_to_instruction(self):
        v = JsonSchemaValidator(
            {"type": "object", "properties": {"name": {"type": "string"}}}
        )
        instruction = v.to_instruction()
        assert "name" in instruction
        assert "string" in instruction


# ── PromptTemplate ──


class TestPromptTemplate:
    def test_render(self):
        t = PromptTemplate(
            name="test",
            template="Hello {{ name }}!",
            variables={"name": {"type": "string"}},
        )
        result = t.render(name="World")
        assert result == "Hello World!"

    def test_render_default(self):
        t = PromptTemplate(
            name="test",
            template="Hello {{ name }}!",
            variables={"name": {"type": "string", "default": "World"}},
        )
        result = t.render()
        assert result == "Hello World!"

    def test_validate(self):
        t = PromptTemplate(
            name="test",
            template="{{ x }}",
            variables={"x": {"type": "string", "required": True}},
        )
        errors = t.validate()
        assert len(errors) >= 1
        errors = t.validate(x="ok")
        assert len(errors) == 0

    def test_to_dict(self):
        t = PromptTemplate(
            name="test", template="Hello", description="A test", category="general"
        )
        d = t.to_dict()
        assert d["name"] == "test"


class TestPromptTemplateManager:
    def test_register_and_get(self):
        m = PromptTemplateManager()
        t = PromptTemplate(name="test", template="Hello {{ name }}!")
        m.register(t)
        assert m.get("test").name == "test"

    def test_get_nonexistent(self):
        m = PromptTemplateManager()
        with pytest.raises(KeyError):
            m.get("nonexistent")

    def test_render(self):
        m = PromptTemplateManager()
        m.register(
            PromptTemplate(
                name="greet",
                template="Hi {{ username }}!",
                variables={"username": {"type": "string"}},
            )
        )
        result = m.render("greet", username="Alice")
        assert result == "Hi Alice!"

    def test_list(self):
        m = PromptTemplateManager()
        m.register(PromptTemplate(name="a", template="x", category="cat1"))
        m.register(PromptTemplate(name="b", template="y", category="cat2"))
        assert len(m.list()) == 2
        assert len(m.list(category="cat1")) == 1

    def test_register_defaults(self):
        m = PromptTemplateManager()
        register_default_prompt_templates(m)
        assert len(m.list()) >= 5


# ── SubGraph ──


class TestSubGraph:
    def test_to_node_config(self):
        g = AgentGraph(name="sub", description="a sub graph")
        g.add_node("start", NodeConfig(type="start"))
        g.add_node("end", NodeConfig(type="end"))
        g.add_edge("start", "end")
        sg = SubGraphNode(g, input_mapping={"a": "b"})
        config = sg.to_node_config("sub_1", "My Sub")
        assert config.type == "tool"
        assert config.tool_name == "__sub_graph__"
        assert config.tool_params["graph_id"] == g.id

    def test_registry(self):
        r = SubGraphRegistry()
        g = AgentGraph(name="test")
        r.register(g)
        assert r.get(g.id).name == "test"
        assert r.count == 1

    def test_registry_nonexistent(self):
        r = SubGraphRegistry()
        with pytest.raises(KeyError):
            r.get("nonexistent")


# ── GraphDeployer ──


class TestGraphDeployer:
    def test_export_import_json(self):
        g = AgentGraph(name="Test", description="test graph")
        g.add_node("start", NodeConfig(type="start"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = GraphDeployer.export_as_json(g, Path(tmpdir) / "test.json")
            loaded = GraphDeployer.import_from_json(path)
            assert loaded.name == "Test"
            assert loaded.description == "test graph"

    def test_export_python(self):
        g = AgentGraph.create_default("Test")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = GraphDeployer.export_as_python(g, Path(tmpdir) / "test.py")
            assert path.exists()
            content = path.read_text()
            assert "Test" in content
            assert "async def main" in content

    def test_export_yaml(self):
        g = AgentGraph.create_default("Test")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = GraphDeployer.export_as_yaml(g, Path(tmpdir) / "test.yaml")
            assert path.exists()
            content = path.read_text()
            assert "Test" in content

    def test_export_fastapi(self):
        g = AgentGraph.create_default("Test")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = GraphDeployer.export_as_fastapi(g, Path(tmpdir) / "server.py")
            assert path.exists()
            content = path.read_text()
            assert "FastAPI" in content
            assert "run_agent" in content

    def test_import_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            GraphDeployer.import_from_json("/nonexistent/file.json")

    def test_list_formats(self):
        formats = GraphDeployer.list_formats()
        names = [f["name"] for f in formats]
        assert "json" in names
        assert "python" in names
        assert "yaml" in names
        assert "fastapi" in names


# ── PluginManager ──


class TestPluginManager:
    def test_init(self):
        reg = ToolRegistry()
        pm = PluginManager(reg)
        assert pm.loaded_count == 0
        assert pm.plugin_dir.exists()

    def test_discover_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = ToolRegistry()
            pm = PluginManager(reg, plugin_dir=tmpdir)
            plugins = pm.discover()
            assert len(plugins) == 0

    def test_create_plugin_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = ToolRegistry()
            pm = PluginManager(reg, plugin_dir=tmpdir)
            path = pm.create_plugin_template("my_tool", "A custom tool")
            assert path.exists()
            content = path.read_text()
            assert "my_tool" in content
            assert "My_toolTool" in content

    def test_load_nonexistent_plugin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = ToolRegistry()
            pm = PluginManager(reg, plugin_dir=tmpdir)
            result = pm.load_plugin("nonexistent")
            assert result is None
