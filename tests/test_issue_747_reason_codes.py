# Issue #747: stable dispatch reason codes — every v1 enum value exercised
# on real failure paths (harness exceptions raised by actual team code),
# table-driven over the classifier + live RPC assertions for the daemon
# surface. Non-leaking rule: machine paths collapse to invocation_not_allowed.
from __future__ import annotations

import os

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.reason_codes import (
    HINTS,
    REASON_CODES,
    classify_error,
    error_payload,
    leaks_machine_detail,
)


def _brief_payload(**kw):
    d = {
        "objective": "do the thing",
        "output_format": "markdown report",
        "tool_guidance": "use terminal sparingly",
        "boundaries": "repo only",
        "token_budget": 20000,
        "concurrency": 1,
        "depth": 1,
    }
    d.update(kw)
    return d


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition(**kw):
    d = {
        "name": "rc-team",
        "namespace_key": "team:rc-team",
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
        ],
    }
    d.update(kw)
    return d


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


async def _register(daemon, **kw):
    res = await _run(daemon, "teams.register", {"definition": _definition(**kw)})
    assert res["status"] == "ok", res
    return res


def test_enum_closed_and_hints_total():
    assert len(REASON_CODES) == len(set(REASON_CODES))
    for code in REASON_CODES:
        assert code in HINTS, code
    for code in HINTS:
        assert code in REASON_CODES, code


def test_error_payload_shape_and_unknown_code_fallback():
    p = error_payload("team_not_registered", "boom")
    assert p["status"] == "error"
    assert p["reason_code"] == "team_not_registered"
    assert p["hint"]
    p2 = error_payload("not-a-code", "boom")
    assert p2["reason_code"] == "invocation_not_allowed"


# ---- table-driven classifier over REAL harness exceptions ----


def _harness_exc(kind: str, message: str) -> Exception:
    if kind == "TeamStateError":
        from fusion_harness.team_runtime import TeamStateError

        return TeamStateError(message)
    if kind == "TeamDefError":
        from fusion_harness.team import TeamDefError

        return TeamDefError(message)
    if kind == "SpawnBriefError":
        from fusion_harness.spawn_protocol import SpawnBriefError

        return SpawnBriefError(message)
    if kind == "LeadBudgetError":
        from agent_runtime.team_lead import LeadBudgetError

        return LeadBudgetError(message)
    if kind == "KeyError":
        return KeyError(message)
    if kind == "FileNotFoundError":
        return FileNotFoundError(message)
    if kind == "ValueError":
        return ValueError(message)
    raise AssertionError(kind)


CASES = [
    # (label, exc kind, harness message, expected code)
    ("unknown team", "KeyError", "team not registered: ghost", "team_not_registered"),
    ("unknown member", "TeamDefError", "unknown member: ghost-member", "member_unknown"),
    ("bad permission mode", "TeamDefError", "unknown permission mode: bypassPermissions", "permission_mode_unknown"),
    ("task unknown", "TeamStateError", "unknown task: t-99", "task_not_found"),
    ("paused team op", "TeamStateError", "team not ACTIVE (state=PAUSED)", "invocation_not_allowed"),
    ("illegal transition", "TeamStateError", "illegal task transition done -> todo (task=t-1)", "invocation_not_allowed"),
    ("duplicate task", "TeamStateError", "duplicate task id: t-1", "invocation_not_allowed"),
    ("depth overflow", "TeamStateError", "orchestration depth overflow: caller depth 2 + team max_depth 2 exceeds 3", "depth_overflow"),
    ("brief over member cap", "SpawnBriefError", "brief token_budget 5000 exceeds member cap 4000 (worker-a)", "budget_exhausted"),
    ("lead budget", "LeadBudgetError", "lead budget exhausted", "budget_exhausted"),
    ("no lead registered", "KeyError", "no lead for team: rc-team", "lead_not_running"),
    ("spec missing", "FileNotFoundError", "no SPEC.md exists yet for team rc-team", "spec_unavailable"),
    ("playbook gated", "ValueError", "playbook disabled on team rc-team (definition.playbook=false)", "invocation_not_allowed"),
    ("runner offline", "ValueError", "kernel runner is not wired for this runtime", "runtime_offline"),
]


@pytest.mark.parametrize("label,kind,message,expected", CASES, ids=[c[0] for c in CASES])
def test_classifier_table(label, kind, message, expected):
    assert classify_error(_harness_exc(kind, message)) == expected


