# Issue #771: all LLM-output JSON parse sites go through
# agent_runtime.parse_repair.parse_model_json — strict json.loads
# short-circuit, then the fusion_harness repair ladder; malformed model
# output no longer kills a turn (truncated / single quotes / trailing
# commas / markdown-fenced are repaired or raise the typed error), every
# invocation is counted per context, and the repair rung is logged.
from __future__ import annotations

import json
import logging

import pytest

from agent_runtime.parse_repair import parse_model_json, parse_repair_stats


@pytest.fixture
def clean_stats():
    from agent_runtime import parse_repair as mod

    saved = dict(mod._stats)
    saved_ctx = dict(mod._by_context)
    mod._stats.update(invocations=0, strict=0, repaired=0, failed=0)
    mod._by_context.clear()
    yield
    mod._stats.update(saved)
    mod._by_context.clear()
    mod._by_context.update(saved_ctx)


MALFORMED_CORPUS = [
    # truncated mid-value
    ('{"passed": true, "score": 0.9', {"passed": True, "score": 0.9}),
    # truncated mid-array
    ('{"issues": ["a", "b"', {"issues": ["a", "b"]}),
    # single quotes
    ("{'winner': 1, 'reason': 'ok'}", {"winner": 1, "reason": "ok"}),
    # trailing comma
    ('{"a": 1, "b": [1, 2,]}', {"a": 1, "b": [1, 2]}),
    # markdown-fenced
    ('```json\n{"winner": 2}\n```', {"winner": 2}),
    # fenced + trailing comma + single quotes combined
    ("```\n{'x': [1, 2,],}\n```", {"x": [1, 2]}),
]


@pytest.mark.parametrize("raw,expected", MALFORMED_CORPUS)
def test_malformed_corpus_repaired(clean_stats, raw, expected):
    assert parse_model_json(raw, context="test.corpus") == expected


def test_strict_json_short_circuits(clean_stats):
    assert parse_model_json('{"ok": true}', context="test.strict") == {"ok": True}


def test_unparseable_raises_typed_error(clean_stats):
    from fusion_harness.json_repair import JSONRepairError

    with pytest.raises(JSONRepairError):
        parse_model_json("no json here at all {{", context="test.garbage")


def test_prose_wrapped_object_repaired(clean_stats):
    raw = 'Here is the result:\n{"goal": "ship", "plan": ["a"]}\nDone.'
    assert parse_model_json(raw, context="test.prose")["goal"] == "ship"


def test_stats_count_invocations_and_outcomes(clean_stats):
    parse_model_json('{"a": 1}', context="s.strict")
    parse_model_json("{'b': 1,}", context="s.repaired")
    with pytest.raises(ValueError):
        parse_model_json(")))", context="s.failed")
    stats = parse_repair_stats()
    assert stats["totals"]["invocations"] == 3
    assert stats["totals"]["strict"] == 1
    assert stats["totals"]["repaired"] == 1
    assert stats["totals"]["failed"] == 1
    assert stats["by_context"]["s.repaired"]["repaired"] == 1
    assert stats["by_context"]["s.failed"]["failed"] == 1


def test_repair_rung_logged(caplog, clean_stats):
    with caplog.at_level(logging.INFO, logger="agent_runtime.parse_repair"):
        parse_model_json("```json\n{'a': 1,}\n```", context="t.rung")
    repair_lines = [r.message for r in caplog.records if "repaired" in r.message]
    assert repair_lines, "repair event must be logged"
    assert "t.rung" in repair_lines[0]
    assert "rung=" in repair_lines[0]


def test_planner_parse_llm_steps_repairs_fenced_output():
    from agent_runtime.planner import PlannerEngine

    raw = "```json\n[{'action': 'create', 'description': 'd',\n 'target_files': ['x'], 'estimated_complexity': 'low', 'dependencies': []},]\n```"
    steps = PlannerEngine()._parse_llm_steps(raw)
    assert len(steps) == 1
    assert steps[0].action == "create"


def test_team_lead_extract_json_tolerates_prose_and_quotes():
    from agent_runtime.team_lead import TeamLead

    raw = "Ledger:\n{'goal': 'g', 'facts': {}, 'plan': [{'step': 's'}]}\nok"
    data = TeamLead._extract_json(raw)
    assert data["goal"] == "g"


def test_team_lead_extract_json_typed_error_on_garbage():
    from agent_runtime.team_lead import TeamLead

    with pytest.raises(ValueError):
        TeamLead._extract_json("nothing parseable ]]")


def test_verifier_result_repair_keeps_fallback():
    from agent_runtime.verifier import VerificationResult

    # malformed (single-quoted, fenced) reply must still yield a verdict
    raw = "```json\n{'passed': true, 'score': 0.8, 'issues': [], 'suggestion': ''}\n```"
    data = parse_model_json(raw, context="verifier.result")
    result = VerificationResult(
        passed=bool(data.get("passed", False)),
        score=float(data.get("score", 0.0)),
        issues=data.get("issues", []),
        suggestion=data.get("suggestion", ""),
        attempt=1,
        max_attempts=3,
        metadata={},
    )
    assert result.passed is True
    assert isinstance(json.dumps(data), str)
