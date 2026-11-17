# Issue #733: teams.playbook_render / teams.playbook_trace — read-only
# surface over the harness Playbook op log. Team gating via
# TeamDefinition.playbook; typed errors (not 500) for disabled teams and
# missing stores; trace returns the originating episode chain.
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from tests.test_issue_341_subagent import _make_parent
from tools.registry import ToolRegistry


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition(playbook=False):
    return {
        "name": "pb-team",
        "namespace_key": "team:pb-team",
        "playbook": playbook,
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
        ],
    }


def _runtime(**kwargs):
    kwargs.setdefault("responses", [])
    kwargs.setdefault("tools", ToolRegistry())
    rt, client = _make_parent(**kwargs)
    rt.kernel = {"orchestrator": build_kernel_orchestrator(rt)}
    return rt, client


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


async def _registered(daemon, playbook):
    rt, _client = _runtime()
    daemon._get_runtime = lambda: rt
    await _run(daemon, "teams.register", {"definition": _definition(playbook)})
    return rt


async def test_render_playbook_disabled_typed_error(daemon):
    await _registered(daemon, playbook=False)
    res = await _run(daemon, "teams.playbook_render", {"team": "pb-team"})
    assert res["status"] == "error"
    assert "playbook disabled" in res["message"]


async def test_render_missing_store_typed_error(daemon):
    await _registered(daemon, playbook=True)
    res = await _run(daemon, "teams.playbook_render", {"team": "pb-team"})
    assert res["status"] == "error"
    assert "no playbook store" in res["message"]


async def test_render_and_trace_roundtrip(daemon):
    await _registered(daemon, playbook=True)
    from agent_runtime.playbook_store import PlaybookStore

    store = PlaybookStore()
    add = store.append_lesson(
        "coordination",
        "worker-a stalls without an explicit handoff message",
        episodes=["ep-run-1", "ep-run-2"],
    )
    assert add["status"] == "ok"

    res = await _run(daemon, "teams.playbook_render", {"team": "pb-team"})
    assert res["status"] == "ok"
    assert res["team"] == "pb-team"
    assert res["rendered"].startswith("# Playbook: agent-studio")
    assert len(res["entries"]) == 1
    entry = res["entries"][0]
    assert entry["section"] == "coordination"
    assert entry["episodes"] == ["ep-run-1", "ep-run-2"]

    trace = await _run(
        daemon, "teams.playbook_trace", {"team": "pb-team", "entry_id": entry["entry_id"]}
    )
    assert trace["status"] == "ok"
    assert trace["episodes"] == ["ep-run-1", "ep-run-2"]


async def test_trace_unknown_entry_typed_error(daemon):
    await _registered(daemon, playbook=True)
    from agent_runtime.playbook_store import PlaybookStore

    PlaybookStore().append_lesson("s", "c", episodes=["ep-1"])
    res = await _run(
        daemon, "teams.playbook_trace", {"team": "pb-team", "entry_id": "agent-studio-e999"}
    )
    assert res["status"] == "error"
    assert "not found" in res["message"]


async def test_trace_missing_entry_id_param(daemon):
    await _registered(daemon, playbook=True)
    res = await _run(daemon, "teams.playbook_trace", {"team": "pb-team"})
    assert res["status"] == "error"


async def test_unknown_team_rejected(daemon):
    await _registered(daemon, playbook=True)
    res = await _run(daemon, "teams.playbook_render", {"team": "ghost"})
    assert res["status"] == "error"
    assert "not registered" in res["message"]


async def test_handlers_satisfy_dispatch_contract():
    from agent_runtime.dispatchers.teams import TeamsDispatcher

    sd = TeamsDispatcher.__new__(TeamsDispatcher)
    handlers = TeamsDispatcher.get_handlers(sd)
    import inspect

    for name in ("teams.playbook_render", "teams.playbook_trace"):
        assert inspect.iscoroutinefunction(handlers[name]), name
