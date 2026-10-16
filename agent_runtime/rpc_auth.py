"""#326: UDS JSON-RPC auth enforcement — server-side closure of the control plane.

The daemon's UDS channel previously ignored `_auth` unless identity was
enabled, leaving an unauthenticated local control plane (audit 0910 P0-1).
This module enforces, on every RPC:

- `_auth` present -> JWT validated (signature + expiry + tenant). Identity
  mode (FUSION_IDENTITY_ENABLED=1) delegates to fusion-identity verify;
  local mode verifies HS256 JWTs signed with a per-install secret.
- `_auth` invalid -> JSON-RPC error code 401 on ANY method (the
  fusion-studio IPCClient authEnforced probe sends ping + forged jwt and
  expects exactly 401).
- `_auth` missing -> 401 unless the method is in PRE_AUTH_ALLOWLIST (the
  login flow itself, liveness/health probes, capability discovery).
- FUSION_AUTH_ENFORCE=0 -> full legacy #279 behavior (identity-gated only),
  escape hatch for tests / pre-migration local dev.

auth.login is the local-mode bootstrap: a valid daemon API key (fk-...) is
exchanged for a locally-signed JWT, which clients then inject as _auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTH_ENFORCE_ENV = "FUSION_AUTH_ENFORCE"
JWT_SECRET_FILE = "rpc_jwt_secret"
JWT_ISSUER = "fusion-agent-studio"
JWT_TTL_SECONDS = 12 * 3600
JWT_LEEWAY_SECONDS = 30

# Methods callable WITHOUT `_auth` (pre-auth bootstrap): the login flow
# itself, liveness/health probes and capability discovery. An `_auth` param
# that is present but invalid is rejected on ANY method — the client
# authEnforced probe (ping + forged jwt) must observe 401, so ping must NOT
# be a blanket bypass.
PRE_AUTH_ALLOWLIST = frozenset(
    {"auth.login", "ping", "daemon.ping", "rpc.discover"}
)


class RpcAuthError(Exception):
    """Auth rejection carrying the operator-facing reason (mapped to 401)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def auth_enforcement_enabled() -> bool:
    raw = os.environ.get(AUTH_ENFORCE_ENV, "1").strip().lower()
    return raw not in ("0", "false", "off")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _secret_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / JWT_SECRET_FILE


def _load_or_create_secret(base_dir: str | Path) -> bytes:
    path = _secret_path(base_dir)
    if path.exists():
        data = path.read_bytes().strip()
        if data:
            return data
    # First run: generate a per-install secret, 0o600 next to store.db.
    secret = secrets.token_hex(32).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secret + b"\n")
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("rpc_auth: could not tighten jwt secret perms: %s", exc)
    logger.info("rpc_auth: generated new RPC JWT signing secret at %s", path)
    return secret


def issue_local_jwt(
    base_dir: str | Path,
    tid: str = "local",
    sub: str = "local-user",
    ttl_seconds: int = JWT_TTL_SECONDS,
) -> dict[str, Any]:
    secret = _load_or_create_secret(base_dir)
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": JWT_ISSUER, "sub": sub, "tid": tid, "iat": now, "exp": now + ttl_seconds}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    token = f"{signing_input}.{_b64url(sig)}"
    logger.info("rpc_auth: issued local jwt tid=%s sub=%s ttl=%ss", tid, sub, ttl_seconds)
    return {"jwt": token, "tid": tid, "expires_at": now + ttl_seconds}


