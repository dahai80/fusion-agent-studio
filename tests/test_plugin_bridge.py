"""Tests for plugin_bridge — BaseTool → PluginManifest adaptation."""

import pytest

pytest.importorskip("fusion_plugins_ecosystem")

from agent_runtime.plugin_bridge import (
    build_manifests,
    build_registry,
    gateway_info,
    invoke_tool,
    list_mcp_tools,
)
from tools import create_default_registry


@pytest.fixture
def registry():
    return create_default_registry()


class TestBuildManifests:
    def test_all_tools_have_manifests(self, registry):
        manifests = build_manifests(registry._tools)
        assert len(manifests) == registry.count

    def test_manifest_fields(self, registry):
        manifests = build_manifests(registry._tools)
        for m in manifests:
            assert m.id.startswith("agent_")
            assert m.version == "0.1.0"
            assert m.capabilities
            assert m.default_mounted

    def test_manifest_id_matches_tool_name(self, registry):
        manifests = build_manifests(registry._tools)
        ids = {m.id for m in manifests}
        for name in registry._tools:
            assert f"agent_{name}" in ids


class TestBuildRegistry:
    def test_registry_has_all_tools(self, registry):
        plugin_reg = build_registry(registry._tools)
        manifests = plugin_reg.list()
        assert len(manifests) == registry.count

    def test_registry_get_by_id(self, registry):
        plugin_reg = build_registry(registry._tools)
        m = plugin_reg.get("agent_file_read")
        assert m is not None


class TestListMcpTools:
    def test_mcp_tool_format(self, registry):
        tools = list_mcp_tools(registry._tools)
        assert len(tools) == registry.count
        for t in tools:
            assert "name" in t
            assert t["name"].startswith("mcp__plugin__agent_")
            assert "inputSchema" in t

    def test_mcp_tool_has_file_read(self, registry):
        tools = list_mcp_tools(registry._tools)
        names = [t["name"] for t in tools]
        assert any("file_read" in n for n in names)


class TestGatewayInfo:
    def test_gateway_info_keys(self, registry):
        info = gateway_info(registry._tools)
        assert isinstance(info, dict)


class TestInvokeTool:
    @pytest.mark.asyncio
    async def test_invoke_success(self, registry):
        tool = registry._tools["file_read"]
        result = await invoke_tool(tool, {"path": "/etc/hostname"})
        assert result["success"] is True
        assert "result" in result

    @pytest.mark.asyncio
    async def test_invoke_missing_param(self, registry):
        tool = registry._tools["file_read"]
        result = await invoke_tool(tool, {})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_invoke_nonexistent_tool(self, registry):
        tool = registry._tools.get("nonexistent")
        assert tool is None
