"""Tests for issue #661 - auth.login must accept loopback-issued API keys.

apikey.loopback_create writes keys into the canonical apikeys/ subdir
(daemon _get_apikey_manager base), but auth.login used to validate against
the root index only, so freshly issued fk- keys were rejected key_not_found.
Fix: auth_login takes apikey_dir (canonical store first, root fallback).
"""

from __future__ import annotations

import pytest

from agent_runtime.apikey_manager import ApiKeyManager
from agent_runtime.rpc_auth import RpcAuthError, auth_login


@pytest.fixture(autouse=True)
def _no_identity(monkeypatch, tmp_path):
    monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSION_AGENT_STUDIO_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_WEB_MODULE_KEY", raising=False)


def _issue(base, name="e2e"):
    mgr = ApiKeyManager(base)
    out = mgr.create(name)
    return out["key_secret"], out["key_id"]


class TestLoopbackKeyRoundTrip:
    async def test_subdir_key_logs_in_with_apikey_dir(self, tmp_path):
        base = tmp_path / "home"
        subdir = base / "apikeys"
        secret, key_id = _issue(subdir)
        issued = auth_login({"apikey": secret}, base, apikey_dir=subdir)
        assert issued.get("token") or issued.get("jwt")
        assert issued["key_id"] == key_id

    async def test_subdir_key_rejected_without_apikey_dir(self, tmp_path):
        # Regression anchor: the pre-#661 behavior — root-only lookup.
        base = tmp_path / "home"
        subdir = base / "apikeys"
        secret, _ = _issue(subdir)
        with pytest.raises(RpcAuthError):
            auth_login({"apikey": secret}, base)

    async def test_legacy_root_key_still_validates(self, tmp_path):
        base = tmp_path / "home"
        secret, key_id = _issue(base)
        issued = auth_login({"apikey": secret}, base, apikey_dir=base / "apikeys")
        assert issued["key_id"] == key_id

    async def test_unknown_key_rejected_even_with_apikey_dir(self, tmp_path):
        base = tmp_path / "home"
        _issue(base / "apikeys")
        with pytest.raises(RpcAuthError):
            auth_login({"apikey": "fk-not-a-real-key-000000"}, base, apikey_dir=base / "apikeys")

    async def test_issued_jwt_verifies_against_root_secret(self, tmp_path):
        # JWT secret ownership must stay at base_dir regardless of apikey_dir.
        from agent_runtime.rpc_auth import verify_rpc_jwt

        base = tmp_path / "home"
        subdir = base / "apikeys"
        secret, _ = _issue(subdir)
        issued = auth_login({"apikey": secret}, base, apikey_dir=subdir)
        claims = verify_rpc_jwt(issued["jwt"], base)
        assert claims.get("tid") == "local"