def _verify_local_jwt(token: str, base_dir: str | Path) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise RpcAuthError("malformed jwt: expected 3 segments")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        sig = _b64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise RpcAuthError(f"malformed jwt: {exc}") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise RpcAuthError("unsupported jwt alg (want HS256)")
    secret = _load_or_create_secret(base_dir)
    expected = hmac.new(secret, f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise RpcAuthError("jwt signature mismatch")
    if not isinstance(payload, dict):
        raise RpcAuthError("jwt payload not an object")
    if payload.get("iss") != JWT_ISSUER:
        raise RpcAuthError("jwt issuer mismatch")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise RpcAuthError("jwt missing exp")
    if time.time() > exp + JWT_LEEWAY_SECONDS:
        raise RpcAuthError("jwt expired")
    tid = payload.get("tid")
    if not isinstance(tid, str) or not tid:
        raise RpcAuthError("jwt missing tenant (tid)")
    return payload


def verify_rpc_jwt(token: str, base_dir: str | Path) -> dict[str, Any]:
    """Validate a caller JWT. Identity mode delegates to fusion-identity
    (signature + expiry + tenant verified server-side); local mode verifies
    the per-install HS256 signature. Raises RpcAuthError on any reject."""
    from .identity_integration import is_identity_enabled

    if is_identity_enabled():
        from .identity_integration import verify_identity_jwt

        # verify_identity_jwt raises RuntimeError on reject; normalize so the
        # 401 mapping in _dispatch is uniform.
        try:
            claims = verify_identity_jwt(token)
        except RuntimeError as exc:
            raise RpcAuthError(f"identity rejected jwt: {exc}") from exc
        return claims
    return _verify_local_jwt(token, base_dir)


def authenticate_rpc(
    params: Any, method: str, base_dir: str | Path
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Gate one RPC (enforcement ON only — the legacy path stays in _dispatch).

    Returns (allowed, claims, reject_reason):
    - allowed=True, claims set: valid `_auth`; caller should bind the tenant
      context and strip `_auth` before invoking the handler.
    - allowed=True, claims None: no `_auth` and the method is allowlisted
      pre-auth (login flow / liveness / discovery).
    - allowed=False: reject with JSON-RPC error code 401 + reason.
    """
    auth = params.get("_auth") if isinstance(params, dict) else None
    if auth is None:
        if method in PRE_AUTH_ALLOWLIST:
            return True, None, None
        return False, None, f"missing _auth for {method} (login via auth.login)"
    if not isinstance(auth, dict) or not str(auth.get("jwt") or "").strip():
        return False, None, "malformed _auth: jwt required"
    try:
        claims = verify_rpc_jwt(str(auth.get("jwt")), base_dir)
    except RpcAuthError as exc:
        return False, None, exc.reason
    except Exception as exc:
        logger.exception("rpc_auth: verification error")
        return False, None, f"auth verification failed: {exc}"
    return True, claims, None


def auth_login(params: dict[str, Any], base_dir: str | Path) -> dict[str, Any]:
    """auth.login handler: exchange a daemon API key for a local JWT.

    Identity mode refuses — clients log in through fusion-identity and inject
    that jwt directly; this endpoint is the local-mode bootstrap only.
    """
    from .identity_integration import is_identity_enabled

    if is_identity_enabled():
        raise RpcAuthError("identity mode: login via fusion-identity, not auth.login")
    apikey = str(params.get("apikey") or params.get("key") or "").strip()
    if not apikey:
        raise RpcAuthError("apikey required")
    # #327: the static operator key (FUSION_AGENT_STUDIO_API_KEY, or the
    # persisted web_module_key file) also logs in without a persisted fk-
    # key — the gateway/CLI integration path.
    from .shell_registry import static_module_key

    static = static_module_key()
    key_id = ""
    if static and hmac.compare_digest(static, apikey):
        key_id = "static-env"
    else:
        from .apikey_manager import ApiKeyManager

        mgr = ApiKeyManager(base_dir)
        result = mgr.validate(apikey)
        if not result.get("valid"):
            raise RpcAuthError(f"invalid apikey: {result.get('reason', 'rejected')}")
        key_id = str(result.get("key_id", "apikey"))
    tid = str(params.get("tid") or "local").strip() or "local"
    issued = issue_local_jwt(base_dir, tid=tid, sub=key_id)
    issued["key_id"] = key_id
    return issued
