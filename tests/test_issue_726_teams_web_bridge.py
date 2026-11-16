# Issue #726 (C3-enable): teams.* messaging/read surface exposed over the
# web RPC bridge for the @fusion/chat adapter — allowlist rows land with
# test rows (bridge file rule), teams.status members carry a presence field
# derived from the team lifecycle, and lifecycle/destructive methods stay
# denied at the gate.
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST
from tests.test_issue_341_subagent import _make_parent
from tools.registry import ToolRegistry


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition():
    return {
        "name": "chat-team",
        "namespace_key": "team:chat-team",
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
            {"name": "worker-b", "role": "does B", "budget_tokens": 40000},
        ],
    }


def _runtime(**kwargs):
    kwargs.setdefault("responses", [])
    kwargs.setdefault("tools", ToolRegistry())
    rt, client = _make_parent(**kwargs)
    rt.kernel = {"orchestrator": build_kernel_orchestrator(rt)}
    return rt, client


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def _active(daemon):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition()})
    return rt


async def test_status_members_carry_presence_available(daemon):
    await _active(daemon)
    res = await _run(daemon, "teams.status", {"team": "chat-team"})
    assert res["status"] == "ok"
    assert res["lifecycle"] == "ACTIVE"
    for m in res["members"]:
        assert m["presence"] == "available"


async def test_status_presence_paused_when_team_paused(daemon):
    await _active(daemon)
    await _run(daemon, "teams.pause", {"team": "chat-team"})
    res = await _run(daemon, "teams.status", {"team": "chat-team"})
    for m in res["members"]:
        assert m["presence"] == "paused"
    # pause stays off the web bridge even though the dispatcher supports it
    assert "teams.pause" not in WEB_RPC_ALLOWLIST


async def test_status_presence_offline_before_activation(daemon, tmp_path):
    # DEFINED lifecycle (never activated) maps to offline.
    from fusion_harness.team import TeamDefinition, TeamMember
    from fusion_harness.team_runtime import TeamRuntime

    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    definition = TeamDefinition(
        name="dormant",
        namespace_key="team:dormant",
        members=[TeamMember(name="w", role="r")],
    )
    team_rt = TeamRuntime(definition, state_dir=str(tmp_path / "dormant-state"))
    rt.teams_registry = {"dormant": team_rt}
    res = await _run(daemon, "teams.status", {"team": "dormant"})
    assert res["members"][0]["presence"] == "offline"


def test_adapter_surface_methods_allowlisted():
    for method in ("teams.send", "teams.broadcast", "teams.inbox", "teams.status", "teams.list"):
        assert method in WEB_RPC_ALLOWLIST, method
