"""Plugin bridge — adapt agent-studio BaseTool → fusion-plugins-ecosystem PluginManifest.

Importers: daemon_server.py (plugin.* RPC), tests/test_plugin_bridge.py
API: build_manifests(), build_registry(), invoke_tool()
Data schemas: PluginManifest (from fusion_plugins_ecosystem), BaseTool.openai_schema()
"""

from __future__ import annotations

import logging
from typing import Any

from tools.base import BaseTool

logger = logging.getLogger(__name__)


def _param_type_from_json(json_type: str):
    from fusion_plugins_ecosystem.registry import PluginParamType

    mapping = {
        "string": PluginParamType.STRING,
        "integer": PluginParamType.INT,
        "number": PluginParamType.FLOAT,
        "boolean": PluginParamType.BOOL,
        "array": PluginParamType.ARRAY,
        "object": PluginParamType.OBJECT,
    }
    return mapping.get(json_type, PluginParamType.STRING)


def _tool_to_manifest(tool: BaseTool) -> Any:
    from fusion_plugins_ecosystem.registry import (
        PluginCapability,
        PluginCategory,
        PluginManifest,
        PluginParam,
    )

    params = []
    schema = tool.openai_schema()
    func_def = schema.get("function", {})
    props = func_def.get("parameters", {}).get("properties", {})
    required = set(func_def.get("parameters", {}).get("required", []))

    for pname, pdef in props.items():
        ptype = _param_type_from_json(pdef.get("type", "string"))
        enum_vals = tuple(pdef.get("enum", [])) if "enum" in pdef else None
        params.append(
            PluginParam(
                name=pname,
                type=ptype,
                description=pdef.get("description", ""),
                required=pname in required,
                default=pdef.get("default"),
                enum=enum_vals,
            )
        )

    return PluginManifest(
        id=f"agent_{tool.name}",
        name=func_def.get("description", tool.description) or tool.name,
        version="0.1.0",
        category=PluginCategory.CUSTOM,
        description=tool.description or tool.name,
        capabilities=(PluginCapability.MCP_TOOL,),
        params=tuple(params),
        entry_point=None,
        default_mounted=True,
        timeout_seconds=60,
    )


def build_manifests(tools: dict[str, BaseTool]) -> list[Any]:
    return [_tool_to_manifest(t) for t in tools.values()]


def build_registry(tools: dict[str, BaseTool]) -> Any:
    from fusion_plugins_ecosystem.registry import PluginRegistry

    registry = PluginRegistry()
    for manifest in build_manifests(tools):
        registry.register(manifest)
    return registry


async def invoke_tool(
    tool: BaseTool,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = await tool.execute(**arguments)
        return {"success": True, "result": result}
    except (ValueError, TypeError, OSError, RuntimeError) as e:
        logger.error("plugin_bridge invoke_tool %s failed: %s", tool.name, e)
        return {"success": False, "error": str(e)}


def list_mcp_tools(tools: dict[str, BaseTool]) -> list[dict[str, Any]]:
    from fusion_plugins_ecosystem.mcp_exporter import MCPExporter

    registry = build_registry(tools)
    exporter = MCPExporter(registry)
    return exporter.list_tools()


def gateway_info(tools: dict[str, BaseTool]) -> dict[str, Any]:
    from fusion_plugins_ecosystem.claude_gateway import ClaudeGateway

    registry = build_registry(tools)
    gateway = ClaudeGateway(registry)
    return gateway.gateway_info()