# ---- live RPC failure paths carry reason_code ----


async def test_rpc_team_not_registered(daemon):
    res = await _run(daemon, "teams.status", {"team": "ghost"})
    assert res["status"] == "error"
    assert res["reason_code"] == "team_not_registered"
    assert res["hint"]


async def test_rpc_member_unknown(daemon):
    await _register(daemon)
    res = await _run(
        daemon,
        "teams.run_member",
        {"team": "rc-team", "member": "ghost", "brief": _brief_payload()},
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "member_unknown"


async def test_rpc_permission_mode_unknown(daemon):
    res = await _run(
        daemon,
        "teams.register",
        {"definition": _definition(members=[{"name": "w", "role": "r", "permission_mode": "bogus"}])},
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "permission_mode_unknown"


async def test_rpc_task_not_found(daemon):
    await _register(daemon)
    res = await _run(daemon, "teams.transition", {"team": "rc-team", "task_id": "t-99", "to": "done"})
    assert res["status"] == "error"
    assert res["reason_code"] == "task_not_found"


async def test_rpc_budget_exhausted(daemon):
    await _register(daemon)
    res = await _run(
        daemon,
        "teams.run_member",
        {
            "team": "rc-team",
            "member": "worker-a",
            "brief": _brief_payload(token_budget=99999),
        },
    )
    assert res["status"] == "error"
    assert res["reason_code"] == "budget_exhausted"


async def test_rpc_lead_not_running(daemon):
    await _register(daemon)
    res = await _run(daemon, "teams.lead_stop", {"team": "rc-team"})
    assert res["status"] == "error"
    assert res["reason_code"] == "lead_not_running"


async def test_rpc_depth_overflow(daemon):
    from fusion_harness.spawn_protocol import brief_from_event
    from fusion_harness.team_runtime import TeamStateError

    await _register(daemon, max_depth=1)
    team_rt = daemon._get_runtime().teams_registry["rc-team"]
    member = team_rt.definition.member("worker-a")
    brief = brief_from_event(_brief_payload())
    with pytest.raises(TeamStateError) as exc:
        await team_rt.run_member(member, "", brief, lambda *a, **k: None, depth=3)
    from agent_runtime.dispatchers.teams import TeamsDispatcher

    out = TeamsDispatcher._error(exc.value)
    assert out["reason_code"] == "depth_overflow"
    assert out["status"] == "error"


async def test_rpc_invocation_not_allowed_paused(daemon):
    await _register(daemon)
    await _run(daemon, "teams.pause", {"team": "rc-team"})
    res = await _run(daemon, "teams.spec_write", {"team": "rc-team", "text": "x"})
    assert res["status"] == "error"
    assert res["reason_code"] == "invocation_not_allowed"


async def test_rpc_spec_unavailable(daemon):
    res = await _register(daemon)
    os.remove(res["state_dir"] + "/SPEC.md")
    out = await _run(daemon, "teams.spec_read", {"team": "rc-team"})
    assert out["status"] == "error"
    assert out["reason_code"] == "spec_unavailable"
    assert "no SPEC.md" in out["message"]


async def test_machine_path_collapses(daemon):
    from agent_runtime.dispatchers.teams import TeamsDispatcher

    out = TeamsDispatcher._error(OSError("cannot write /private/tmp/secret-dir/x.txt: denied"))
    assert out["reason_code"] == "invocation_not_allowed"
    assert "/private/tmp" not in out["message"]
    assert "secret-dir" not in out["message"]


async def test_studio_home_paths_do_not_collapse(daemon):
    from agent_runtime.dispatchers.teams import TeamsDispatcher
    from agent_runtime.paths import studio_home

    home = str(studio_home())
    out = TeamsDispatcher._error(FileNotFoundError(f"state file missing under {home}/teams/rc-team"))
    assert out["status"] == "error"
    assert home not in out["message"]
    assert "~" in out["message"]


def test_leaks_machine_detail_detection():
    assert leaks_machine_detail("cannot read /tmp/x.sock")
    assert leaks_machine_detail("connect to /var/run/guard.sock failed")
    assert not leaks_machine_detail("team rc-team is PAUSED")
    assert not leaks_machine_detail("state missing under ~/teams/rc-team")


def test_queued_coalesced_reserved_but_valid():
    # Dispatch states, not failure codes: valid enum members with operator
    # hints, produced by async dispatch paths (v1 keeps the enum closed).
    assert "queued" in HINTS and "coalesced" in HINTS
