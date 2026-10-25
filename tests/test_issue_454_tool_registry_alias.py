"""Issue #454 — mcp.register_server AttributeError: runtime.tool_registry.

AgentRuntime stores the tool_registry kwarg as self.tools; the MCP and
plugin dispatchers read/write runtime.tool_registry, which never existed
— the first mcp.register_server / plugin RPC raised AttributeError and
the RPC answered -32000. A property alias keeps both names on the same
backing attr.
"""
from __future__ import annotations

import pytest

from agent_runtime.runtime import AgentRuntime


@pytest.fixture
def socket_path(tmp_path):
    return str(tmp_path / "studio.sock")


@pytest.fixture
def store_db(tmp_path):
    return str(tmp_path / "store.db")


@pytest.fixture
async def daemon_stub(socket_path, store_db):
    from agent_runtime.daemon_server import DaemonServer

    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    yield d
    await d.stop()


def test_tool_registry_alias_reads_tools():
    from tools import create_default_registry

    registry = create_default_registry()
    runtime = AgentRuntime(tool_registry=registry)
    assert runtime.tools is registry
    assert runtime.tool_registry is registry


def test_tool_registry_alias_write_propagates():
    from tools import create_default_registry

    runtime = AgentRuntime()
    assert runtime.tools is None
    registry = create_default_registry()
    runtime.tool_registry = registry
    assert runtime.tools is registry
    runtime.tools = None
    assert runtime.tool_registry is None


async def test_mcp_register_server_path_initializes_registry(daemon_stub):
    # The failing path from the issue: mcp.register_server on a runtime
    # without a pre-built tool registry must not raise AttributeError.
    resp = await daemon_stub._dispatch(
        "mcp.register_server",
        {"server_url": "http://127.0.0.1:19999/mcp"},
    )
    assert resp["status"] in ("ok", "error")
    runtime = daemon_stub._get_runtime()
    assert runtime.tool_registry is runtime.tools
    assert runtime.tools is not None
