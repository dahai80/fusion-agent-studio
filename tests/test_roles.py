"""Tests for M3-1 issue#322: role-based tool filtering on BaseTool + ToolRegistry."""

from __future__ import annotations

from tools.base import BaseTool
from tools.registry import ToolRegistry


class _ResearcherTool(BaseTool):
    name = "research_tool"
    description = "research only"
    parameters = {}
    roles = frozenset({"researcher"})

    async def execute(self, **kwargs):
        return "ok"


class _WriterTool(BaseTool):
    name = "writer_tool"
    description = "writer only"
    parameters = {}
    roles = frozenset({"writer"})

    async def execute(self, **kwargs):
        return "ok"


class _SharedTool(BaseTool):
    name = "shared_tool"
    description = "shared"
    parameters = {}
    roles = frozenset({"researcher", "writer"})

    async def execute(self, **kwargs):
        return "ok"


class _NoRolesTool(BaseTool):
    name = "legacy_tool"
    description = "no roles attr set"
    parameters = {}

    async def execute(self, **kwargs):
        return "ok"


def test_base_tool_default_roles_all():
    # Tools without explicit roles default to frozenset({"all"}).
    assert _NoRolesTool.roles == frozenset({"all"})


def test_registry_filter_by_role():
    reg = ToolRegistry()
    reg.register(_ResearcherTool())
    reg.register(_WriterTool())
    reg.register(_SharedTool())

    researcher = reg.tools_for_role("researcher")
    assert "research_tool" in researcher
    assert "shared_tool" in researcher
    assert "writer_tool" not in researcher

    writer = reg.tools_for_role("writer")
    assert "writer_tool" in writer
    assert "shared_tool" in writer
    assert "research_tool" not in writer


def test_registry_all_role_sees_everything():
    reg = ToolRegistry()
    reg.register(_ResearcherTool())
    reg.register(_WriterTool())
    reg.register(_NoRolesTool())

    all_tools = reg.tools_for_role("all")
    assert set(all_tools) == {"research_tool", "writer_tool", "legacy_tool"}


def test_registry_shared_role():
    reg = ToolRegistry()
    reg.register(_SharedTool())

    assert "shared_tool" in reg.tools_for_role("researcher")
    assert "shared_tool" in reg.tools_for_role("writer")
    assert "shared_tool" not in reg.tools_for_role("imager")


def test_to_openai_schemas_role_filter():
    reg = ToolRegistry()
    reg.register(_ResearcherTool())
    reg.register(_WriterTool())
    reg.register(_NoRolesTool())

    researcher_schemas = reg.to_openai_schemas(role="researcher")
    researcher_names = {s["function"]["name"] for s in researcher_schemas}
    assert "research_tool" in researcher_names
    assert "legacy_tool" in researcher_names  # "all" default visible
    assert "writer_tool" not in researcher_names


def test_to_openai_schemas_default_all():
    reg = ToolRegistry()
    reg.register(_ResearcherTool())
    reg.register(_WriterTool())

    # Default role="all" returns everything (backward compat).
    schemas = reg.to_openai_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == {"research_tool", "writer_tool"}


def test_list_tools_role_filter():
    reg = ToolRegistry()
    reg.register(_ResearcherTool())
    reg.register(_WriterTool())

    researcher_list = reg.list_tools(role="researcher")
    researcher_names = {t["name"] for t in researcher_list}
    assert "research_tool" in researcher_names
    assert "writer_tool" not in researcher_names


def test_agent_context_role_default():
    from agent_runtime.context import AgentContext

    ctx = AgentContext()
    assert ctx.role == "all"


def test_agent_context_role_set():
    from agent_runtime.context import AgentContext

    ctx = AgentContext(role="researcher")
    assert ctx.role == "researcher"


def test_default_registry_backward_compat():
    # Existing built-in tools (no roles attr) must remain visible under "all".
    from tools import create_default_registry

    reg = create_default_registry()
    all_tools = reg.to_openai_schemas(role="all")
    assert len(all_tools) > 0
    # Every built-in tool without explicit roles should have "all" in roles.
    for tool in reg._tools.values():
        assert "all" in tool.roles or len(tool.roles - {"all"}) > 0
