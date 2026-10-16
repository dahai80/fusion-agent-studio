import asyncio

import pytest

from agent_runtime.agent_package import resolve_soul_prompt
from agent_runtime.dispatchers.agent import AgentDispatcher
from agent_runtime.persona import (
    load_layer,
    merge_persona_layers,
    parse_sections,
    resolve_persona,
    save_layer,
)


@pytest.fixture()
def persona_root(tmp_path, monkeypatch):
    import agent_runtime.persona as pm

    monkeypatch.setattr(pm, "PERSONA_ROOT", tmp_path / "persona")
    return tmp_path / "persona"


@pytest.fixture()
def agents_root(tmp_path, monkeypatch):
    import agent_runtime.agent_package as ap

    monkeypatch.setattr(ap, "AGENTS_ROOT", tmp_path / "agents")
    return tmp_path / "agents"


def _make_agent(agents_root, agent_id, soul):
    from agent_runtime.agent_package import AgentPackage

    pkg = AgentPackage(agents_root / agent_id)
    pkg.save_soul(soul)
    return pkg


def test_parse_sections():
    out = parse_sections("preamble text\n## Role\nbody A\n## Style\nbody B")
    assert out[""] == "preamble text"
    assert out["Role"] == "body A"
    assert out["Style"] == "body B"
    assert parse_sections("") == {}
    assert parse_sections("just one blob") == {"": "just one blob"}


def test_merge_layers_conflicts():
    low = "# base\n\n## Role\nworker\n## Tone\ncalm"
    high = "## Role\nlead\n## Extra\nx"
    res = merge_persona_layers([("low", low), ("high", high)])
    assert res["layers_applied"] == ["low", "high"]
    assert res["conflicts"] == [
        {"section": "Role", "winner": "high", "loser": "lower layer"}
    ]
    assert "## Role\n\nlead" in res["persona"]
    assert "## Tone\n\ncalm" in res["persona"]
    assert "## Extra\n\nx" in res["persona"]


def test_layer_save_load_roundtrip(persona_root):
    save_layer("global", "# global rules")
    assert load_layer("global") == "# global rules"
    save_layer("team", "# team rules", name="ops")
    assert load_layer("team", "ops") == "# team rules"
    with pytest.raises(ValueError):
        save_layer("team", "x")
    with pytest.raises(ValueError):
        load_layer("bogus")


def test_resolve_persona_stack(persona_root, agents_root):
    save_layer("global", "## Role\ngeneric worker")
    save_layer("team", "## Role\nops lead\n## Scope\ninfra", name="ops")
    _make_agent(agents_root, "a1", "## Role\ncode specialist\n## Voice\nterse")
    res = resolve_persona(agent_id="a1", team_id="ops", agents_root=agents_root)
    assert "## Scope\n\ninfra" in res["persona"]
    assert "## Role\n\ncode specialist" in res["persona"]
    assert res["conflicts"] == [
        {"section": "Role", "winner": "team:ops", "loser": "lower layer"},
        {"section": "Role", "winner": "agent:a1", "loser": "lower layer"},
    ]
    assert res["layers_applied"] == ["global", "team:ops", "agent:a1"]


def test_resolve_persona_run_override_wins(persona_root, agents_root):
    _make_agent(agents_root, "a2", "## Role\nbase")
    res = resolve_persona(
        agent_id="a2", run_override="## Role\nrun-specific", agents_root=agents_root
    )
    assert "## Role\n\nrun-specific" in res["persona"]
    assert res["layers_applied"][-1] == "run"


def test_resolve_persona_empty_falls_back(persona_root, agents_root):
    res = resolve_persona(agent_id="ghost", fallback="FB", agents_root=agents_root)
    assert res["persona"] == "FB"
    assert res["layers_applied"] == []


def test_resolve_soul_prompt_layers(persona_root, agents_root):
    save_layer("global", "## Ground\nglobal rule")
    _make_agent(agents_root, "a3", "## Role\nsolo")
    out = resolve_soul_prompt("a3", tenant_id="t1", team_id="ops")
    assert "## Ground\n\nglobal rule" in out
    assert "## Role\n\nsolo" in out
    plain = resolve_soul_prompt("a3")
    assert "## Role" in plain and "solo" in plain


def test_persona_rpcs(persona_root, agents_root):
    _make_agent(agents_root, "a9", "soul body")

    class _Stub:
        pass

    d = AgentDispatcher.__new__(AgentDispatcher)
    res = asyncio.run(
        d._handle_agent_resolve_persona({"agent_id": "a9", "team_id": "ops"})
    )
    assert res["layers_applied"] == ["agent:a9"]
    saved = asyncio.run(
        d._handle_agent_set_persona_layer(
            {"layer": "team", "name": "ops", "content": "## T\nteam rule"}
        )
    )
    assert saved["layer"] == "team"
    got = asyncio.run(d._handle_agent_get_persona_layer({"layer": "team", "name": "ops"}))
    assert "team rule" in got["content"]
    err = asyncio.run(d._handle_agent_set_persona_layer({"layer": "bogus", "content": "x"}))
    assert err["status"] == "error"
    err2 = asyncio.run(d._handle_agent_resolve_persona({"agent_id": "a9", "run_override": "## R\nrun"}))
    assert "## R\n\nrun" in err2["persona"]
