"""#274: Chat↔FSB (fusion-smallbusiness) integration client.

Env-gated via FUSION_FSB_ENABLED=1. Calls the FSB backend HTTP API:
- POST /api/v1/fsb/workspace/{wsId}/agent/bind    {agentId}
- POST /api/v1/fsb/workspace/{wsId}/agent/unbind
- POST /api/v1/fsb/workspace/{wsId}/chat/run      {query, inputData}

Unset (default) = FSB integration off — all client methods no-op/return None,
local Agent Studio chat behavior unchanged (CI/local-dev green).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_FSB_URL_ENV = "FUSION_FSB_URL"
_FSB_ENABLED_ENV = "FUSION_FSB_ENABLED"
_DEFAULT_URL = "http://127.0.0.1:11460"
_TIMEOUT = float(os.environ.get("FUSION_FSB_TIMEOUT", "5"))


def is_fsb_enabled() -> bool:
    return os.environ.get(_FSB_ENABLED_ENV, "0").strip() == "1"


def _fsb_url() -> str:
    return os.environ.get(_FSB_URL_ENV, _DEFAULT_URL).rstrip("/")


class FSBClient:
    """Thin HTTP client to the fusion-smallbusiness backend.

    Constructed lazily. Fail-soft: every method catches network/HTTP errors,
    logs at warning, returns None (chat continues without FSB). Never raises
    to callers — FSB is an auxiliary integration, not a hard dependency.
    """

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self._base_url = (base_url or _fsb_url()).rstrip("/")
        self._timeout = timeout if timeout is not None else _TIMEOUT

    def bind(self, workspace_id: str, agent_id: str) -> dict[str, Any] | None:
        if not is_fsb_enabled() or not workspace_id or not agent_id:
            return None
        import httpx

        try:
            resp = httpx.post(
                f"{self._base_url}/api/v1/fsb/workspace/{workspace_id}/agent/bind",
                json={"agentId": agent_id},
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                logger.warning("fsb bind ws=%s agent=%s -> %s %s", workspace_id, agent_id, resp.status_code, resp.text[:200])
                return None
            logger.info("fsb bind ok ws=%s agent=%s", workspace_id, agent_id)
            return resp.json()
        except Exception as e:
            logger.warning("fsb bind call failed (fail-soft): %s", e)
            return None

    def unbind(self, workspace_id: str) -> dict[str, Any] | None:
        if not is_fsb_enabled() or not workspace_id:
            return None
        import httpx

        try:
            resp = httpx.post(
                f"{self._base_url}/api/v1/fsb/workspace/{workspace_id}/agent/unbind",
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                logger.warning("fsb unbind ws=%s -> %s %s", workspace_id, resp.status_code, resp.text[:200])
                return None
            logger.info("fsb unbind ok ws=%s", workspace_id)
            return resp.json()
        except Exception as e:
            logger.warning("fsb unbind call failed (fail-soft): %s", e)
            return None

    def chat_run(self, workspace_id: str, query: str, input_data: dict | None = None) -> dict[str, Any] | None:
        """NL query -> FSB intent match -> workflow run. Returns FSB run dict or None.

        None = FSB disabled/unreachable; 404 (no matching workflow) returned as
        {"matched": False, "status": 404} so the chat can show "no workflow matched".
        """
        if not is_fsb_enabled() or not workspace_id or not query:
            return None
        import httpx

        try:
            resp = httpx.post(
                f"{self._base_url}/api/v1/fsb/workspace/{workspace_id}/chat/run",
                json={"query": query, "inputData": input_data or {}},
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                logger.info("fsb chat_run ws=%s no workflow matched", workspace_id)
                return {"matched": False, "status": 404}
            if resp.status_code >= 400:
                logger.warning("fsb chat_run ws=%s -> %s %s", workspace_id, resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            logger.info("fsb chat_run ws=%s matched=%s", workspace_id, data.get("matched"))
            return data
        except Exception as e:
            logger.warning("fsb chat_run call failed (fail-soft): %s", e)
            return None


_singleton: FSBClient | None = None


def get_fsb_client() -> FSBClient:
    global _singleton
    if _singleton is None:
        _singleton = FSBClient()
    return _singleton
