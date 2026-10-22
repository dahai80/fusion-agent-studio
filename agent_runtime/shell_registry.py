"""#327: union-web shell module registration + heartbeat (PRD §5.2).

The daemon registers web/manifest.json with the fusion-gateway shell
registry (POST /api/v1/shell/manifests, fg-key auth) and keeps it alive
with periodic heartbeats so the module stays "online" in the shell menu
(heartbeat_timeout marks silent modules unreachable/greyed).

Env-gated — requires BOTH:
- FUSION_SHELL_GATEWAY_URL: gateway base, e.g. http://127.0.0.1:11432
- FUSION_GATEWAY_ADMIN_KEY: a gateway api_key authorized for /api/v1/*

Optional:
- FUSION_SHELL_MANIFEST: manifest path override (default web/manifest.json
  next to the repo root).
- FUSION_SHELL_UPSTREAM: #399 proxy target override (default the daemon
  HTTP port http://127.0.0.1:11442, which serves the SPA + /v1/*).

#399 (gateway#232 dynamic proxy): registration injects `upstream` +
`api_key` (the module key, same value the /shell proxy authenticates with)
into the manifest payload so the gateway can resolve /shell/<id>/ from the
dynamic registry — no static web_shell.modules config needed. api_key is
time from static_module_key() at registration; #436 (K4) stops sending it
entirely unless the static env override is set — the gateway (K1) issues an
fm- module key in the registration response, which is persisted to
~/.fusion-agent-studio/web_module_key (0600) and used for heartbeat auth.
Inbound gateway→studio auth dual-accepts the env static key + issued key
during the migration grace window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

GATEWAY_URL_ENV = "FUSION_SHELL_GATEWAY_URL"
ADMIN_KEY_ENV = "FUSION_GATEWAY_ADMIN_KEY"
MANIFEST_PATH_ENV = "FUSION_SHELL_MANIFEST"
UPSTREAM_ENV = "FUSION_SHELL_UPSTREAM"
MODULE_KEY_ENV = "FUSION_AGENT_STUDIO_API_KEY"
MODULE_KEY_FILE = Path.home() / ".fusion-agent-studio" / "web_module_key"
DEFAULT_UPSTREAM = "http://127.0.0.1:11442"
REGISTER_INTERVAL_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 5.0


def load_module_key() -> str:
    """#436 (K4): issued fm- key store — file content only (no env)."""
    try:
        data = MODULE_KEY_FILE.read_text(encoding="utf-8").strip()
        return data
    except OSError:
        return ""


