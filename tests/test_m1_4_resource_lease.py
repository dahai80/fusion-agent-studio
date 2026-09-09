"""M1-4 contract tests: ResourceLease module + daemon RPCs (§5.3).

Issue #303: F1 structural fix — exclusive resources need explicit lease.
lease_apply → granted or queued (no kill). ttl expiry → evidence + team alert.

Runner: pytest tests/test_m1_4_resource_lease.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.persistence import AgentStore
from agent_runtime.resource_lease import (
    KIND_ADVISORY,
    KIND_EXCLUSIVE,
    KIND_SHARED_SLOT,
    LEASE_EXPIRED,
    LEASE_GRANTED,
    LEASE_QUEUED,
    LEASE_RELEASED,
    ResourceLease,
)
from agent_runtime.task_store import TaskStore


async def _rpc_call(socket_path, method, params=None, msg_id=1):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=10.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(data)


@pytest.fixture
def store_db(tmp_path):
    return str(tmp_path / "test_store.db")


@pytest.fixture
def lease_manager(store_db):
    s = AgentStore(db_path=store_db)
    rl = ResourceLease(store=s)
    yield rl
    s.close()


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
async def daemon(socket_path, tmp_path):
    d = DaemonServer(
        socket_path=socket_path,
        ws_port=0,
        cluster_port=0,
        http_port=0,
        store_path=str(tmp_path / "test_store.db"),
    )
    await d.start()
    d._task_store = TaskStore(db_path=str(tmp_path / "test_tasks.db"))
    yield d
    await d.stop()


class TestResourceRegistry:
    def test_default_resources_seeded(self, lease_manager):
        resources = {r["resource_id"]: r for r in lease_manager.list_resources()}
        assert "browser:douyin_profile" in resources
        assert resources["browser:douyin_profile"]["kind"] == KIND_EXCLUSIVE
        assert resources["browser:douyin_profile"]["slots"] == 1
        assert resources["gpu:mlx"]["kind"] == KIND_SHARED_SLOT
        assert resources["gpu:mlx"]["slots"] == 2
        assert resources["gpu:comfyui"]["slots"] == 1
        assert resources["disk:out"]["kind"] == KIND_ADVISORY

    def test_register_custom_resource(self, lease_manager):
        lease_manager.register_resource(
            "browser:custom", KIND_EXCLUSIVE, 1, "custom browser", "default"
        )
        r = lease_manager.list_resources()
        ids = [x["resource_id"] for x in r]
        assert "browser:custom" in ids


class TestLeaseApply:
    def test_exclusive_granted_then_queued(self, lease_manager):
        r1 = lease_manager.lease_apply("browser:douyin_profile", task_id="t1")
        assert r1["status"] == LEASE_GRANTED
        assert r1["lease_id"].startswith("lease_")
        r2 = lease_manager.lease_apply("browser:douyin_profile", task_id="t2")
        assert r2["status"] == LEASE_QUEUED
        assert r2["position"] == 1

    def test_shared_slot_grants_up_to_slots(self, lease_manager):
        r1 = lease_manager.lease_apply("gpu:mlx", task_id="g1")
        r2 = lease_manager.lease_apply("gpu:mlx", task_id="g2")
        r3 = lease_manager.lease_apply("gpu:mlx", task_id="g3")
        assert r1["status"] == LEASE_GRANTED
        assert r2["status"] == LEASE_GRANTED
        assert r3["status"] == LEASE_QUEUED
        assert r3["position"] == 1

    def test_advisory_always_granted(self, lease_manager):
        r1 = lease_manager.lease_apply("disk:out", task_id="d1")
        r2 = lease_manager.lease_apply("disk:out", task_id="d2")
        assert r1["status"] == LEASE_GRANTED
        assert r2["status"] == LEASE_GRANTED

    def test_unknown_resource_error(self, lease_manager):
        r = lease_manager.lease_apply("nonexistent:foo", task_id="x")
        assert r["status"] == "error"

    def test_granted_has_expires_at(self, lease_manager):
        r = lease_manager.lease_apply("browser:douyin_profile", task_id="t1", ttl=300)
        assert r["status"] == LEASE_GRANTED
        assert r["expires_at"] > 0


class TestLeaseRelease:
    def test_release_granted_promotes_queued(self, lease_manager):
        r1 = lease_manager.lease_apply("browser:douyin_profile", task_id="t1")
        r2 = lease_manager.lease_apply("browser:douyin_profile", task_id="t2")
        assert r2["status"] == LEASE_QUEUED
        rel = lease_manager.lease_release(r1["lease_id"], reason="done")
        assert rel["status"] == LEASE_RELEASED
        promoted = lease_manager.get_lease(r2["lease_id"])
        assert promoted["status"] == LEASE_GRANTED
        assert promoted["position"] == 0
        assert promoted["expires_at"] > 0

    def test_release_queued_no_promote(self, lease_manager):
        r1 = lease_manager.lease_apply("browser:douyin_profile", task_id="t1")
        r2 = lease_manager.lease_apply("browser:douyin_profile", task_id="t2")
        rel = lease_manager.lease_release(r2["lease_id"], reason="cancel_wait")
        assert rel["status"] == LEASE_RELEASED
        # t1 still granted, no promotion (nothing queued left)
        t1 = lease_manager.get_lease(r1["lease_id"])
        assert t1["status"] == LEASE_GRANTED

    def test_release_unknown_lease(self, lease_manager):
        r = lease_manager.lease_release("lease_nonexistent")
        assert r["status"] == "error"

    def test_release_already_released(self, lease_manager):
        r1 = lease_manager.lease_apply("browser:douyin_profile", task_id="t1")
        lease_manager.lease_release(r1["lease_id"], "done")
        r2 = lease_manager.lease_release(r1["lease_id"], "again")
        assert r2["status"] in (LEASE_RELEASED, LEASE_EXPIRED)


class TestExpirySweep:
    def test_expired_granted_swept(self, lease_manager):
        r1 = lease_manager.lease_apply("gpu:mlx", task_id="g1", ttl=0.05)
        r2 = lease_manager.lease_apply("gpu:mlx", task_id="g2", ttl=0.05)
        r3 = lease_manager.lease_apply("gpu:mlx", task_id="g3")  # queued
        time.sleep(0.1)
        expired = lease_manager.expiry_sweep()
        assert len(expired) == 2
        expired_ids = {e["lease_id"] for e in expired}
        assert r1["lease_id"] in expired_ids
        assert r2["lease_id"] in expired_ids
        for e in expired:
            assert e["status"] == LEASE_EXPIRED
        # g3 should be promoted to granted
        promoted = lease_manager.get_lease(r3["lease_id"])
        assert promoted["status"] == LEASE_GRANTED

    def test_no_expired_returns_empty(self, lease_manager):
        lease_manager.lease_apply("gpu:mlx", task_id="g1", ttl=300)
        expired = lease_manager.expiry_sweep()
        assert expired == []


class TestDaemonRPCs:
    @pytest.mark.asyncio
    async def test_rpc_lease_apply_granted(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": "rpc-t1"},
        )
        result = resp["result"]
        assert result["status"] == LEASE_GRANTED
        assert result["lease_id"].startswith("lease_")

    @pytest.mark.asyncio
    async def test_rpc_lease_apply_queued(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": "rpc-t1"},
        )
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": "rpc-t2"},
        )
        assert resp["result"]["status"] == LEASE_QUEUED
        assert resp["result"]["position"] == 1

    @pytest.mark.asyncio
    async def test_rpc_lease_release(self, daemon):
        apply = await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": "rpc-t1"},
        )
        lease_id = apply["result"]["lease_id"]
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.lease_release",
            {"lease_id": lease_id, "reason": "done"},
        )
        assert resp["result"]["status"] == LEASE_RELEASED

    @pytest.mark.asyncio
    async def test_rpc_resource_list(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.list",
            {},
        )
        resources = resp["result"]["resources"]
        ids = [r["resource_id"] for r in resources]
        assert "browser:douyin_profile" in ids
        assert "gpu:mlx" in ids

    @pytest.mark.asyncio
    async def test_rpc_lease_list(self, daemon):
        await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": "rpc-t1"},
        )
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.lease_list",
            {"resource_id": "browser:douyin_profile"},
        )
        leases = resp["result"]["leases"]
        assert len(leases) >= 1
        assert leases[0]["status"] == LEASE_GRANTED

    @pytest.mark.asyncio
    async def test_rpc_lease_apply_task_writeback(self, daemon, tmp_path):
        # submit task first, then lease_apply should write resource_lease_id
        sub = await _rpc_call(
            daemon.socket_path,
            "task.submit",
            {"title": "lease task", "graph_id": "g1"},
        )
        task_id = sub["result"]["task"]["task_id"]
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.lease_apply",
            {"resource_id": "browser:douyin_profile", "task_id": task_id},
        )
        assert resp["result"]["status"] == LEASE_GRANTED
        get = await _rpc_call(daemon.socket_path, "task.get", {"task_id": task_id})
        task = get["result"]["task"]
        assert task["resource_lease_id"] == resp["result"]["lease_id"]

    @pytest.mark.asyncio
    async def test_rpc_resource_register(self, daemon):
        resp = await _rpc_call(
            daemon.socket_path,
            "resource.register",
            {
                "resource_id": "custom:res",
                "kind": "exclusive",
                "slots": 1,
                "description": "test custom",
            },
        )
        assert resp["result"]["status"] == "ok"
        listing = await _rpc_call(daemon.socket_path, "resource.list", {})
        ids = [r["resource_id"] for r in listing["result"]["resources"]]
        assert "custom:res" in ids
