"""ResourceLease — 独占资源租约协议 (M1-4, §5.3).

F1 结构性解法: 独占资源 (playwright browser / gpu / disk) 必须显式租约.
lease_apply → granted 或 queued(排队, 不 kill). ttl 到期 → expired + 证据 + team 告警.
消灭"按进程年龄猜锁主" → 先查事实(证据)再动资源.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .persistence import AgentStore

logger = logging.getLogger(__name__)

KIND_EXCLUSIVE = "exclusive"
KIND_SHARED_SLOT = "shared_slot"
KIND_ADVISORY = "advisory"

LEASE_GRANTED = "granted"
LEASE_QUEUED = "queued"
LEASE_RELEASED = "released"
LEASE_EXPIRED = "expired"

DEFAULT_TTL = 1800.0


class ResourceLease:
    """资源租约管理器. daemon 单写者, agent/GUI 只读 + RPC 提议."""

    def __init__(self, store: AgentStore | None = None):
        self._store = store
        self._seeded = False
        if store is not None:
            self.seed_default_resources()
        logger.info(
            "ResourceLease initialized (store=%s, seeded=%s)",
            "on" if store else "off",
            self._seeded,
        )

    def seed_default_resources(self) -> None:
        # §5.3 默认资源注册表. 幂等 (register_resource upsert).
        if self._store is None or self._seeded:
            return
        defaults = [
            ("browser:douyin_profile", KIND_EXCLUSIVE, 1, "publisher/commenter Playwright session"),
            ("gpu:mlx", KIND_SHARED_SLOT, 2, "script/tts/analyze LLM calls"),
            ("gpu:comfyui", KIND_SHARED_SLOT, 1, "imager/voicer ComfyUI graph"),
            ("disk:out", KIND_ADVISORY, 0, "synthesis/ingress (capacity advisory only)"),
        ]
        for rid, kind, slots, desc in defaults:
            self._store.register_resource(rid, kind, slots, desc, "default")
        self._seeded = True
        logger.info("ResourceLease seeded %d default resources", len(defaults))

    def lease_apply(
        self,
        resource_id: str,
        task_id: str = "",
        owner_role: str = "",
        owner_agent: str = "",
        ttl: float = DEFAULT_TTL,
        team: str = "default",
    ) -> dict[str, Any]:
        # 申请租约. exclusive/shared_slot: 槽位满 → queued(排队). advisory: 永远 granted.
        if self._store is None:
            return {"status": "granted", "lease_id": "", "reason": "no_store"}
        resource = self._store.get_resource(resource_id)
        if resource is None:
            logger.warning("lease_apply unknown resource: %s", resource_id)
            return {"status": "error", "error": f"unknown resource: {resource_id}"}
        kind = resource["kind"]
        slots = resource["slots"]
        now = time.time()
        lease_id = f"lease_{uuid.uuid4().hex[:12]}"

        if kind == KIND_ADVISORY:
            lease = {
                "lease_id": lease_id,
                "resource_id": resource_id,
                "task_id": task_id,
                "owner_role": owner_role,
                "owner_agent": owner_agent,
                "status": LEASE_GRANTED,
                "position": 0,
                "ttl": 0,
                "granted_at": now,
                "expires_at": 0,
                "released_at": 0,
                "reason": "",
                "team": team,
            }
            self._store.save_lease(lease)
            logger.info(
                "lease granted (advisory): %s resource=%s task=%s",
                lease_id,
                resource_id,
                task_id,
            )
            return {"status": "granted", "lease_id": lease_id, "position": 0}

        active = self._store.count_active_leases(resource_id)
        if active < slots:
            expires_at = now + ttl if ttl > 0 else 0
            lease = {
                "lease_id": lease_id,
                "resource_id": resource_id,
                "task_id": task_id,
                "owner_role": owner_role,
                "owner_agent": owner_agent,
                "status": LEASE_GRANTED,
                "position": 0,
                "ttl": ttl,
                "granted_at": now,
                "expires_at": expires_at,
                "released_at": 0,
                "reason": "",
                "team": team,
            }
            self._store.save_lease(lease)
            logger.info(
                "lease granted: %s resource=%s task=%s expires=%d",
                lease_id,
                resource_id,
                task_id,
                expires_at,
            )
            return {
                "status": "granted",
                "lease_id": lease_id,
                "position": 0,
                "expires_at": expires_at,
            }

        position = self._store.max_queued_position(resource_id) + 1
        lease = {
            "lease_id": lease_id,
            "resource_id": resource_id,
            "task_id": task_id,
            "owner_role": owner_role,
            "owner_agent": owner_agent,
            "status": LEASE_QUEUED,
            "position": position,
            "ttl": ttl,
            "granted_at": now,
            "expires_at": 0,
            "released_at": 0,
            "reason": "queued_wait_slot",
            "team": team,
        }
        self._store.save_lease(lease)
        logger.info(
            "lease queued: %s resource=%s task=%s position=%d",
            lease_id,
            resource_id,
            task_id,
            position,
        )
        return {"status": "queued", "lease_id": lease_id, "position": position}

    def lease_release(self, lease_id: str, reason: str = "") -> dict[str, Any]:
        # 显式释放. queued 也释放 (取消排队). 释放后 promote 排队中下一个.
        if self._store is None:
            return {"status": "released", "reason": "no_store"}
        lease = self._store.get_lease(lease_id)
        if lease is None:
            logger.warning("lease_release unknown: %s", lease_id)
            return {"status": "error", "error": "lease not found"}
        if lease["status"] in (LEASE_RELEASED, LEASE_EXPIRED):
            return {"status": lease["status"], "reason": "already terminal"}
        was_granted = lease["status"] == LEASE_GRANTED
        now = time.time()
        lease["status"] = LEASE_RELEASED
        lease["released_at"] = now
        lease["reason"] = reason
        self._store.save_lease(lease)
        logger.info(
            "lease released: %s resource=%s reason=%s",
            lease_id,
            lease["resource_id"],
            reason,
        )
        promoted_id = ""
        if was_granted:
            promoted = self._promote_queued(lease["resource_id"], lease["team"])
            if promoted:
                promoted_id = promoted.get("lease_id", "")
        # #319: surface promoted lease so daemon can broadcast resource.lease_granted.
        return {"status": "released", "lease_id": lease_id, "promoted_lease": promoted_id}

    def _promote_queued(self, resource_id: str, team: str) -> dict | None:
        # 释放后把排队第一个 promote 为 granted (按 position 升序).
        # #319: return promoted lease so caller can broadcast resource.lease_granted.
        queued = self._store.list_leases(resource_id=resource_id, status=LEASE_QUEUED, team=team)
        if not queued:
            return None
        queued.sort(key=lambda x: x["position"])
        nxt = queued[0]
        now = time.time()
        ttl = nxt.get("ttl", DEFAULT_TTL)
        expires_at = now + ttl if ttl > 0 else 0
        nxt["status"] = LEASE_GRANTED
        nxt["position"] = 0
        nxt["granted_at"] = now
        nxt["expires_at"] = expires_at
        nxt["reason"] = ""
        self._store.save_lease(nxt)
        logger.info(
            "lease promoted: %s resource=%s task=%s",
            nxt["lease_id"],
            resource_id,
            nxt.get("task_id", ""),
        )
        return nxt

    def expiry_sweep(self, now: float | None = None) -> list[dict]:
        # ttl 到期未释放 → expired + 返回清单 (daemon 写证据 + team 告警).
        # #319: each expired dict carries "promoted_lease" so daemon broadcasts
        # both resource.lease_expired and resource.lease_granted (promoted successor).
        if self._store is None:
            return []
        if now is None:
            now = time.time()
        expired = self._store.list_expired_leases(now)
        for lease in expired:
            lease["status"] = LEASE_EXPIRED
            lease["released_at"] = now
            lease["reason"] = "ttl_expired"
            self._store.save_lease(lease)
            logger.warning(
                "lease EXPIRED: %s resource=%s task=%s expires_at=%d",
                lease["lease_id"],
                lease["resource_id"],
                lease.get("task_id", ""),
                lease["expires_at"],
            )
            promoted = self._promote_queued(lease["resource_id"], lease["team"])
            lease["promoted_lease"] = promoted.get("lease_id", "") if promoted else ""
        return expired

    def list_resources(self, team: str = "") -> list[dict]:
        if self._store is None:
            return []
        return self._store.list_resources(team)

    def list_leases(self, resource_id: str = "", status: str = "", team: str = "") -> list[dict]:
        if self._store is None:
            return []
        return self._store.list_leases(resource_id, status, team)

    def get_lease(self, lease_id: str) -> dict | None:
        if self._store is None:
            return None
        return self._store.get_lease(lease_id)

    def register_resource(
        self,
        resource_id: str,
        kind: str,
        slots: int = 1,
        description: str = "",
        team: str = "default",
    ) -> None:
        if self._store is None:
            return
        self._store.register_resource(resource_id, kind, slots, description, team)
