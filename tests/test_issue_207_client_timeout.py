"""Tests for #207: AgentClient.call timeout — daemon no-response must not hang
the caller forever.

Uses lightweight asyncio unix-socket servers (not the full DaemonServer) to
isolate the client timeout path without the ws/cluster/http port startup cost
that made the daemon_stub fixture heavy and prone to OOM-kill under py3.14.

Covers:
1. per-call timeout fires on a handler that never returns → error dict, no hang
2. default_timeout (from __init__) applies when per-call timeout is None
3. per-call timeout overrides default_timeout
4. timeout=None (legacy) still works against a normal server (regression)
5. a fast normal response under a timeout returns the result unchanged
6. connection refused / missing socket still returns a friendly error
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from agent_runtime.sdk import AgentClient


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


async def _slow_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    # Read the request line, then sleep past the client timeout so the timeout
    # fires — but still complete so server.wait_closed() in teardown does not
    # block. Simulates a daemon handler that hung on a long task.
    try:
        await reader.readline()
        await asyncio.sleep(0.5)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _fast_ping_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    # Reply immediately with a JSON-RPC result so the legacy no-timeout path and
    # the fast-response-under-timeout path both see a normal result.
    import json

    try:
        await reader.readline()
        writer.write(
            (json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"pong": True}}) + "\n").encode()
        )
        await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _start_server(sock_path: str, handler):
    server = await asyncio.start_unix_server(handler, sock_path, limit=2 ** 20)
    return server


class TestCallTimeout:
    @pytest.mark.asyncio
    async def test_per_call_timeout_returns_error_no_hang(self, socket_path):
        server = await _start_server(socket_path, _slow_handler)
        try:
            client = AgentClient(socket_path=socket_path)
            result = await asyncio.wait_for(client.call("ping", timeout=0.05), timeout=5.0)
            assert "error" in result
            assert "timeout" in result["error"]
            assert "ping" in result["error"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_default_timeout_applies_when_per_call_none(self, socket_path):
        server = await _start_server(socket_path, _slow_handler)
        try:
            client = AgentClient(socket_path=socket_path, default_timeout=0.05)
            result = await asyncio.wait_for(client.call("ping"), timeout=5.0)
            assert "error" in result
            assert "timeout 0.05" in result["error"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_per_call_timeout_overrides_default(self, socket_path):
        server = await _start_server(socket_path, _slow_handler)
        try:
            # default 10s but per-call 0.05 must fire first
            client = AgentClient(socket_path=socket_path, default_timeout=10.0)
            result = await asyncio.wait_for(client.call("ping", timeout=0.05), timeout=5.0)
            assert "error" in result
            assert "timeout 0.05" in result["error"]
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_timeout_none_legacy_path_works(self, socket_path):
        # Regression: no timeout against a healthy server returns the real result.
        server = await _start_server(socket_path, _fast_ping_handler)
        try:
            client = AgentClient(socket_path=socket_path)
            result = await asyncio.wait_for(client.call("ping"), timeout=5.0)
            assert result.get("pong") is True
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_fast_response_under_timeout_returns_result(self, socket_path):
        # A timeout set does not corrupt a fast normal response.
        server = await _start_server(socket_path, _fast_ping_handler)
        try:
            client = AgentClient(socket_path=socket_path, default_timeout=5.0)
            result = await asyncio.wait_for(client.call("ping"), timeout=5.0)
            assert result.get("pong") is True
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_missing_socket_returns_friendly_error(self, socket_path):
        # No server listening (socket file absent) — FileNotFoundError path
        # unified with ConnectionRefusedError into the friendly message.
        client = AgentClient(socket_path=socket_path)
        result = await client.call("ping")
        assert "error" in result
        assert "not running" in result["error"]
