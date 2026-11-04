from __future__ import annotations

import asyncio
import json

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.loopback import (
    TRUST_HTTP,
    TRUST_UDS,
    current_transport_trust,
    is_loopback_trusted,
    set_transport_trust,
)
from agent_runtime.rpc_auth import authenticate_rpc
from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

SOCK_ENV_METHODS = ("apikey.loopback_create", "apikey.loopback_rotate")


class _StubAudit:
    def __init__(self):
        self.entries: list[dict] = []

    def log_action(self, **kw):
        self.entries.append(kw)
        return None


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    ds = DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
        ws_port=None,
        cluster_port=None,
        http_port=None,
    )
    # Isolation: the apikey manager defaults to the live
    # ~/.fusion-agent-studio/apikeys index — inject a tmp one.
    from agent_runtime.apikey_manager import ApiKeyManager

    ds._apikey_mgr = ApiKeyManager(tmp_path / "apikeys")
    stub = _StubAudit()
    monkeypatch.setattr(ds, "_audit_logger", stub)
    return ds


async def test_trust_context_defaults_to_http():
    assert current_transport_trust() == TRUST_HTTP
    assert not is_loopback_trusted()


async def test_loopback_create_rejected_off_uds(daemon):
    # No transport trust (HTTP request task) — loopback issuance must refuse.
    handler = daemon._get_handler("apikey.loopback_create")
    assert handler is not None
    rec = await handler({"name": "ghost"})
    assert rec["status"] == "error"
    assert "UDS" in rec["message"]
    assert not daemon._audit_logger.entries


async def test_loopback_rotate_rejected_off_uds(daemon):
    handler = daemon._get_handler("apikey.loopback_rotate")
    rec = await handler({"key_id": "nope"})
    assert rec["status"] == "error"
    assert not daemon._audit_logger.entries


async def test_loopback_create_on_uds_issues_key_and_audits(daemon):
    set_transport_trust(TRUST_UDS)
    handler = daemon._get_handler("apikey.loopback_create")
    rec = await handler({"name": "recover-579", "permissions": ["admin"]})
    assert rec.get("key_secret"), rec
    assert rec.get("key_id")
    actions = [e["action"] for e in daemon._audit_logger.entries]
    assert actions == ["apikey.create"]
    assert daemon._audit_logger.entries[0]["result"] == "success"
    assert daemon._audit_logger.entries[0]["actor_id"] == "loopback-cli"
    set_transport_trust(TRUST_HTTP)


async def test_loopback_rotate_on_uds_rotates_and_audits(daemon):
    set_transport_trust(TRUST_UDS)
    create = daemon._get_handler("apikey.create")
    rec = await create({"name": "pre-579"})
    key_id = rec["key_id"]
    handler = daemon._get_handler("apikey.loopback_rotate")
    rot = await handler({"key_id": key_id})
    assert rot.get("key_secret"), rot
    actions = [e["action"] for e in daemon._audit_logger.entries]
    assert "apikey.rotate" in actions
    set_transport_trust(TRUST_HTTP)


async def test_loopback_create_requires_name(daemon):
    set_transport_trust(TRUST_UDS)
    handler = daemon._get_handler("apikey.loopback_create")
    rec = await handler({})
    assert rec["status"] == "error"
    assert "name" in rec["message"]
    set_transport_trust(TRUST_HTTP)


def test_loopback_methods_pre_auth_callable_without_jwt():
    for method in SOCK_ENV_METHODS:
        allowed, claims, reason = authenticate_rpc({}, method, "/tmp/none")
        assert allowed, f"{method} should be pre-auth callable: {reason}"
        assert claims is None


def test_loopback_methods_absent_from_http_bridge_allowlist():
    for method in SOCK_ENV_METHODS:
        assert method not in WEB_RPC_ALLOWLIST


async def test_uds_end_to_end_recovery(tmp_path):
    # Full loop: peer-uid-verified UDS connection -> pre-auth loopback_create
    # -> one-time secret returned. Proves "bootstrap plaintext lost" is
    # recoverable from the product.
    sock_path = str(tmp_path / "e2e.sock")
    if len(sock_path) > 100:
        # AF_UNIX path limit; keep the temp socket short-lived in /tmp.
        import tempfile

        sock_path = tempfile.mkdtemp(prefix="f579_") + "/e2e.sock"
    ds = DaemonServer(
        socket_path=sock_path,
        store_path=str(tmp_path / "store.db"),
        ws_port=None,
        cluster_port=None,
        http_port=None,
    )
    # Isolation: _get_apikey_manager and AuditLogger default to the live
    # ~/.fusion-agent-studio store — inject tmp instances so the e2e loop
    # never writes real credentials or audit rows.
    from agent_runtime.apikey_manager import ApiKeyManager

    ds._apikey_mgr = ApiKeyManager(tmp_path / "apikeys")
    ds._audit_logger = _StubAudit()
    await ds.start()
    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)
        req = {"jsonrpc": "2.0", "id": 7, "method": "apikey.loopback_create",
               "params": {"name": "e2e-recover"}}
        writer.write(json.dumps(req).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        resp = json.loads(line)
        assert "error" not in resp, resp
        result = resp.get("result", {})
        assert result.get("key_secret"), result
        writer.close()
    finally:
        await ds.stop()
