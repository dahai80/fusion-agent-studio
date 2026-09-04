"""#271: fusion-identity multi-tenant integration (env-gated opt-in).

Covers:
- is_identity_enabled / install_identity_middleware env-gate.
- verify_identity_jwt callback (httpx mocked) — reject/accept/missing-tid.
- guard_client._resolve_tenant_id sources from TenantContext when identity on,
  else falls back to caller param.
- env-gate off = current behavior unchanged (no middleware, tenant_id = caller).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ── env-gate ──


class TestIdentityEnvGate:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        from agent_runtime.identity_integration import is_identity_enabled

        assert is_identity_enabled() is False

    def test_enabled_when_set(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import is_identity_enabled

        assert is_identity_enabled() is True

    def test_other_values_disabled(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "0")
        from agent_runtime.identity_integration import is_identity_enabled

        assert is_identity_enabled() is False


class TestInstallMiddleware:
    def test_disabled_noop_returns_false(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        from agent_runtime.identity_integration import install_identity_middleware

        app = MagicMock()
        assert install_identity_middleware(app) is False
        app.add_middleware.assert_not_called()

    def test_enabled_installs(self, monkeypatch):
        pytest.importorskip("fusion_core")
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import install_identity_middleware

        app = MagicMock()
        captured = {}

        def fake_install(app_, **kwargs):
            captured["verify_jwt"] = kwargs.get("verify_jwt")
            captured["require_jwt"] = kwargs.get("require_jwt")

        with patch(
            "fusion_core.tenant.middleware.install_tenant_middleware",
            side_effect=fake_install,
        ):
            result = install_identity_middleware(app)
        assert result is True
        assert captured["require_jwt"] is True
        assert callable(captured["verify_jwt"])

    def test_enabled_but_import_fails_falls_back(self, monkeypatch):
        import sys

        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import install_identity_middleware

        app = MagicMock()
        # break the import itself (simulate fusion_core absent in CI)
        monkeypatch.setitem(sys.modules, "fusion_core.tenant.middleware", None)
        result = install_identity_middleware(app)
        assert result is False
        app.add_middleware.assert_not_called()


# ── verify_identity_jwt ──


class TestVerifyJwt:
    def test_missing_service_token_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "")
        from agent_runtime.identity_integration import verify_identity_jwt

        with pytest.raises(RuntimeError, match="service token"):
            verify_identity_jwt("caller-token")

    def test_verify_ok_returns_claims(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import verify_identity_jwt

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"claims": {"tid": "tenant-a", "role": "admin"}}
        with patch("httpx.post", return_value=resp):
            claims = verify_identity_jwt("caller-token")
        assert claims["tid"] == "tenant-a"

    def test_verify_rejected_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import verify_identity_jwt

        resp = MagicMock(status_code=401, text="revoked")
        with patch("httpx.post", return_value=resp):
            with pytest.raises(RuntimeError, match="rejected"):
                verify_identity_jwt("bad-token")

    def test_verify_no_tid_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import verify_identity_jwt

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"claims": {"role": "x"}}
        with patch("httpx.post", return_value=resp):
            with pytest.raises(RuntimeError, match="missing tid"):
                verify_identity_jwt("token")

    def test_verify_network_error_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import verify_identity_jwt

        with patch("httpx.post", side_effect=ConnectionError("refused")):
            with pytest.raises(RuntimeError, match="unreachable"):
                verify_identity_jwt("token")


class TestReportUsage:
    def test_disabled_noop(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        from agent_runtime.identity_integration import report_usage

        with patch("httpx.post") as mock_post:
            report_usage("t1", 100)
        mock_post.assert_not_called()

    def test_enabled_posts(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import report_usage

        with patch("httpx.post") as mock_post:
            report_usage("t1", 100, agent_id="a1")
        mock_post.assert_called_once()
        assert "t1" in mock_post.call_args.args[0]


# ── guard_client tenant resolution ──


class TestGuardTenantResolution:
    def test_identity_off_uses_caller_param(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        from agent_runtime.guard_client import GuardSafetyBackend

        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.list_rules.return_value = ([], 0)
        gv = MagicMock(action="allow", risk_level="l1", reason="ok", requires_approval=False)
        gv.redacted_content = ""
        mock_client.evaluate.return_value = gv
        backend = GuardSafetyBackend(client=mock_client, tenant_id="caller-tid")
        backend.evaluate(category="command", content="ls")
        assert mock_client.evaluate.call_args.kwargs["tenant_id"] == "caller-tid"

    def test_identity_on_sources_from_context(self, monkeypatch):
        pytest.importorskip("fusion_core")
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.guard_client import GuardSafetyBackend

        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.list_rules.return_value = ([], 0)
        gv = MagicMock(action="allow", risk_level="l1", reason="ok", requires_approval=False)
        gv.redacted_content = ""
        mock_client.evaluate.return_value = gv
        backend = GuardSafetyBackend(client=mock_client, tenant_id="caller-tid")

        fake_ctx = MagicMock()
        fake_ctx.tenant_id = "ctx-tenant"
        with patch("fusion_core.tenant.context.current", return_value=fake_ctx):
            backend.evaluate(category="command", content="ls")
        assert mock_client.evaluate.call_args.kwargs["tenant_id"] == "ctx-tenant"

    def test_identity_on_no_context_falls_back(self, monkeypatch):
        pytest.importorskip("fusion_core")
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.guard_client import GuardSafetyBackend

        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.list_rules.return_value = ([], 0)
        gv = MagicMock(action="allow", risk_level="l1", reason="ok", requires_approval=False)
        gv.redacted_content = ""
        mock_client.evaluate.return_value = gv
        backend = GuardSafetyBackend(client=mock_client, tenant_id="caller-tid")

        with patch("fusion_core.tenant.context.current", return_value=None):
            backend.evaluate(category="command", content="ls")
        assert mock_client.evaluate.call_args.kwargs["tenant_id"] == "caller-tid"


# ── #279: consume_rpc_auth / reset_rpc_auth ──


class TestConsumeRpcAuth:
    def test_identity_off_returns_none(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        from agent_runtime.identity_integration import consume_rpc_auth

        assert consume_rpc_auth({"_auth": {"jwt": "x"}}) is None

    def test_no_auth_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import consume_rpc_auth

        assert consume_rpc_auth({}) is None
        assert consume_rpc_auth({"other": 1}) is None

    def test_empty_auth_returns_none(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import consume_rpc_auth

        assert consume_rpc_auth({"_auth": {}}) is None

    def test_missing_jwt_raises(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        from agent_runtime.identity_integration import consume_rpc_auth

        with pytest.raises(RuntimeError, match="missing jwt"):
            consume_rpc_auth({"_auth": {"tid": "t1"}})

    def test_invalid_jwt_raises_runtime(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import consume_rpc_auth

        resp = MagicMock(status_code=401, text="revoked")
        with patch("httpx.post", return_value=resp):
            with pytest.raises(RuntimeError):
                consume_rpc_auth({"_auth": {"jwt": "bad"}})

    def test_valid_jwt_binds_context(self, monkeypatch):
        pytest.importorskip("fusion_core")
        from fusion_core.tenant.context import current

        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        from agent_runtime.identity_integration import (
            consume_rpc_auth,
            reset_rpc_auth,
        )

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "claims": {"tid": "tenant-x", "role": "admin"}
        }
        with patch("httpx.post", return_value=resp):
            token = consume_rpc_auth({"_auth": {"jwt": "valid"}})

        assert token is not None
        ctx = current()
        assert ctx.tenant_id == "tenant-x"
        # reset clears the bound context
        reset_rpc_auth(token)
        assert current() is None

    def test_reset_none_is_noop(self, monkeypatch):
        from agent_runtime.identity_integration import reset_rpc_auth

        reset_rpc_auth(None)  # must not raise


# ── #279: _dispatch auth wiring (DaemonServer) ──


class TestDispatchAuth:
    def _make_server(self):
        from agent_runtime.daemon_server import DaemonServer

        return DaemonServer(store_path="/tmp/test_dispatch_auth.db")

    def test_dispatch_no_auth_identity_off_unscoped(self, monkeypatch):
        monkeypatch.delenv("FUSION_IDENTITY_ENABLED", raising=False)
        srv = self._make_server()
        seen = {}

        async def fake_handler(params):
            seen["params"] = params
            return {"ok": True}

        monkeypatch.setattr(srv, "_get_handler", lambda m: fake_handler)
        msg = {"jsonrpc": "2.0", "id": 1, "method": "noop", "params": {}}
        result = __import__("asyncio").run(srv._dispatch(msg))
        assert result["result"] == {"ok": True}

    def test_dispatch_valid_auth_strips_and_succeeds(self, monkeypatch):
        pytest.importorskip("fusion_core")
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        srv = self._make_server()
        seen = {}

        async def fake_handler(params):
            seen["params"] = params
            return {"ok": True}

        monkeypatch.setattr(srv, "_get_handler", lambda m: fake_handler)
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "claims": {"tid": "tenant-x", "role": "admin"}
        }
        with patch("httpx.post", return_value=resp):
            msg = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "noop",
                "params": {"_auth": {"jwt": "valid"}, "keep": 1},
            }
            result = __import__("asyncio").run(srv._dispatch(msg))
        assert result["result"] == {"ok": True}
        # _auth stripped from params passed to handler; real params kept
        assert "_auth" not in seen["params"]
        assert seen["params"]["keep"] == 1

    def test_dispatch_invalid_auth_returns_401_style(self, monkeypatch):
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc-secret")
        srv = self._make_server()
        called = {"n": 0}

        async def fake_handler(params):
            called["n"] += 1
            return {"ok": True}

        monkeypatch.setattr(srv, "_get_handler", lambda m: fake_handler)
        resp = MagicMock(status_code=401, text="revoked")
        with patch("httpx.post", return_value=resp):
            msg = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "noop",
                "params": {"_auth": {"jwt": "bad"}},
            }
            result = __import__("asyncio").run(srv._dispatch(msg))
        assert result["error"]["code"] == -32001
        assert "Auth rejected" in result["error"]["message"]
        # handler must NOT be called on auth reject
        assert called["n"] == 0