def save_module_key(key: str) -> bool:
    """#436 (K4): persist gateway-issued key (0600). Never logs plaintext."""
    key = (key or "").strip()
    if not key:
        return False
    try:
        MODULE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODULE_KEY_FILE.write_text(key + "\n", encoding="utf-8")
        MODULE_KEY_FILE.chmod(0o600)
        logger.info("gateway-issued module key persisted: %s (%s)", MODULE_KEY_FILE, _mask_key(key))
        return True
    except OSError as e:
        logger.warning("module key file unwritable: %s", e)
        return False


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def _extract_issued_key(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for field in ("api_key", "issued_key", "key", "module_key"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("data")
    if isinstance(nested, dict):
        for field in ("api_key", "issued_key", "key", "module_key"):
            value = nested.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def static_module_key() -> str:
    """Module key injected by the gateway /shell proxy (Bearer upstream).

    Source order: FUSION_AGENT_STUDIO_API_KEY env > persisted
    ~/.fusion-agent-studio/web_module_key (0600) > empty (auth falls back
    to the ApiKeyManager store). #436 (K4): the file may hold the
    gateway-issued fm- key; on restart start.sh exports it as the env value.
    """
    env_key = os.environ.get(MODULE_KEY_ENV, "").strip()
    if env_key:
        return env_key
    return load_module_key()


def accepted_module_keys() -> list[str]:
    """#436 (K4): inbound dual-accept set — env static key + issued key.

    Gateway /shell proxy authenticates upstream with whichever key it holds
    for the module; during the migration grace window that may be the old
    static key while the issued fm- key is already persisted (or vice versa).
    """
    keys = []
    env_key = os.environ.get(MODULE_KEY_ENV, "").strip()
    if env_key:
        keys.append(env_key)
    file_key = load_module_key()
    if file_key and file_key not in keys:
        keys.append(file_key)
    return keys


def _default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "web" / "manifest.json"


def resolve_upstream(manifest: dict[str, Any]) -> str:
    """#399: /shell/<id>/ proxy target — env override > manifest > default.

    Mirrors the gateway's validateUpstreamURL: absolute http(s) URL only.
    """
    upstream = (
        os.environ.get(UPSTREAM_ENV, "").strip()
        or str(manifest.get("upstream") or "").strip()
        or DEFAULT_UPSTREAM
    )
    parsed = urlparse(upstream)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"shell upstream must be an absolute http(s) URL: {upstream}")
    return upstream.rstrip("/")


def build_registration_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """#399: inject upstream into the POST payload.

    #436 (K4): api_key is no longer self-supplied from the persisted file —
    the gateway (K1) issues an fm- key in the registration response, which
    _registry_loop_body persists. Only the static env override
    (FUSION_AGENT_STUDIO_API_KEY) is still sent, for backward compat with
    gateways that accept static keys during the grace window. The gateway
    keeps the key in memory only (APIKey zeroed in list responses).
    """
    payload = dict(manifest)
    payload["upstream"] = resolve_upstream(manifest)
    env_key = os.environ.get(MODULE_KEY_ENV, "").strip()
    if env_key:
        payload["api_key"] = env_key
    return payload


def shell_registration_enabled() -> bool:
    gateway = os.environ.get(GATEWAY_URL_ENV, "").strip()
    admin_key = os.environ.get(ADMIN_KEY_ENV, "").strip()
    return bool(gateway and admin_key)


def _load_manifest() -> dict[str, Any]:
    path = Path(os.environ.get(MANIFEST_PATH_ENV, "").strip() or _default_manifest_path())
    if not path.exists():
        raise FileNotFoundError(f"shell manifest not found: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not manifest.get("id"):
        raise ValueError(f"shell manifest missing id: {path}")
    logger.info("shell manifest loaded from %s (id=%s)", path, manifest["id"])
    return manifest


async def _registry_loop_body(client: Any, manifest: dict[str, Any], interval: float) -> None:
    module_id = str(manifest["id"])
    gateway = os.environ.get(GATEWAY_URL_ENV, "").strip().rstrip("/")
    admin_key = os.environ.get(ADMIN_KEY_ENV, "").strip()
    admin_headers = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}
    try:
        payload = build_registration_payload(manifest)
    except Exception as e:
        logger.error("shell registration disabled (manifest unusable): %s", e)
        return
    logger.info(
        "shell manifest upstream=%s api_key=%s",
        payload["upstream"],
        "set" if payload.get("api_key") else "unset",
    )
    registered = False
    while True:
        try:
            if not registered:
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests",
                    json=payload,
                    headers=admin_headers,
                )
                if resp.status_code == 200:
                    registered = True
                    logger.info("shell manifest registered: %s", module_id)
                    # #436 (K4): persist gateway-issued key — returned in
                    # plaintext exactly once, in the registration response.
                    issued = _extract_issued_key(_safe_json(resp))
                    if issued and issued != load_module_key():
                        save_module_key(issued)
                    elif issued:
                        logger.info("gateway-issued module key unchanged, not rewriting file")
                else:
                    logger.warning(
                        "shell manifest register failed: %d %s",
                        resp.status_code,
                        resp.text[:200],
                    )
            else:
                # #436 (K4): heartbeat authenticates with the issued fm- key
                # (or static override); admin key only as last-resort fallback.
                module_key = load_module_key()
                beat_key = module_key or os.environ.get(MODULE_KEY_ENV, "").strip() or admin_key
                headers = {"Authorization": f"Bearer {beat_key}", "Content-Type": "application/json"}
                resp = await client.post(
                    f"{gateway}/api/v1/shell/manifests/{module_id}/heartbeat",
                    headers=headers,
                )
                if resp.status_code == 404:
                    # Gateway restarted and dropped dynamic manifests.
                    logger.info("shell heartbeat 404, re-registering %s", module_id)
                    registered = False
                elif resp.status_code == 401 and module_key:
                    # Issued key rotated/revoked — re-register via admin key
                    # to pick up the fresh issued key.
                    logger.warning("shell heartbeat 401 (issued key invalid), re-registering %s", module_id)
                    registered = False
                elif resp.status_code != 200:
                    logger.warning(
                        "shell heartbeat failed: %d %s",
                        resp.status_code,
                        resp.text[:200],
                    )
        except asyncio.CancelledError:
            logger.info("shell registry loop cancelled (module=%s)", module_id)
            raise
        except Exception as e:
            logger.warning("shell registry loop error (will retry): %s", e)
        await asyncio.sleep(interval)


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:
        return None


async def shell_registry_loop() -> None:
    """Register once, heartbeat forever; re-register after gateway restarts.

    Runs as a daemon background task. All failures are logged and retried on
    the next tick — registration must never take the daemon down.
    """
    try:
        manifest = _load_manifest()
    except Exception as e:
        logger.error("shell registration disabled (manifest unusable): %s", e)
        return
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        await _registry_loop_body(client, manifest, REGISTER_INTERVAL_SECONDS)
