# Issue #683: fusion-security guardrails thin client (P2.3).
#
# LLM input/output filtering as a service — fusion-security HTTP surface:
#   POST /api/v1/guardrails/input   prompt-injection filter (rule tier)
#   POST /api/v1/guardrails/output  secret-leak / tool-invocation / exfil filter
# Auth: scan:read API key + X-Tenant-Id; every block lands in the tenant
# audit stream on the producer side.
#
# Opt-in and fail-open by design: disabled by default, and when the service
# is unreachable / errors the turn proceeds (never hard-fail the loop) —
# the only hard stop is an explicit action=block verdict.
#
# Env:
#   FUSION_GUARDRAILS_ENABLED  1/true enables the integration (default off)
#   FUSION_SECURITY_URL        default http://127.0.0.1:11454
#   FUSION_GUARDRAILS_API_KEY  scan:read API key (Bearer)
#   FUSION_TENANT_ID           tenant header

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11454"
DEFAULT_TIMEOUT_S = 3.0


def guardrails_enabled() -> bool:
    return os.environ.get("FUSION_GUARDRAILS_ENABLED", "").lower() in ("1", "true", "yes", "on")


class GuardrailsClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        tenant_id: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Any = None,
    ):
        self._base_url = (
            base_url
            or os.environ.get("FUSION_SECURITY_URL")
            or os.environ.get("FUSION_GUARDRAILS_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("FUSION_GUARDRAILS_API_KEY", "")
        self._tenant_id = tenant_id or os.environ.get("FUSION_TENANT_ID", "")
        self._timeout_s = timeout_s
        self._transport = transport

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._tenant_id:
            headers["X-Tenant-Id"] = self._tenant_id
        return headers

    async def _scan(self, endpoint: str, text: str) -> dict:
        # Returns {"action": "allow"|"block", "reason": str, "category": str}.
        # Any transport/HTTP/shape failure degrades to allow (fail-open) —
        # the guardrails service must never take the agent loop down.
        if not text or not text.strip():
            return {"action": "allow", "reason": "", "category": ""}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s, transport=self._transport) as client:
                resp = await client.post(
                    f"{self._base_url}{endpoint}",
                    json={"text": text},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("guardrails %s scan skipped (service unavailable): %s", endpoint, e)
            return {"action": "allow", "reason": "", "category": "", "skipped": True}
        action = str((data or {}).get("action", "allow") or "allow").lower()
        verdict = {
            "action": "block" if action == "block" else "allow",
            "reason": str((data or {}).get("reason", "") or ""),
            "category": str((data or {}).get("category", "") or ""),
        }
        if verdict["action"] == "block":
            logger.warning(
                "guardrails %s BLOCK category=%s reason=%s",
                endpoint,
                verdict["category"],
                verdict["reason"],
            )
        return verdict

    async def check_input(self, text: str) -> dict:
        return await self._scan("/api/v1/guardrails/input", text)

    async def check_output(self, text: str) -> dict:
        return await self._scan("/api/v1/guardrails/output", text)


REDACTION_NOTICE = (
    "[Model output withheld by guardrails: the response matched a "
    "sensitive-content policy (secret leakage / data exfiltration). "
    "Rephrase the request or contact the administrator.]"
)


async def check_llm_input(text: str) -> dict:
    # Module-level seam used by the runtime — no-op when disabled.
    if not guardrails_enabled():
        return {"action": "allow", "reason": "", "category": "", "skipped": True}
    return await GuardrailsClient().check_input(text)


async def check_llm_output(text: str) -> dict:
    if not guardrails_enabled():
        return {"action": "allow", "reason": "", "category": "", "skipped": True}
    return await GuardrailsClient().check_output(text)
