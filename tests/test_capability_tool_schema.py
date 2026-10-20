from __future__ import annotations

from agent_runtime import capability_tools
from tools.registry import ToolRegistry


class _Manifest:
    def __init__(self, tools):
        self.tools = tools
        self.author = "tester"
        self.status = "published"
        self.version_int = 1


class _Runtime:
    def __init__(self):
        self.tools = ToolRegistry()


def _cap(**params) -> dict:
    return {
        "id": "knowledge.search",
        "title": "Search",
        "description": "Search the knowledge base",
        "params": params,
    }


def _sync(monkeypatch, params: dict):
    monkeypatch.setattr(capability_tools, "_lookup_capability", lambda _cid: _cap(**params))
    rt = _Runtime()
    out = capability_tools.sync_agent_tools(rt, "agent123", _Manifest(["knowledge.search"]))
    return rt, out


def test_schema_level_required_populates_properties(monkeypatch):
    # registry capability specs declare "required": ["query"] at the params
    # level (standard JSON Schema). The old per-property-only reader produced
    # an empty properties map, so the LLM saw a tool it could not call.
    rt, out = _sync(
        monkeypatch,
        {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "search text"},
                "top_k": {"type": "integer"},
            },
        },
    )
    name = "bt_agent123_knowledge_search"
    assert out["registered"] == [name]
    tool = rt.tools.get(name)
    schema = tool.openai_schema()
    props = schema["function"]["parameters"]["properties"]
    assert props == {"query": {"type": "string", "description": "search text"}}
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_per_property_required_still_supported(monkeypatch):
    rt, out = _sync(
        monkeypatch,
        {
            "type": "object",
            "properties": {
                "sku_id": {"type": "string", "required": True},
                "delta": {"type": "integer"},
            },
        },
    )
    tool = rt.tools.get(out["registered"][0])
    assert list(tool.parameters) == ["sku_id"]


def test_no_required_params_yields_empty_schema(monkeypatch):
    rt, out = _sync(
        monkeypatch,
        {"type": "object", "properties": {"filter": {"type": "string"}}},
    )
    tool = rt.tools.get(out["registered"][0])
    assert tool.parameters == {}
