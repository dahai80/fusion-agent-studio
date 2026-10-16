"""N6 ToolSearch lazy schema loading + MCP output cap (issue #343)."""



from tools.base import BaseTool
from tools.registry import ToolRegistry


class _Dummy(BaseTool):
    def __init__(self, name, desc):
        self.name = name
        self.description = desc
        self.parameters = {"x": {"type": "string"}}

    async def execute(self, **kwargs):
        return "ok"


def _registry(n=30):
    r = ToolRegistry()
    for i in range(n):
        r.register(_Dummy(f"tool_{i:02d}", f"utility number {i}"))
    return r


def test_search_tools_substring():
    r = _registry(5)
    r.register(_Dummy("shell_exec", "run shell commands"))
    hits = r.search_tools("shell")
    assert len(hits) == 1 and hits[0]["name"] == "shell_exec"
    assert r.search_tools("utility", limit=3)[0]["name"] == "tool_00"
    assert len(r.search_tools("zzz")) == 0


async def test_tool_search_tool_executes():
    from tools.tool_search import ToolSearchTool

    r = _registry(5)
    t = ToolSearchTool(lambda: r)
    assert t.name == "tool_search"
    assert t.readonly is True
    out = await t.execute(query="tool_0", limit=3)
    import json

    data = json.loads(out)
    assert data["count"] <= 3
    assert data["results"][0]["name"].startswith("tool_0")


async def test_tool_search_no_registry():
    from tools.tool_search import ToolSearchTool

    t = ToolSearchTool(lambda: None)
    out = await t.execute(query="x")
    assert out.startswith("Error:")


def test_runtime_defers_schemas(monkeypatch):
    monkeypatch.setenv("FUSION_TOOLSEARCH", "1")
    monkeypatch.setenv("TOOLSEARCH_INLINE_LIMIT", "5")
    from agent_runtime.runtime import AgentRuntime

    r = _registry(10)
    rt = AgentRuntime(tool_registry=r)
    assert rt._toolsearch_enabled is True
    assert r.has("tool_search")
    schemas = r.to_openai_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "tool_search" in names


def test_runtime_toolsearch_off_by_default(monkeypatch):
    monkeypatch.delenv("FUSION_TOOLSEARCH", raising=False)
    from agent_runtime.runtime import AgentRuntime

    r = _registry(3)
    rt = AgentRuntime(tool_registry=r)
    assert rt._toolsearch_enabled is False
    assert not r.has("tool_search")


def test_mcp_output_cap_truncates(monkeypatch):
    from tools.mcp_tool import MCPTool

    monkeypatch.setenv("FUSION_MCP_OUTPUT_CAP_TOKENS", "10")
    big = {"content": [{"type": "text", "text": "x" * 1000}]}
    out = MCPTool._format_result(big)
    assert len(out) < 200
    assert "truncated" in out


def test_mcp_output_under_cap_untouched(monkeypatch):
    from tools.mcp_tool import MCPTool

    monkeypatch.setenv("FUSION_MCP_OUTPUT_CAP_TOKENS", "25000")
    small = {"content": [{"type": "text", "text": "short answer"}]}
    out = MCPTool._format_result(small)
    assert out == "short answer"
