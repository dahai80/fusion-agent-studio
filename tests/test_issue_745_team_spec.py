# Issue #745: teams.spec_read / teams.spec_write over the team state_dir
# SPEC.md + register-time placeholder seeding (initial text via register
# payload) + lead prompt consult injection (_spec_block).
from __future__ import annotations

import os

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from agent_runtime.team_lead import _spec_block
from tests.test_issue_341_subagent import _make_parent
from tools.registry import ToolRegistry


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


def _definition(**kw):
    d = {
        "name": "spec-team",
        "namespace_key": "team:spec-team",
        "members": [
            {"name": "worker-a", "role": "does A", "budget_tokens": 40000},
        ],
    }
    d.update(kw)
    return d


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


async def _register(daemon, params=None):
    payload = dict(params or {})
    payload.setdefault("definition", _definition())
    return await _run(daemon, "teams.register", payload), payload


async def test_register_seeds_skeleton(daemon, tmp_path):
    res, _ = await _register(daemon)
    assert res["status"] == "ok"
    spec_path = res["state_dir"] + "/SPEC.md"
    assert os.path.isfile(spec_path)
    text = open(spec_path, encoding="utf-8").read()
    for section in ("## Roles", "## Conventions", "## Interfaces", "## Current split"):
        assert section in text


async def test_register_initial_spec_text(daemon):
    res, _ = await _register(daemon, {"spec": "Custom team spec body"})
    assert res["status"] == "ok"
    text = open(res["state_dir"] + "/SPEC.md", encoding="utf-8").read()
    assert text.startswith("Custom team spec body")


async def test_re_register_does_not_clobber(daemon):
    res, _ = await _register(daemon, {"spec": "Original body"})
    assert res["status"] == "ok"
    again = await _run(daemon, "teams.register", {"definition": _definition()})
    assert again["status"] == "error"
    assert "already registered" in again["message"]
    text = open(res["state_dir"] + "/SPEC.md", encoding="utf-8").read()
    assert text.startswith("Original body")


async def test_spec_write_read_roundtrip(daemon):
    res, _ = await _register(daemon)
    assert res["status"] == "ok"
    body = "# SPEC — spec-team\n\n## Roles\n\nworker-a: all the work\n"
    w = await _run(daemon, "teams.spec_write", {"team": "spec-team", "text": body})
    assert w["status"] == "ok"
    assert w["bytes"] == len(body)
    r = await _run(daemon, "teams.spec_read", {"team": "spec-team"})
    assert r["status"] == "ok"
    assert r["spec"] == body
    assert r["path"] == res["state_dir"] + "/SPEC.md"


async def test_spec_write_requires_text(daemon):
    await _register(daemon)
    res = await _run(daemon, "teams.spec_write", {"team": "spec-team", "text": "   "})
    assert res["status"] == "error"


async def test_spec_write_paused_team_clean_error(daemon):
    await _register(daemon)
    await _run(daemon, "teams.pause", {"team": "spec-team"})
    res = await _run(daemon, "teams.spec_write", {"team": "spec-team", "text": "x"})
    assert res["status"] == "error"
    assert "PAUSED" in res["message"]
    assert "ACTIVE" in res["message"]


async def test_spec_write_disbanded_team_clean_error(daemon):
    await _register(daemon)
    await _run(daemon, "teams.disband", {"team": "spec-team"})
    res = await _run(daemon, "teams.spec_write", {"team": "spec-team", "text": "x"})
    assert res["status"] == "error"


async def test_spec_read_missing_file_typed_error(daemon):
    res, _ = await _register(daemon)
    os.remove(res["state_dir"] + "/SPEC.md")
    r = await _run(daemon, "teams.spec_read", {"team": "spec-team"})
    assert r["status"] == "error"
    assert "no SPEC.md" in r["message"]


async def test_spec_read_unknown_team(daemon):
    res = await _run(daemon, "teams.spec_read", {"team": "ghost"})
    assert res["status"] == "error"
    assert "not registered" in res["message"]


async def test_spec_block_reads_file_and_degrades(tmp_path):
    class FakeTeam:
        definition = type("D", (), {"name": "spec-team"})()

        def __init__(self, state_dir):
            self.state_dir = str(state_dir)

    team = FakeTeam(tmp_path / "t")
    os.makedirs(team.state_dir)
    assert _spec_block(team) == "(no SPEC.md yet)"
    with open(team.state_dir + "/SPEC.md", "w", encoding="utf-8") as fh:
        fh.write("role boundary line\n")
    assert _spec_block(team) == "role boundary line"


async def test_spec_block_truncates(tmp_path):
    from agent_runtime.team_lead import _SPEC_CHARS

    class FakeTeam:
        definition = type("D", (), {"name": "spec-team"})()

        def __init__(self, state_dir):
            self.state_dir = str(state_dir)

    team = FakeTeam(tmp_path / "t")
    os.makedirs(team.state_dir)
    with open(team.state_dir + "/SPEC.md", "w", encoding="utf-8") as fh:
        fh.write("x" * (_SPEC_CHARS + 500))
    block = _spec_block(team)
    assert len(block) < _SPEC_CHARS + 100
    assert block.endswith("(truncated)")


async def test_task_ledger_prompt_has_spec_slot():
    from agent_runtime.team_lead import TASK_LEDGER_PROMPT

    out = TASK_LEDGER_PROMPT.format(
        goal="g", spec="THE-SPEC-BLOCK", board="b", previous=""
    )
    assert "THE-SPEC-BLOCK" in out
    assert "consult it before dividing new tasks" in out
