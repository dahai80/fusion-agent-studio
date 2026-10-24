"""Sandbox selection for runs (#432).

A run names its execution sandbox: "local" runs inside this daemon, while
"multi-node:<node_id>" delegates execution to the fusion-multi-node cluster
router. The router is an HTTP service; unreachable routers fail the run
visibly instead of silently falling back to local execution.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# #450: multi-node master serves on 11452 (port registry + start.sh); 11457
# was never registered — delegation without FUSION_CLUSTER_URL failed with
# "cluster router unreachable".
DEFAULT_CLUSTER_URL = "http://127.0.0.1:11452"
LOCAL = "local"


@dataclass
class SandboxTarget:
    raw: str
    mode: str
    node: str = ""

    @property
    def is_local(self) -> bool:
        return self.mode == LOCAL

    def to_dict(self) -> dict:
        return {"sandbox": self.raw, "mode": self.mode, "node": self.node}


def parse_sandbox(value: str | None) -> SandboxTarget:
    raw = (value or LOCAL).strip() or LOCAL
    if raw == LOCAL:
        return SandboxTarget(raw=LOCAL, mode=LOCAL)
    if raw.startswith("multi-node:"):
        node = raw[len("multi-node:"):].strip()
        if not node:
            raise ValueError("sandbox multi-node:<node_id> requires a node id")
        return SandboxTarget(raw=raw, mode="multi-node", node=node)
    raise ValueError(f"unsupported sandbox '{raw}' (use local or multi-node:<node_id>)")


def cluster_url() -> str:
    return os.environ.get("FUSION_CLUSTER_URL", DEFAULT_CLUSTER_URL).strip() or DEFAULT_CLUSTER_URL


async def delegate_run(
    target: SandboxTarget,
    task_id: str,
    agent_id: str,
    graph_id: str,
    goal: str,
    client=None,
) -> dict:
    import httpx

    payload = {
        "name": f"{task_id}:{agent_id or graph_id}",
        "mode": "agent",
        "model_name": agent_id or graph_id,
        "priority": 5,
    }
    headers = {"X-Fusion-Target-Node": target.node, "X-Fusion-Run-Id": task_id}
    token = os.environ.get("FUSION_CLUSTER_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{cluster_url().rstrip('/')}/api/tasks/submit"
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "sandbox delegate failed run=%s node=%s status=%s body=%s",
                task_id,
                target.node,
                resp.status_code,
                resp.text[:200],
            )
            return {
                "ok": False,
                "error": f"cluster router {url} returned {resp.status_code}",
                "node": target.node,
            }
        data = resp.json() if resp.content else {}
        cluster_task_id = str((data or {}).get("task_id", ""))
        logger.info(
            "sandbox delegated run=%s node=%s cluster_task=%s",
            task_id,
            target.node,
            cluster_task_id,
        )
        return {
            "ok": True,
            "node": target.node,
            "cluster_task_id": cluster_task_id,
            "assigned_nodes": list((data or {}).get("assigned_nodes") or []),
        }
    except Exception as e:
        logger.warning("sandbox delegate error run=%s node=%s: %s", task_id, target.node, e)
        return {"ok": False, "error": f"cluster router unreachable: {e}", "node": target.node}
    finally:
        if owns_client:
            await client.aclose()
