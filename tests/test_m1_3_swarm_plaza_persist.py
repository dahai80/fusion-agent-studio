"""M1-3 contract tests: swarm/plaza SQLite persistence + readback verify + dedup + reconcile.

Covers design doc ~/fusion/architecture/m1-agent-studio-team-state-impl.md §2 M1-3:
- plaza_messages persist + readback verify + sha256 dedup
- plaza_channels persist
- swarm_agents persist
- swarm_delegations persist + status update
- team_launch_phases (active/finished/reconciled)
- backwards compat (store=None = in-memory, no crash)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_runtime.persistence import AgentStore
from agent_runtime.plaza import Plaza, PlazaMessage, PlazaChannel, _message_hash
from agent_runtime.swarm_router import (
    SwarmRouter,
    SwarmAgent,
    TaskDelegation,
    HandoffContext,
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_team.db"
        s = AgentStore(str(db_path))
        yield s
        s.close()


# ── Plaza persistence ──


class TestPlazaPersistence:
    def test_broadcast_persists_message(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        msg = plaza.broadcast("ch1", "alice", "hello @bob")
        assert store.verify_plaza_message(msg.id) is True

    def test_readback_verify_after_write(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        msg = plaza.broadcast("ch1", "alice", "hello world")
        # 回读验证: 消息落盘可查
        msgs = store.load_all_plaza_messages()
        assert len(msgs) == 1
        assert msgs[0]["message_id"] == msg.id
        assert msgs[0]["channel"] == "ch1"
        assert msgs[0]["sender"] == "alice"

    def test_dedup_same_hash_skipped(self, store):
        # 同 channel+sender+content+round 的消息 hash 相同, 第二次写被跳过
        h = _message_hash("ch1", "alice", "hello", 1)
        assert store.save_plaza_message("id1", "ch1", "alice", 1.0, {}, h) is True
        assert store.save_plaza_message("id2", "ch1", "alice", 1.0, {}, h) is False
        assert len(store.load_all_plaza_messages()) == 1

    def test_channel_persisted_across_reload(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        # 新 Plaza 实例从 store 加载
        plaza2 = Plaza(store=store)
        channels = plaza2.list_channels()
        assert len(channels) == 1
        assert channels[0].name == "ch1"
        assert "alice" in channels[0].participants

    def test_messages_reloaded_from_store(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        plaza.broadcast("ch1", "alice", "msg1")
        plaza.broadcast("ch1", "bob", "msg2")
        # 重载
        plaza2 = Plaza(store=store)
        msgs = plaza2.get_messages("ch1")
        assert len(msgs) == 2
        assert msgs[0].content == "msg1"
        assert msgs[1].content == "msg2"

    def test_delete_channel_removes_from_store(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        plaza.broadcast("ch1", "alice", "hello")
        assert plaza.delete_channel("ch1") is True
        # 重载后频道应消失
        plaza2 = Plaza(store=store)
        assert len(plaza2.list_channels()) == 0

    def test_store_none_backwards_compat(self):
        # store=None: 纯内存, 不崩溃, 行为同原 Plaza
        plaza = Plaza()
        plaza.create_channel("ch1", ["alice", "bob"])
        msg = plaza.broadcast("ch1", "alice", "hello")
        assert msg.id != ""
        assert len(plaza.get_messages("ch1")) == 1

    def test_designate_speaker_persists(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        msg = plaza.designate_speaker("ch1", "bob")
        assert store.verify_plaza_message(msg.id) is True

    def test_human_break_in_persists(self, store):
        plaza = Plaza(store=store)
        plaza.create_channel("ch1", ["alice", "bob"])
        msg = plaza.human_break_in("ch1", "stop!")
        assert store.verify_plaza_message(msg.id) is True


# ── Swarm persistence ──


class TestSwarmPersistence:
    def test_register_agent_persists(self, store):
        sw = SwarmRouter(store=store)
        a = SwarmAgent(id="a1", name="alice", capabilities=["cap1"])
        sw.register_agent(a)
        agents = store.load_swarm_agents()
        assert len(agents) == 1
        assert agents[0]["agent_id"] == "a1"
        assert agents[0]["name"] == "alice"

    def test_agents_reloaded_from_store(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice", capabilities=["cap1"]))
        sw.register_agent(SwarmAgent(id="a2", name="bob", capabilities=["cap2"]))
        # 重载
        sw2 = SwarmRouter(store=store)
        assert len(sw2.list_agents()) == 2

    def test_unregister_agent_removed_from_store(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice"))
        assert sw.unregister_agent("a1") is True
        sw2 = SwarmRouter(store=store)
        assert len(sw2.list_agents()) == 0

    def test_delegate_persists_delegation(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice", capabilities=["cap1"], handoff_targets=["a2"]))
        sw.register_agent(SwarmAgent(id="a2", name="bob", capabilities=["cap2"]))
        delegation = sw.delegate("a1", "do thing", capability="cap2")
        assert delegation is not None
        dels = store.load_swarm_delegations()
        assert len(dels) == 1
        assert dels[0]["delegator"] == "a1"
        assert dels[0]["delegatee"] == "a2"

    def test_evaluate_updates_status_persisted(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice", capabilities=["cap1"], handoff_targets=["a2"]))
        sw.register_agent(SwarmAgent(id="a2", name="bob", capabilities=["cap2"]))
        delegation = sw.delegate("a1", "do thing", capability="cap2")
        sw.evaluate(delegation.id, {"ok": True})
        # 重载后状态应为 completed
        sw2 = SwarmRouter(store=store)
        d = sw2.get_delegation(delegation.id)
        assert d.status == "completed"
        assert d.result == {"ok": True}

    def test_delegations_reloaded_from_store(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice", capabilities=["cap1"], handoff_targets=["a2"]))
        sw.register_agent(SwarmAgent(id="a2", name="bob", capabilities=["cap2"]))
        sw.delegate("a1", "task1", capability="cap2")
        sw.delegate("a1", "task2", capability="cap2")
        sw2 = SwarmRouter(store=store)
        assert len(sw2.list_delegations()) == 2

    def test_store_none_backwards_compat(self):
        # store=None: 纯内存, 不崩溃
        sw = SwarmRouter()
        sw.register_agent(SwarmAgent(id="a1", name="alice"))
        assert sw.get_agent("a1") is not None
        assert len(sw.list_agents()) == 1

    def test_handoff_still_works_with_store(self, store):
        sw = SwarmRouter(store=store)
        sw.register_agent(SwarmAgent(id="a1", name="alice", handoff_targets=["a2"]))
        sw.register_agent(SwarmAgent(id="a2", name="bob"))
        ctx = HandoffContext(task_id="t1", hop_count=0)
        new_ctx = sw.handoff("a1", "a2", ctx)
        assert new_ctx is not None
        assert new_ctx.hop_count == 1


# ── Team launch phases ──


class TestTeamLaunchPhases:
    def test_set_and_get_phase(self, store):
        store.set_team_launch_phase("teamA", "active")
        phases = store.get_team_launch_phases("active")
        assert len(phases) == 1
        assert phases[0]["team"] == "teamA"
        assert phases[0]["phase"] == "active"

    def test_phase_update(self, store):
        store.set_team_launch_phase("teamA", "active")
        store.set_team_launch_phase("teamA", "finished")
        assert len(store.get_team_launch_phases("active")) == 0
        assert len(store.get_team_launch_phases("finished")) == 1

    def test_reconcile_active_to_reconciled(self, store):
        # 模拟崩溃: 3 团队残留 active
        store.set_team_launch_phase("teamA", "active")
        store.set_team_launch_phase("teamB", "active")
        store.set_team_launch_phase("teamC", "finished")
        # reconcile: active → reconciled
        active = store.get_team_launch_phases("active")
        for row in active:
            store.set_team_launch_phase(row["team"], "reconciled")
        assert len(store.get_team_launch_phases("active")) == 0
        assert len(store.get_team_launch_phases("reconciled")) == 2
        assert len(store.get_team_launch_phases("finished")) == 1

    def test_get_all_phases(self, store):
        store.set_team_launch_phase("teamA", "active")
        store.set_team_launch_phase("teamB", "finished")
        all_phases = store.get_team_launch_phases()
        assert len(all_phases) == 2


# ── Migration safety ──


class TestMigrationSafety:
    def test_migration_idempotent(self, store):
        # 迁移已跑 (fixture 创建时). 再创建新 store 同库不应崩.
        db_path = store.db_path
        store.close()
        s2 = AgentStore(str(db_path))
        # 表仍在
        s2.set_team_launch_phase("teamX", "active")
        assert len(s2.get_team_launch_phases("active")) == 1
        s2.close()

    def test_old_db_without_v2_upgrades(self, tmp_path):
        # 模拟老库 (v1): 先建 v1 库, 再开新 AgentStore 应自动迁移 v2
        db_path = tmp_path / "old.db"
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE graphs (id TEXT PRIMARY KEY, name TEXT, data TEXT, "
            "version TEXT, created_at REAL, updated_at REAL, description TEXT DEFAULT '')"
        )
        conn.commit()
        conn.close()
        # 新 AgentStore 应迁移 v2
        s = AgentStore(str(db_path))
        s.set_team_launch_phase("teamOld", "active")
        assert len(s.get_team_launch_phases("active")) == 1
        s.close()
