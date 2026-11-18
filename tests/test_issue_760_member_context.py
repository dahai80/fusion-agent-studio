# Issue #760: teams.register must carry TeamMember.context through the RPC
# boundary. Previously _handle_register dropped the field silently, so every
# member registered with the dataclass default context="" and the harness
# had nothing to inject into member turns.
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from tests.test_issue_341_subagent import _make_parent
from tests.test_issue_707_teams_dispatcher import _definition, _run, _stub_registry


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


def _runtime(**kwargs):
    kwargs.setdefault("responses", [])
    kwargs.setdefault("tools", _stub_registry())
    rt, client = _make_parent(**kwargs)
    rt.kernel = {"orchestrator": build_kernel_orchestrator(rt)}
    return rt, client


async def test_register_carries_member_context(daemon):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    definition = _definition()
    definition["members"][0]["context"] = "You own data ingestion. Prefer csv over json."
    res = await _run(daemon, "teams.register", {"definition": definition})
    assert res["status"] == "ok"
    team_rt = daemon._get_runtime().teams_registry["pair"]
    member = next(m for m in team_rt.definition.members if m.name == "worker-a")
    assert member.context == "You own data ingestion. Prefer csv over json."


async def test_register_context_defaults_empty(daemon):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    res = await _run(daemon, "teams.register", {"definition": _definition()})
    assert res["status"] == "ok"
    team_rt = daemon._get_runtime().teams_registry["pair"]
    member = next(m for m in team_rt.definition.members if m.name == "worker-b")
    assert member.context == ""


async def test_register_context_none_and_non_string_coerced(daemon):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    definition = _definition()
    definition["members"][0]["context"] = None
    definition["members"][1]["context"] = 12345
    res = await _run(daemon, "teams.register", {"definition": definition})
    assert res["status"] == "ok"
    members = daemon._get_runtime().teams_registry["pair"].definition.members
    assert members[0].context == ""
    assert members[1].context == "12345"
