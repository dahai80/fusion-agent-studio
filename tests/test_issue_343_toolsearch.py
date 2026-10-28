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
    # #488: registration moved to top-level surfaces (_register_toolsearch);
    # __init__ keeps subagent registries caller-filtered.
    rt._register_toolsearch()
    assert r.has("tool_search")
    schemas = r.to_openai_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "tool_search" in names


def test_runtime_toolsearch_on_by_default(monkeypatch):
    # #488: flipped default — ToolSearch ships ON; FUSION_TOOLSEARCH is the
    # opt-OUT (0/false/no restores full inline schemas)
    monkeypatch.delenv("FUSION_TOOLSEARCH", raising=False)
    from agent_runtime.runtime import AgentRuntime

    r = _registry(3)
    rt = AgentRuntime(tool_registry=r)
    assert rt._toolsearch_enabled is True
    # #488: top-level surfaces call _register_toolsearch after construction;
    # the plain runtime must NOT auto-register (subagent isolation).
    assert not r.has("tool_search")
    rt._register_toolsearch()
    assert r.has("tool_search")


def test_runtime_toolsearch_opt_out(monkeypatch):
    monkeypatch.setenv("FUSION_TOOLSEARCH", "0")
    from agent_runtime.runtime import AgentRuntime

    r = _registry(3)
    rt = AgentRuntime(tool_registry=r)
    assert rt._toolsearch_enabled is False
    assert not r.has("tool_search")


async def test_stdio_transport_skips_interleaved_notifications(tmp_path):
    # #488: a chatty standard MCP server — notifications (progress, logging,
    # initialized) and a server-to-client request interleaved between
    # responses — must not desynchronize request/response pairing.
    import sys

    from tools.mcp_tool import StdioMCPTransport

    script = tmp_path / "chatty_mcp_server.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    rid = req['id']\n"
        "    # notification-style noise BEFORE the response\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress',\n"
        "                       'params': {'progress': 1}}))\n"
        "    # server-to-client request (has an id, but not ours)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': 999, 'method': 'sampling/createMessage',\n"
        "                       'params': {}}))\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'echo': req['method']}}))\n"
        "    sys.stdout.flush()\n"
    )
    transport = StdioMCPTransport([sys.executable, str(script)])
    try:
        resp = await transport._rpc("tools/list")
        # _initialize already consumed id 1
        assert resp["id"] == 2
        assert resp["result"] == {"echo": "tools/list"}
        # second request on the same connection — id must advance and still pair
        resp2 = await transport._rpc("tools/call", {"name": "x"})
        assert resp2["id"] == 3
        assert resp2["result"] == {"echo": "tools/call"}
    finally:
        await transport.close()


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
