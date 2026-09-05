"""#289/#290/#291: agent versioning, agent-scoped audit/session logs, style.delete RPC.

Fusion Studio audit audit-product-0905 found these RPC methods return -32601:
- agent_studio.agent.snapshot / .versions / .restore_version  (#289)
- agent_studio.audit.trail / agent_studio.session.logs         (#290)
- style.delete                                                 (#291)

Uses a real DaemonServer over a temp HOME so the full
AgentPackage / AgentVersionStore / AuditLogger / AgentStatusTracker /
StyleManager stack is exercised end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    d = DaemonServer(socket_path=str(tmp_path / "test.sock"))
    d._agents = {}
    yield d


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    return await handler(params or {})


async def _create_agent(daemon, name="VersionBot"):
    return await _run(daemon, "agent.create", {"name": name})


class TestIssue289AgentSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_returns_version_id(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        result = await _run(
            daemon,
            "agent_studio.agent.snapshot",
            {"agent_id": agent_id, "label": "v1"},
        )
        assert result["version_id"]
        assert result["snapshot"]["label"] == "v1"
        assert result["snapshot"]["agent_id"] == agent_id

    @pytest.mark.asyncio
    async def test_snapshot_missing_agent_id(self, daemon):
        result = await _run(daemon, "agent_studio.agent.snapshot", {})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_snapshot_unknown_agent(self, daemon):
        result = await _run(
            daemon, "agent_studio.agent.snapshot", {"agent_id": "nope"}
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_versions_lists_snapshots(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        await _run(daemon, "agent_studio.agent.snapshot", {"agent_id": agent_id})
        await _run(daemon, "agent_studio.agent.snapshot", {"agent_id": agent_id})
        result = await _run(
            daemon, "agent_studio.agent.versions", {"agent_id": agent_id}
        )
        assert result["total"] == 2
        assert len(result["versions"]) == 2

    @pytest.mark.asyncio
    async def test_plain_alias_works(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        r1 = await _run(daemon, "agent.snapshot", {"agent_id": agent_id})
        r2 = await _run(daemon, "agent.versions", {"agent_id": agent_id})
        assert r1["version_id"]
        assert r2["total"] == 1

    @pytest.mark.asyncio
    async def test_restore_version_restores_manifest(self, daemon):
        created = await _create_agent(daemon, name="OrigName")
        agent_id = created["agent_id"]
        snap = await _run(
            daemon, "agent_studio.agent.snapshot", {"agent_id": agent_id}
        )
        version_id = snap["version_id"]

        await _run(
            daemon,
            "agent.update",
            {"agent_id": agent_id, "name": "ChangedName"},
        )

        restored = await _run(
            daemon,
            "agent_studio.agent.restore_version",
            {"agent_id": agent_id, "version_id": version_id},
        )
        assert restored["restored"] is True
        assert restored["agent"]["name"] == "OrigName"

    @pytest.mark.asyncio
    async def test_restore_unknown_version(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        result = await _run(
            daemon,
            "agent_studio.agent.restore_version",
            {"agent_id": agent_id, "version_id": "missing"},
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_restore_missing_params(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        result = await _run(
            daemon, "agent_studio.agent.restore_version", {"agent_id": agent_id}
        )
        assert result["status"] == "error"


class TestIssue290AuditAndSessionLogs:
    @pytest.mark.asyncio
    async def test_audit_trail_scoped_to_agent(self, daemon):
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        audit = daemon._get_audit_logger()
        audit.log_action(
            actor_id="user",
            action="agent.update",
            resource_type="agent",
            resource_id=agent_id,
        )
        audit.log_action(
            actor_id="user",
            action="agent.update",
            resource_type="agent",
            resource_id="other-agent",
        )
        result = await _run(
            daemon, "agent_studio.audit.trail", {"agent_id": agent_id}
        )
        assert result["total"] == 1
        assert result["entries"][0]["resource_id"] == agent_id

    @pytest.mark.asyncio
    async def test_audit_trail_unscoped_returns_all(self, daemon):
        audit = daemon._get_audit_logger()
        audit.log_action(
            actor_id="u", action="x", resource_type="agent", resource_id="a1"
        )
        audit.log_action(
            actor_id="u", action="x", resource_type="agent", resource_id="a2"
        )
        result = await _run(daemon, "agent_studio.audit.trail", {})
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_session_logs_scoped_to_agent(self, daemon):
        from agent_runtime.agent_api import RunHistoryEntry

        tracker = daemon._get_status_tracker()
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        tracker.record_run(
            RunHistoryEntry(
                run_id="r1", agent_id=agent_id, status="completed", started_at=1000.0
            )
        )
        tracker.record_run(
            RunHistoryEntry(
                run_id="r2", agent_id="other", status="completed", started_at=1000.0
            )
        )
        result = await _run(
            daemon, "agent_studio.session.logs", {"agent_id": agent_id}
        )
        assert result["total"] == 1
        assert result["sessions"][0]["run_id"] == "r1"

    @pytest.mark.asyncio
    async def test_session_logs_date_filter(self, daemon):
        import datetime

        from agent_runtime.agent_api import RunHistoryEntry

        tracker = daemon._get_status_tracker()
        created = await _create_agent(daemon)
        agent_id = created["agent_id"]
        old_ts = datetime.datetime(2024, 1, 1).timestamp()
        new_ts = datetime.datetime(2024, 6, 1).timestamp()
        tracker.record_run(
            RunHistoryEntry(
                run_id="old", agent_id=agent_id, status="completed", started_at=old_ts
            )
        )
        tracker.record_run(
            RunHistoryEntry(
                run_id="new",
                agent_id=agent_id,
                status="completed",
                started_at=new_ts,
            )
        )
        result = await _run(
            daemon,
            "agent_studio.session.logs",
            {"agent_id": agent_id, "start_date": "2024-03-01"},
        )
        assert result["total"] == 1
        assert result["sessions"][0]["run_id"] == "new"

    @pytest.mark.asyncio
    async def test_session_logs_no_agent_empty(self, daemon):
        result = await _run(daemon, "agent_studio.session.logs", {})
        assert result["total"] == 0


class TestIssue291StyleDelete:
    def test_style_delete_removes_custom_style(self, daemon):
        mgr = daemon._get_style_manager()
        created = mgr.create("TempStyle", "suffix", "markdown")
        style_id = created["style_id"]
        assert mgr.get(style_id) is not None
        deleted = mgr.delete(style_id)
        assert deleted is True
        assert mgr.get(style_id) is None

    def test_style_delete_refuses_builtin(self, daemon):
        mgr = daemon._get_style_manager()
        builtin_id = next(
            sid for sid, s in mgr._styles.items() if s.is_builtin
        )
        deleted = mgr.delete(builtin_id)
        assert deleted is False
        assert mgr.get(builtin_id) is not None

    def test_style_delete_unknown_returns_false(self, daemon):
        mgr = daemon._get_style_manager()
        assert mgr.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_style_delete_rpc(self, daemon):
        mgr = daemon._get_style_manager()
        created = mgr.create("RpcStyle", "suffix", "markdown")
        style_id = created["style_id"]
        result = await _run(daemon, "style.delete", {"style_id": style_id})
        assert result["deleted"] is True
        assert mgr.get(style_id) is None

    @pytest.mark.asyncio
    async def test_style_delete_rpc_missing_id(self, daemon):
        result = await _run(daemon, "style.delete", {})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_style_delete_rpc_unknown(self, daemon):
        result = await _run(
            daemon, "style.delete", {"style_id": "no-such-style"}
        )
        assert result["status"] == "error"


class TestHandlerRegistration:
    def test_all_methods_registered(self, daemon):
        for method in (
            "agent_studio.agent.snapshot",
            "agent_studio.agent.versions",
            "agent_studio.agent.restore_version",
            "agent_studio.audit.trail",
            "agent_studio.session.logs",
            "agent.snapshot",
            "agent.versions",
            "agent.restore_version",
            "style.delete",
        ):
            assert daemon._get_handler(method) is not None, method
