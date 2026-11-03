"""Issue #549 — reviewer packs: structured findings contract, blind review,
parallel fan-out with per-pack attribution, first-party bundle seeding."""
from __future__ import annotations

import json

import pytest

from agent_runtime.reviewer_packs import (
    REVIEWER_PACKS,
    FindingValidationError,
    ReviewerPackRunner,
    merge_results,
    parse_findings,
    validate_finding,
    write_bundle_dirs,
)

GOOD = {
    "severity": "P1",
    "location": "section 3 / auth.py:42",
    "rationale": "token expiry check uses < instead of <=",
    "recommendation": "use <= with skew margin",
}


def test_validate_finding_accepts_structured():
    f = validate_finding({**GOOD})
    assert f.severity == "P1"
    assert f.pack == ""


@pytest.mark.parametrize("payload", [
    {"severity": "critical", "location": "x", "rationale": "r", "recommendation": "rec"},
    {"severity": "P4", "location": "x", "rationale": "r", "recommendation": "rec"},
    {"severity": "P0", "rationale": "r", "recommendation": "rec"},
    {"severity": "P0", "location": "x", "recommendation": "rec", "rationale": ""},
    {"severity": "P0", "location": "x", "rationale": "r"},
])
def test_validate_finding_rejects_free_text_and_missing_fields(payload):
    with pytest.raises(FindingValidationError):
        validate_finding(payload)


def test_parse_findings_code_fence_and_scalar_payload():
    raw = '```json\n{"findings": [' + json.dumps(GOOD) + "]}\n```"
    out = parse_findings(raw, pack="security")
    assert len(out) == 1
    assert out[0].pack == "security"


def test_parse_findings_rejects_prose_verdict():
    with pytest.raises(FindingValidationError):
        parse_findings("The artifact looks good overall. No major issues.")


class _ScriptedLLM:
    def __init__(self, replies: dict[str, str]):
        self.replies = replies
        self.calls: list[tuple[str, list[dict]]] = []

    async def __call__(self, messages, model: str = "") -> str:
        system = messages[0]["content"]
        key = next(k for k in self.replies if k in system)
        self.calls.append((key, messages))
        return self.replies[key]


def _runner(replies):
    return ReviewerPackRunner(_ScriptedLLM(replies))


@pytest.mark.asyncio
async def test_parallel_fan_out_attribution():
    replies = {
        "Architecture-Contract": json.dumps({"findings": [{**GOOD, "severity": "P2"}]}),
        "Security Reviewer": json.dumps({"findings": [GOOD]}),
        "Domain Reviewer": json.dumps({"findings": []}),
        "Red-Team": json.dumps({"findings": [{**GOOD, "severity": "P0"}]}),
    }
    result = await _runner(replies).run(
        pack_keys=list(REVIEWER_PACKS.keys()),
        artifact="design doc",
        original_request="build a thing",
    )
    assert result["status"] == "ok"
    assert result["total"] == 3
    packs_of = [f["pack"] for f in result["findings"]]
    assert set(packs_of) == {"architecture-contract", "security", "red-team"}
    assert result["findings"][0]["severity"] == "P0"
    per = result["per_pack"]
    assert per["security"]["finding_count"] == 1
    assert per["domain"]["finding_count"] == 0


@pytest.mark.asyncio
async def test_fan_out_is_concurrent():
    import asyncio
    from time import monotonic

    async def slow_llm(messages, model: str = "") -> str:
        await asyncio.sleep(0.2)
        return json.dumps({"findings": []})

    start = monotonic()
    result = await ReviewerPackRunner(slow_llm).run(
        pack_keys=["security", "domain", "red-team", "architecture-contract"],
        artifact="a",
        original_request="r",
    )
    elapsed = monotonic() - start
    assert result["total"] == 0
    assert elapsed < 0.6, f"packs did not run concurrently: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_blind_mode_excludes_author_notes():
    replies = {
        "Security Reviewer": json.dumps({"findings": [GOOD]}),
        "Red-Team": json.dumps({"findings": [GOOD]}),
    }
    llm = _ScriptedLLM(replies)
    await ReviewerPackRunner(llm).run(
        pack_keys=["security", "red-team"],
        artifact="a",
        original_request="r",
        author_notes="SECRET-CODE-42 my design rationale",
        blind=True,
    )
    for _, messages in llm.calls:
        blob = json.dumps(messages)
        assert "SECRET-CODE-42" not in blob


