"""#271: fusion-identity multi-tenant integration (env-gated opt-in).

FUSION_IDENTITY_ENABLED=1 enables:
- install_tenant_middleware on the FastAPI app with a verify_jwt callback
  calling fusion-identity POST /api/v1/auth/verify.
- tenant_id sourced from verified TenantContext (not caller param) into guard.
- usage reporting to fusion-identity.

Unset (default) = current local ApiKeyManager behavior unchanged (local-dev/CI).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_IDENTITY_URL = os.environ.get(
    "FUSION_IDENTITY_URL", "http://127.0.0.1:11470"
).rstrip("/")
_SERVICE_TOKEN_ENV = "FUSION_IDENTITY_SERVICE_TOKEN"
_VERIFY_TIMEOUT = float(os.environ.get("FUSION_IDENTITY_VERIFY_TIMEOUT", "3"))


def is_identity_enabled() -> bool:
    return os.environ.get("FUSION_IDENTITY_ENABLED", "0").strip() == "1"


def _service_token() -> str:
    return os.environ.get(_SERVICE_TOKEN_ENV, "").strip()


def verify_identity_jwt(token: str) -> dict[str, Any]:
    """VerifyJwt callback for install_tenant_middleware.

    Calls fusion-identity /api/v1/auth/verify with the caller's bearer token.
    Service token (FUSION_IDENTITY_SERVICE_TOKEN) authorizes THIS service to
    verify caller tokens. Returns the claims dict; raises on reject/error so
    the middleware returns 401.
    """
    import httpx

    svc_token = _service_token()
    if not svc_token:
        logger.error(
            "identity enabled but %s unset; cannot verify caller tokens",
            _SERVICE_TOKEN_ENV,
        )
        raise RuntimeError("identity service token not configured")
    headers = {"Authorization": f"Bearer {svc_token}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(
            f"{_IDENTITY_URL}/api/v1/auth/verify",
            json={"token": token},
            headers=headers,
            timeout=_VERIFY_TIMEOUT,
        )
    except Exception as e:
        logger.warning("identity verify call failed: %s", e)
        raise RuntimeError(f"identity verify unreachable: {e}") from e
    if resp.status_code != 200:
        logger.warning("identity verify rejected token: %s %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"identity verify rejected: {resp.status_code}")
    claims = resp.json().get("claims") or resp.json()
    if not claims.get("tid") and not claims.get("tenant"):
        logger.warning("identity verify returned no tid claim: %s", claims)
        raise RuntimeError("identity verify: missing tid claim")
    logger.info("identity verify ok tid=%s role=%s", claims.get("tid"), claims.get("role"))
    return claims


def report_usage(tenant_id: str, tokens: int, agent_id: str | None = None) -> None:
    """Best-effort usage reporting to fusion-identity (fire-and-forget)."""
    if not is_identity_enabled():
        return
    svc_token = _service_token()
    if not svc_token or not tenant_id:
        return
    import httpx

    try:
        httpx.post(
            f"{_IDENTITY_URL}/api/v1/tenants/{tenant_id}/usage",
            json={"tokens": tokens, "agent_id": agent_id or "", "source": "fusion-agent-studio"},
            headers={"Authorization": f"Bearer {svc_token}"},
            timeout=_VERIFY_TIMEOUT,
        )
    except Exception as e:
        logger.debug("usage report failed (best-effort): %s", e)


def consume_rpc_auth(params: dict[str, Any]) -> Any:
    """#279: consume `_auth` (jwt/tid) from a JSON-RPC params payload.

    Env-gated (FUSION_IDENTITY_ENABLED). When identity is on AND params carry
    an `_auth` object, verify the JWT via fusion-identity and bind a
    TenantContext for the duration of the handler call. Returns a contextvar
    token to reset after the call (or None when no context was set).

    Behavior:
    - identity off, or no `_auth` -> None (current unscoped behavior preserved).
    - identity on, `_auth` present, valid JWT -> TenantContext set, token returned.
    - identity on, `_auth` present, invalid/expired/unreachable -> raises
      RuntimeError; the caller should return a 401-style JSON-RPC error.
    """
    if not is_identity_enabled():
        return None
    auth = params.get("_auth")
    if not isinstance(auth, dict) or not auth:
        return None
    jwt = auth.get("jwt", "")
    if not jwt:
        raise RuntimeError("auth missing jwt")
    claims = verify_identity_jwt(jwt)
    # from_mapping wants tid/tenant; verify_identity_jwt already enforced tid.
    from fusion_core.tenant.context import from_mapping, set_context
    ctx = from_mapping(claims)
    token = set_context(ctx)
    logger.info("rpc auth bound tid=%s", ctx.tenant_id)
    return token


def reset_rpc_auth(token: Any) -> None:
    """#279: reset the TenantContext set by consume_rpc_auth (finally block)."""
    if token is None:
        return
    try:
        from fusion_core.tenant.context import reset as _reset
        _reset(token)
    except Exception as e:
        logger.debug("rpc auth reset failed: %s", e)


def install_identity_middleware(app: Any) -> bool:
    """Wire install_tenant_middleware on app when identity enabled.

    Returns True if installed, False if disabled (no-op). Fail-closed: if
    enabled but fusion_core/fusion_identity import fails, log error and return
    False (falls back to local ApiKeyManager rather than crashing).
    """
    if not is_identity_enabled():
        return False
    try:
        from fusion_core.tenant.middleware import install_tenant_middleware
    except Exception as e:
        logger.error(
            "identity enabled but fusion_core.tenant.middleware importable? %s; "
            "falling back to local ApiKeyManager",
            e,
        )
        return False
    install_tenant_middleware(app, verify_jwt=verify_identity_jwt, require_jwt=True)
    logger.info("fusion-identity tenant middleware installed on %s", getattr(app, "title", "app"))
    return True
