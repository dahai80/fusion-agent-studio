"""#326: UDS JSON-RPC auth enforcement — server-side closure.

Contract (audit 0910 P0-1):
- invalid `_auth` on ANY method (incl. ping) -> JSON-RPC error code 401,
  exactly what the fusion-studio IPCClient authEnforced probe expects.
- missing `_auth` on protected methods -> 401; PRE_AUTH_ALLOWLIST
  (auth.login / ping / daemon.ping / rpc.discover) stays callable pre-auth.
- auth.login: apikey (fk-...) -> local JWT; subsequent calls carry _auth.
- peer-uid enforcement on accept is #209 (covered by test_issue_209).
"""

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.apikey_manager import ApiKeyManager
from agent_runtime.daemon_server import DaemonServer
from agent_runtime.rpc_auth import issue_local_jwt


async def _rpc_call(socket_path, method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=5.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store_db(tmp_path):
    return str(tmp_path / "test_store.db")


@pytest.fixture
async def daemon(socket_path, store_db):
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def enforce_on(monkeypatch):
    monkeypatch.setenv("FUSION_AUTH_ENFORCE", "1")


@pytest.fixture
def api_key(store_db):
    # A real daemon API key minted in the daemon's auth dir (tmp_path).
    mgr = ApiKeyManager(os.path.dirname(store_db))
    created = mgr.create(name="test-326", permissions=["agent:execute", "admin"])
    return created["key_secret"]


class TestEnforcement:
    async def test_probe_invalid_auth_ping_returns_401(self, daemon, enforce_on):
        # THE client probe contract: ping + forged jwt must 401.
        resp = await _rpc_call(
            daemon.socket_path,
            "ping",
            {"_auth": {"jwt": "__probe_invalid__", "tid": "__probe__"}},
        )
        assert resp["error"]["code"] == 401

    async def test_missing_auth_protected_method_401(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "task.list", {})
        assert resp["error"]["code"] == 401
        assert "missing _auth" in resp["error"]["message"]

    async def test_allowlisted_ping_without_auth_ok(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "ping")
        assert resp["result"]["pong"] is True

    async def test_allowlisted_discover_without_auth_ok(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "rpc.discover")
        assert "methods" in resp["result"] or resp.get("result") is not None

    async def test_malformed_auth_401(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "ping", {"_auth": "not-a-dict"})
        assert resp["error"]["code"] == 401

    async def test_auth_missing_jwt_401(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "task.list", {"_auth": {"tid": "x"}})
        assert resp["error"]["code"] == 401


class TestLoginFlow:
    async def test_login_and_call_with_jwt(self, daemon, enforce_on, api_key):
        login = await _rpc_call(daemon.socket_path, "auth.login", {"apikey": api_key})
        assert login["result"]["jwt"]
        assert login["result"]["tid"] == "local"
        jwt = login["result"]["jwt"]
        resp = await _rpc_call(
            daemon.socket_path, "task.list", {"_auth": {"jwt": jwt, "tid": "local"}}
        )
        assert resp["result"]["total"] >= 0

    async def test_login_bad_apikey_401(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "auth.login", {"apikey": "fk-nope"})
        assert resp["error"]["code"] == 401
        assert "invalid apikey" in resp["error"]["message"]

    async def test_login_missing_apikey_401(self, daemon, enforce_on):
        resp = await _rpc_call(daemon.socket_path, "auth.login", {})
        assert resp["error"]["code"] == 401

    async def test_custom_tid_roundtrip(self, daemon, enforce_on, api_key):
        login = await _rpc_call(
            daemon.socket_path, "auth.login", {"apikey": api_key, "tid": "tenant-7"}
        )
        assert login["result"]["tid"] == "tenant-7"
        jwt = login["result"]["jwt"]
        resp = await _rpc_call(daemon.socket_path, "task.list", {"_auth": {"jwt": jwt}})
        assert "result" in resp


class TestJwtValidation:
    async def test_expired_jwt_401(self, daemon, enforce_on, store_db):
        expired = issue_local_jwt(
            os.path.dirname(store_db), ttl_seconds=-3600
        )
        resp = await _rpc_call(
            daemon.socket_path, "task.list", {"_auth": {"jwt": expired["jwt"]}}
        )
        assert resp["error"]["code"] == 401
        assert "expired" in resp["error"]["message"]

    async def test_tampered_signature_401(self, daemon, enforce_on, store_db):
        issued = issue_local_jwt(os.path.dirname(store_db))
        jwt = issued["jwt"]
        head, payload, sig = jwt.split(".")
        # Flip the FIRST signature char: all six of its bits land in byte 0.
        # (The last char's low bits are base64 padding — flipping it there
        # sometimes decodes to identical bytes and the tamper is invisible.)
        flipped = "A" if sig[0] != "A" else "B"
        tampered = f"{head}.{payload}.{flipped}{sig[1:]}"
        resp = await _rpc_call(
            daemon.socket_path, "task.list", {"_auth": {"jwt": tampered}}
        )
        assert resp["error"]["code"] == 401
        assert "signature" in resp["error"]["message"]

    async def test_foreign_secret_jwt_401(self, daemon, enforce_on, tmp_path):
        # Signed with a DIFFERENT install's secret -> signature mismatch.
        other = issue_local_jwt(tmp_path / "other-install")
        resp = await _rpc_call(
            daemon.socket_path, "task.list", {"_auth": {"jwt": other["jwt"]}}
        )
        assert resp["error"]["code"] == 401

    async def test_auth_dir_follows_store_path(self, daemon, store_db):
        assert daemon._auth_dir == os.path.dirname(store_db)


class TestEnforcementOff:
    async def test_off_missing_auth_legacy_ok(self, daemon, monkeypatch):
        monkeypatch.setenv("FUSION_AUTH_ENFORCE", "0")
        resp = await _rpc_call(daemon.socket_path, "task.list", {})
        assert resp["result"]["total"] >= 0

    async def test_off_invalid_auth_ignored(self, daemon, monkeypatch):
        monkeypatch.setenv("FUSION_AUTH_ENFORCE", "0")
        resp = await _rpc_call(daemon.socket_path, "ping", {"_auth": {"jwt": "junk"}})
        assert resp["result"]["pong"] is True


class TestIdentityMode:
    async def test_identity_invalid_jwt_401(self, daemon, enforce_on, monkeypatch):
        from unittest.mock import MagicMock, patch

        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        resp401 = MagicMock(status_code=401, text="revoked")
        with patch("httpx.post", return_value=resp401):
            resp = await _rpc_call(
                daemon.socket_path, "ping", {"_auth": {"jwt": "bad-identity-jwt"}}
            )
        assert resp["error"]["code"] == 401

    async def test_identity_login_refused(self, daemon, enforce_on, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        resp = await _rpc_call(daemon.socket_path, "auth.login", {"apikey": "fk-x"})
        assert resp["error"]["code"] == 401
        assert "identity" in resp["error"]["message"]
