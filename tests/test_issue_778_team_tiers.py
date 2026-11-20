# Issue #778: wire the harness tier vocabulary + fan-out ladder constants
# into the teams dispatcher.
#
# - model_tier passthrough: optional per-member tier hint validated against
#   kernel VALID_MODEL_TIERS before any kernel call; "" = policy default.
# - Fan-out ladder pre-validation (#124): >FANOUT_HARD_MAX blocked
#   client-side with the band in the message; teams above the complex band
#   get TOKEN_MULTIPLIER_WARNING in the reason payload.
# - Tier provenance: run_member surfaces tier + tier_source
#   (explicit/policy) so the studio UI can show why a member is cheap.
from __future__ import annotations

import pytest
from fusion_harness.spawn_protocol import TOKEN_MULTIPLIER_WARNING

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.reason_codes import classify_error
from tests.test_issue_341_subagent import _llm_response
from tests.test_issue_707_teams_dispatcher import _brief, _definition, _run, _runtime


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _def_with_tier(tier: str) -> dict:
    d = _definition()
    d["members"][0]["model_tier"] = tier
    return d


def _def_with_members(count: int) -> dict:
    d = _definition()
    d["members"] = [
        {
            "name": f"worker-{i:02d}",
            "role": "worker",
            "tool_allowlist": ["file_read"],
            "budget_tokens": 30000,
        }
        for i in range(count)
    ]
    return d


class TestModelTierPassthrough:
    async def test_valid_tier_accepted(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_tier("cheap")})
        assert out["status"] == "ok"
        assert rt.teams_registry["pair"].definition.member("worker-a").model_tier == "cheap"

    async def test_empty_tier_defaults(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _definition()})
        assert out["status"] == "ok"
        assert rt.teams_registry["pair"].definition.member("worker-a").model_tier == ""

    async def test_invalid_tier_typed_error_before_kernel(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_tier("mega")})
        assert out["status"] == "error"
        assert "unknown model tier" in out["message"]
        assert out.get("reason_code") == "model_tier_unknown"
        assert "pair" not in rt.teams_registry, "bad tier must not register the team"

    def test_invalid_tier_classifies_to_model_tier_unknown(self):
        code = classify_error(
            ValueError("unknown model tier 'mega' for member w (valid: cheap, strong, or empty)")
        )
        assert code == "model_tier_unknown"


class TestFanoutLadder:
    async def test_over_hard_max_blocked_client_side(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_members(21)})
        assert out["status"] == "error"
        assert "FANOUT_HARD_MAX=20" in out["message"]
        assert "21 members" in out["message"]
        assert out.get("reason_code") == "fanout_over_limit"
        assert "pair" not in rt.teams_registry

    def test_over_hard_max_classifies_to_fanout_over_limit(self):
        code = classify_error(
            ValueError("fanout hard max exceeded: 21 members exceeds FANOUT_HARD_MAX=20")
        )
        assert code == "fanout_over_limit"

    async def test_hard_band_carries_token_multiplier_warning(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_members(17)})
        assert out["status"] == "ok"
        assert out["fanout_band"] == "hard"
        assert out["warning"] == TOKEN_MULTIPLIER_WARNING

    async def test_complex_band_no_warning(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_members(10)})
        assert out["status"] == "ok"
        assert out["fanout_band"] == "complex"
        assert out["warning"] == ""

    async def test_simple_band(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _definition()})
        assert out["status"] == "ok"
        assert out["fanout_band"] == "simple"
        assert out["warning"] == ""


class TestTierProvenance:
    async def _setup(self, daemon, definition):
        rt, client = _runtime(responses=[_llm_response(content="member final report")])
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": definition})
        added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "do the work"})
        return rt, added["task_id"]

    async def test_explicit_tier_surfaced(self, daemon, _home):
        rt, tid = await self._setup(daemon, _def_with_tier("cheap"))
        out = await _run(
            daemon,
            "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": _brief()},
        )
        assert out["status"] == "ok"
        assert out["result"]["status"] == "completed"
        assert out["result"]["tier"] == "cheap"
        assert out["result"]["tier_source"] == "explicit"

    async def test_policy_default_surfaced(self, daemon, _home):
        from fusion_harness.tier_policy import recommend_tier

        rt, tid = await self._setup(daemon, _definition())
        out = await _run(
            daemon,
            "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": _brief()},
        )
        assert out["status"] == "ok"
        assert out["result"]["tier"] == recommend_tier("member_execution")
        assert out["result"]["tier_source"] == "policy"
