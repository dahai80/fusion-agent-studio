import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from agent_runtime.apikey_manager import ApiKeyManager
from agent_runtime.errors import ErrorCode, raise_api_error

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)

_default_manager = ApiKeyManager(Path.home() / ".fusion-agent-studio")


class ApiKeyAuth:
    def __init__(self, manager: Optional[ApiKeyManager] = None):
        self.manager = manager or _default_manager
        logger.info("ApiKeyAuth initialized with manager base_path=%s", self.manager.base_path)

    async def __call__(
        self,
        request: Request,
        raw_key: Optional[str] = Depends(_api_key_header),
    ) -> None:
        client_ip = self._extract_client_ip(request)
        agent_id = self._extract_agent_id(request)

        logger.debug(
            "ApiKeyAuth processing request: path=%s agent_id=%s client_ip=%s",
            request.url.path,
            agent_id,
            client_ip,
        )

        if not raw_key:
            logger.warning("API key missing for request path=%s client_ip=%s", request.url.path, client_ip)
            raise_api_error(ErrorCode.API_KEY_MISSING)

        result = self.manager.validate(raw_key, agent_id=agent_id, client_ip=client_ip)

        if not result.get("valid"):
            reason = result.get("reason", "unknown")
            logger.warning(
                "API key validation failed: reason=%s key_id=%s path=%s client_ip=%s",
                reason,
                result.get("key_id"),
                request.url.path,
                client_ip,
            )
            self._raise_for_reason(reason)
            return

        key_id = result.get("key_id")
        permissions = result.get("permissions", [])

        request.state.key_id = key_id
        request.state.permissions = permissions

        logger.info(
            "API key validated successfully: key_id=%s permissions=%s path=%s",
            key_id,
            permissions,
            request.url.path,
        )

    def _extract_client_ip(self, request: Request) -> Optional[str]:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
            logger.debug("Extracted client IP from x-forwarded-for: %s", client_ip)
            return client_ip

        client_ip = request.client.host if request.client else None
        logger.debug("Extracted client IP from request.client: %s", client_ip)
        return client_ip

    def _extract_agent_id(self, request: Request) -> Optional[str]:
        agent_id = request.query_params.get("agent_id")
        if not agent_id:
            path_parts = request.url.path.strip("/").split("/")
            for i, part in enumerate(path_parts):
                if part == "agents" and i + 1 < len(path_parts):
                    agent_id = path_parts[i + 1]
                    break

        logger.debug("Extracted agent_id=%s from request", agent_id)
        return agent_id

    def _raise_for_reason(self, reason: str) -> None:
        reason_lower = reason.lower()

        if "ip" in reason_lower or "forbidden" in reason_lower and "ip" in reason_lower:
            raise_api_error(ErrorCode.API_KEY_IP_FORBIDDEN)

        if "agent" in reason_lower or "restricted" in reason_lower and "agent" in reason_lower:
            raise_api_error(ErrorCode.API_KEY_AGENT_RESTRICTED)

        if "invalid" in reason_lower or "not found" in reason_lower or "expired" in reason_lower:
            raise_api_error(ErrorCode.API_KEY_INVALID)

        raise_api_error(ErrorCode.API_KEY_INVALID)
