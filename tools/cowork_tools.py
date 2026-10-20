"""Cowork tool — run fusion-cowork workflows as agent-studio tool steps (issue #402)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from .base import BaseTool

logger = logging.getLogger(__name__)

COWORK_URL_ENV = "FUSION_COWORK_URL"
COWORK_API_KEY_ENV = "FUSION_COWORK_WEB_API_KEY"
DEFAULT_COWORK_URL = "http://127.0.0.1:11461"

# cowork WorkflowStatus terminal values (engine/workflow.py WorkflowStatus) +
# RunRegistry record["status"] overrides (cancelled/failed on async paths).
_TERMINAL_STATUSES = {"success", "failed", "cancelled", "partial"}
# paused = human redirect (W8), pending = queued not yet started.
_BLOCKING_HUMAN_STATUSES = {"paused", "pending"}

_POLL_INTERVAL = 1.0
_MIN_STALL_SECONDS = 3
_MAX_STEPS_IN_RESULT = 50


def cowork_available() -> bool:
    # always available: the tool degrades gracefully at call time when the
    # cowork web API is unreachable (fail visibly in the tool result).
    return True


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _headers() -> dict:
    # cowork web API auth: FUSION_COWORK_WEB_API_KEY (same env the server checks;
    # gateway proxy injects it — tool honors it directly for direct-upstream calls).
    api_key = os.environ.get(COWORK_API_KEY_ENV, "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _base_url() -> str:
    return os.environ.get(COWORK_URL_ENV, DEFAULT_COWORK_URL).rstrip("/")


def _run_output(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)


def _progress_signature(record: dict) -> tuple:
    steps = record.get("steps") or []
    return (
        len(steps),
        tuple(str(s.get("status", "")) for s in steps),
        len(record.get("artifacts") or []),
    )


def _node_outputs(record: dict) -> dict:
    # final node outputs: node_id -> output_data (skip empty), capped.
    out = {}
    for step in (record.get("steps") or [])[:_MAX_STEPS_IN_RESULT]:
        node_id = str(step.get("node_id") or "")
        data = step.get("output_data")
        if not node_id or not isinstance(data, dict) or not data:
            continue
        out[node_id] = data
    return out


def _summarize_run(record: dict) -> dict:
    status = str(record.get("status") or "")
    result = {
        "run_id": record.get("run_id", ""),
        "workflow_id": record.get("workflow_id", ""),
        "workflow_name": record.get("workflow_name", ""),
        "status": status,
    }
    if record.get("error"):
        result["error"] = str(record.get("error"))[:2000]
    if record.get("review"):
        result["review"] = record.get("review")
    if status in _TERMINAL_STATUSES:
        result["summary"] = (record.get("result") or {}).get("summary", "")
        result["node_outputs"] = _node_outputs(record)
        result["artifacts"] = [
            {
                "node_id": a.get("node_id", ""),
                "name": a.get("name", ""),
                "type": a.get("type", ""),
                "path": a.get("path", ""),
                "truncated": bool(a.get("truncated")),
            }
            for a in (record.get("artifacts") or [])[:_MAX_STEPS_IN_RESULT]
        ]
    return result


class CoworkWorkflowRunTool(BaseTool):
    name = "cowork_workflow_run"
    description = (
        "Run a fusion-cowork workflow (DAG) as a tool step. Start by workflow_id or a "
        "natural-language prompt (cowork generates the DAG server-side), optionally pass "
        "inputs, then wait and return run status + final node outputs. When the run needs "
        "human approval (paused / approval-stalled), returns a pending status with the "
        "run_id instead of blocking. Upstream: fusion-cowork web API (:11461, "
        "FUSION_COWORK_URL override, auth via FUSION_COWORK_WEB_API_KEY)."
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["run", "status"],
            "description": "run = start a new run; status = poll an existing run_id",
            "default": "run",
        },
        "workflow_id": {
            "type": "string",
            "description": "cowork workflow id (from cowork /api/workflows); required for action=run unless prompt given",
        },
        "prompt": {
            "type": "string",
            "description": "natural-language description; cowork generates the DAG via /api/ai/generate-workflow",
        },
        "inputs": {
            "type": "object",
            "description": "optional run inputs forwarded verbatim in the run body (upstream honors when supported)",
            "default": {},
        },
        "wait": {
            "type": "boolean",
            "description": "true = poll to terminal/pending state; false = fire-and-forget, returns run_id",
            "default": True,
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "max seconds to wait before returning current status (action=run, wait=true)",
            "default": 120,
        },
        "stall_seconds": {
            "type": "integer",
            "description": "seconds without step progress + pending approvals before reporting pending_approval",
            "default": 15,
        },
        "run_id": {
            "type": "string",
            "description": "run id to poll (required for action=status)",
        },
    }

    async def execute(self, **kwargs) -> str:
        action = str(kwargs.get("action") or "run").strip().lower()
        base_url = _base_url()
        logger.info(
            "cowork_workflow_run action=%s base=%s workflow_id=%s prompt_len=%d wait=%s",
            action,
            base_url,
            kwargs.get("workflow_id") or "",
            len(str(kwargs.get("prompt") or "")),
            kwargs.get("wait", True),
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if action == "status":
                    return await self._poll_status(client, base_url, str(kwargs.get("run_id") or ""))
                return await self._run(client, base_url, kwargs)
        except httpx.ConnectError as e:
            logger.warning("cowork_workflow_run upstream unreachable: %s", e)
            return _run_output({
                "error": f"cowork upstream unreachable at {base_url}: {e}",
                "hint": f"start fusion-cowork web serve or set {COWORK_URL_ENV}",
            })
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("detail", {}).get("message", "")
            except Exception:
                detail = e.response.text[:500]
            logger.warning("cowork_workflow_run upstream %s: %s", e.response.status_code, detail)
            return _run_output({"error": f"cowork API {e.response.status_code}: {detail}"})
        except httpx.TimeoutException:
            logger.warning("cowork_workflow_run upstream timeout")
            return _run_output({"error": f"cowork upstream timeout at {base_url}"})

    async def _run(self, client: httpx.AsyncClient, base_url: str, kwargs: dict) -> str:
        workflow_id = str(kwargs.get("workflow_id") or "").strip()
        prompt = str(kwargs.get("prompt") or "").strip()
        inputs = kwargs.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        wait = _as_bool(kwargs.get("wait"), True)
        timeout_seconds = max(1, _as_int(kwargs.get("timeout_seconds"), 120))
        stall_seconds = max(_MIN_STALL_SECONDS, _as_int(kwargs.get("stall_seconds"), 15))

        if not workflow_id and not prompt:
            return _run_output({"error": "workflow_id or prompt is required for action=run"})

        body: dict = {}
        if workflow_id:
            body["workflow_id"] = workflow_id
        else:
            gen = await client.post(
                f"{base_url}/api/ai/generate-workflow",
                headers=_headers(),
                json={"prompt": prompt},
            )
            gen.raise_for_status()
            body["workflow"] = gen.json().get("workflow") or {}
            body["name"] = str(body["workflow"].get("name") or "")
        if inputs:
            body["inputs"] = inputs

        resp = await client.post(f"{base_url}/api/runs", headers=_headers(), json=body)
        resp.raise_for_status()
        started = resp.json()
        run_id = str(started.get("run_id") or "")
        if not run_id:
            return _run_output({"error": "cowork run start returned no run_id", "response": started})

        if not wait:
            logger.info("cowork_workflow_run fire-and-forget run_id=%s", run_id)
            return _run_output({
                "run_id": run_id,
                "status": started.get("status", "running"),
                "mode": "fire_and_forget",
                "hint": f"poll later with action=status run_id={run_id}",
            })

        return await self._wait_loop(client, base_url, run_id, timeout_seconds, stall_seconds)

    async def _wait_loop(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        run_id: str,
        timeout_seconds: int,
        stall_seconds: int,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        stall_deadline = time.monotonic() + stall_seconds
        last_signature = None
        status = "running"
        record: dict = {}
        while time.monotonic() < deadline:
            resp = await client.get(f"{base_url}/api/runs/{run_id}", headers=_headers())
            resp.raise_for_status()
            record = resp.json()
            status = str(record.get("status") or "")
            if status in _TERMINAL_STATUSES:
                logger.info("cowork_workflow_run terminal run_id=%s status=%s", run_id, status)
                return _run_output(_summarize_run(record))
            if status in _BLOCKING_HUMAN_STATUSES:
                logger.info("cowork_workflow_run human-gated run_id=%s status=%s", run_id, status)
                out = _summarize_run(record)
                out["pending"] = True
                out["hint"] = f"poll later with action=status run_id={run_id}"
                return _run_output(out)

            signature = _progress_signature(record)
            if signature != last_signature:
                last_signature = signature
                stall_deadline = time.monotonic() + stall_seconds
            elif time.monotonic() >= stall_deadline:
                # no step progress for stall_seconds: check cowork approvals
                # (node-level CONFIRM holds the run while status stays running)
                pending = await self._pending_approvals(client, base_url)
                if pending:
                    logger.info(
                        "cowork_workflow_run approval-stalled run_id=%s approvals=%d",
                        run_id,
                        len(pending),
                    )
                    out = _summarize_run(record)
                    out["status"] = "pending_approval"
                    out["pending"] = True
                    out["approvals"] = pending
                    out["hint"] = (
                        f"confirm in cowork UI, then poll with action=status run_id={run_id}"
                    )
                    return _run_output(out)
                stall_deadline = time.monotonic() + stall_seconds
            await asyncio.sleep(_POLL_INTERVAL)

        logger.info("cowork_workflow_run wait timeout run_id=%s status=%s", run_id, status)
        out = _summarize_run(record)
        out["pending"] = True
        out["hint"] = f"poll later with action=status run_id={run_id}"
        return _run_output(out)

    async def _pending_approvals(self, client: httpx.AsyncClient, base_url: str) -> list:
        try:
            resp = await client.get(f"{base_url}/api/permissions/pending", headers=_headers())
            resp.raise_for_status()
            return resp.json().get("approvals") or []
        except (httpx.HTTPStatusError, httpx.HTTPError) as e:
            # approvals endpoint optional (503 when permission manager off) —
            # degrade to continue polling instead of failing the run wait.
            logger.debug("cowork approvals poll unavailable: %s", e)
            return []

    async def _poll_status(self, client: httpx.AsyncClient, base_url: str, run_id: str) -> str:
        if not run_id:
            return _run_output({"error": "run_id is required for action=status"})
        resp = await client.get(f"{base_url}/api/runs/{run_id}", headers=_headers())
        resp.raise_for_status()
        out = _summarize_run(resp.json())
        if out.get("status") not in _TERMINAL_STATUSES:
            out["pending"] = True
            out["hint"] = f"poll again with action=status run_id={run_id}"
        return _run_output(out)