@pytest.mark.asyncio
async def test_non_blind_mode_includes_author_notes():
    replies = {"Security Reviewer": json.dumps({"findings": [GOOD]})}
    llm = _ScriptedLLM(replies)
    await ReviewerPackRunner(llm).run(
        pack_keys=["security"],
        artifact="a",
        original_request="r",
        author_notes="AUTHOR-NOTES-VISIBLE",
        blind=False,
    )
    blob = json.dumps(llm.calls[0][1])
    assert "AUTHOR-NOTES-VISIBLE" in blob


@pytest.mark.asyncio
async def test_pack_failure_isolated_per_pack():
    replies = {
        "Security Reviewer": "not json at all",
        "Red-Team": json.dumps({"findings": [GOOD]}),
    }
    result = await _runner(replies).run(
        pack_keys=["security", "red-team"], artifact="a", original_request="r"
    )
    assert result["per_pack"]["security"]["status"] == "error"
    assert result["per_pack"]["red-team"]["finding_count"] == 1
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_unknown_pack_rejected():
    with pytest.raises(ValueError, match="unknown reviewer pack"):
        await _runner({}).run(pack_keys=["ghost"], artifact="a", original_request="r")


def test_merge_results_severity_order():
    merged = merge_results(
        [
            {"pack": "red-team", "status": "ok", "findings": [{**GOOD, "severity": "P2"}]},
            {"pack": "security", "status": "ok", "findings": [{**GOOD, "severity": "P0"}]},
            {"pack": "domain", "status": "error", "error": "boom", "findings": []},
        ]
    )
    assert [f["severity"] for f in merged["findings"]] == ["P0", "P2"]
    assert merged["per_pack"]["domain"]["status"] == "error"


def test_four_first_party_packs_registered():
    assert set(REVIEWER_PACKS.keys()) == {
        "architecture-contract",
        "security",
        "domain",
        "red-team",
    }
    for pack in REVIEWER_PACKS.values():
        assert pack.system_prompt.strip()
        assert "STRICT JSON" in pack.system_prompt or "Mandatory Dissent" in pack.system_prompt


def test_seed_writes_installable_bundles(tmp_path):
    dirs = write_bundle_dirs(tmp_path / "packs")
    assert len(dirs) == 4
    for d in dirs:
        manifest = json.loads((d / "manifest.json").read_text())
        assert manifest["kind"] == "reviewer_pack"
        assert manifest["agent"]["name"].startswith("reviewer-")
        assert (d / "agent.md").is_file()
        assert len(list((d / "skills").glob("*/SKILL.md"))) == 1


def test_red_team_mandatory_dissent():
    assert "at least one finding" in REVIEWER_PACKS["red-team"].system_prompt
    assert "empty list" in REVIEWER_PACKS["red-team"].system_prompt


@pytest.mark.asyncio
async def test_dispatcher_wired_into_daemon(tmp_path):
    import json as _json

    from agent_runtime.daemon_server import DaemonServer
    from agent_runtime.dispatchers.reviewer import ReviewerDispatcher

    daemon = DaemonServer(store_path=str(tmp_path / "store.db"))
    assert daemon._get_handler("reviewer.packs") is not None
    assert daemon._get_handler("reviewer.run") is not None
    assert daemon._get_handler("reviewer.seed") is not None
    assert daemon._get_handler("reviewer.schema") is not None

    class _GW:
        async def __call__(self, messages, model=""):
            return '```json\n{"findings": [' + _json.dumps(GOOD) + "]}\n```"

    from agent_runtime.reviewer_packs import ReviewerPackRunner

    daemon._reviewer_pack_runner = ReviewerPackRunner(_GW())

    dispatcher = ReviewerDispatcher(daemon)
    packs = await dispatcher._handle_packs({})
    assert packs["count"] == 4

    schema = await dispatcher._handle_schema({})
    assert schema["severities"] == ["P0", "P1", "P2", "P3"]

    run = await dispatcher._handle_run(
        {"packs": ["security"], "artifact": "doc", "request": "goal"}
    )
    assert run["status"] == "ok"
    assert run["total"] == 1
    assert run["findings"][0]["pack"] == "security"

    missing = await dispatcher._handle_run({"artifact": "  "})
    assert missing["status"] == "error"
    assert "artifact required" in missing["message"]

    bad = await dispatcher._handle_run({"packs": ["ghost"], "artifact": "a"})
    assert bad["status"] == "error"
    assert "unknown reviewer pack" in bad["message"]

    seed = await dispatcher._handle_seed({"base_dir": str(tmp_path / "seed")})
    assert seed["status"] == "ok"
    assert seed["count"] == 4
