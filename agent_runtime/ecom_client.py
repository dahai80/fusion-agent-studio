"""Console↔ecommerce capability-spine client (plan
f-ecommerce-agent-c-s-p-1020 §5.2 — the fsb_client pattern, second
consumer).

Env-gated via FUSION_ECOM_ENABLED=1. Talks to the e-commerce console:
- GET  /api/ecommerce/agent-platform/capabilities   (agent tool manifest)
- POST /api/ecommerce/agent-platform/call           (single audited door)

Auth: bearer FUSION_AGENT_BRIDGE_KEY — the console holds the same value;
it is a service-to-service secret, never exposed to agents or browsers.

Unset (default) = integration off — every method returns None, Agent
Studio behavior unchanged (CI/local-dev green). Fail-soft by design: the
console is an auxiliary integration here, never a hard dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ECOM_ENABLED_ENV = "FUSION_ECOM_ENABLED"
_ECOM_URL_ENV = "FUSION_ECOM_URL"
_ECOM_KEY_ENV = "FUSION_AGENT_BRIDGE_KEY"
_DEFAULT_URL = "http://127.0.0.1:8787"
_TIMEOUT = float(os.environ.get("FUSION_ECOM_TIMEOUT", "60"))


def is_ecom_enabled() -> bool:
    return os.environ.get(_ECOM_ENABLED_ENV, "0").strip() == "1"


def _ecom_url() -> str:
    return os.environ.get(_ECOM_URL_ENV, _DEFAULT_URL).rstrip("/")


def _bridge_key() -> str:
    return os.environ.get(_ECOM_KEY_ENV, "").strip()


class EcomClient:
    """Thin HTTP client to the e-commerce console's capability spine.

    Fail-soft: network/HTTP errors log at warning and return None; callers
    degrade to "capabilities unavailable", never crash the agent runtime.
    """

    def __init__(self, base_url: str | None = None,
                 timeout: float | None = None):
        self._base_url = (base_url or _ecom_url()).rstrip("/")
        self._timeout = timeout if timeout is not None else _TIMEOUT

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {_bridge_key()}"} \
            if _bridge_key() else {}

    def capabilities(self) -> list[dict[str, Any]] | None:
        """Agent tool manifest for the console's curated registry."""
        if not is_ecom_enabled():
            return None
        import httpx

        try:
            resp = httpx.get(
                f"{self._base_url}/api/ecommerce/agent-platform/capabilities",
                headers=self._headers(), timeout=self._timeout)
            if resp.status_code >= 400:
                logger.warning("ecom capabilities -> %s %s",
                               resp.status_code, resp.text[:200])
                return None
            return resp.json().get("capabilities") or []
        except Exception as e:
            logger.warning("ecom capabilities failed (fail-soft): %s", e)
            return None

    def call(self, capability_id: str, params: dict[str, Any] | None,
             *, agent_id: str, version: int, publisher: str) \
            -> dict[str, Any] | None:
        """Dispatch one capability through the audited door.

        Returns the console's envelope {capability, source, mutation, data},
        an error dict {"error": ..., "status": <code>} for console-level
        denials (draft mutation, RBAC, disabled capability), or None when
        the integration is off / unreachable.
        """
        if not is_ecom_enabled() or not capability_id:
            return None
        import httpx

        try:
            resp = httpx.post(
                f"{self._base_url}/api/ecommerce/agent-platform/call",
                headers=self._headers(),
                json={
                    "capability_id": capability_id,
                    "params": params or {},
                    "agent_id": agent_id,
                    "version": version,
                    "publisher": publisher,
                },
                timeout=self._timeout)
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("detail", resp.text[:200])
                except Exception:
                    detail = resp.text[:200]
                logger.warning("ecom call %s agent=%s -> %s %s",
                               capability_id, agent_id, resp.status_code,
                               str(detail)[:200])
                return {"error": detail, "status": resp.status_code}
            return resp.json()
        except Exception as e:
            logger.warning("ecom call %s failed (fail-soft): %s",
                           capability_id, e)
            return None


_singleton: EcomClient | None = None


def get_ecom_client() -> EcomClient:
    global _singleton
    if _singleton is None:
        _singleton = EcomClient()
    return _singleton
