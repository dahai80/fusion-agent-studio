"""Tests for issue #664 - LLM timeout must survive clean-env daemon respawns.

Supervisor auto-recovery / launchd respawn drops FUSION_LLM_TIMEOUT, and the
120s default made seat agents generating long documents on ~20 tok/s local
models structurally unable to finish. Default is now 900s, the client HTTP
deadline never undercuts the outer per-call wait, and start.sh persists the
override for respawned daemons.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime._runtime_nodes import _llm_call_timeout
from server.fusion_mlx_client import _llm_deadline_seconds

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FUSION_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("FUSION_LLM_DEADLINE_S", raising=False)


class TestOuterCallTimeout:
    def test_default_900_clean_env(self):
        # #664 acceptance: no env var needed for multi-minute generations.
        assert _llm_call_timeout() == 900.0

    def test_env_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_TIMEOUT", "2400")
        assert _llm_call_timeout() == 2400.0


class TestInnerDeadline:
    def test_default_matches_outer(self):
        assert _llm_deadline_seconds() == 900.0

    def test_falls_back_to_call_timeout_env(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_TIMEOUT", "600")
        assert _llm_deadline_seconds() == 600.0

    def test_deadline_env_wins_over_call_timeout(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_TIMEOUT", "600")
        monkeypatch.setenv("FUSION_LLM_DEADLINE_S", "1200")
        assert _llm_deadline_seconds() == 1200.0

    def test_invalid_values_ignored_not_crash(self, monkeypatch):
        monkeypatch.setenv("FUSION_LLM_DEADLINE_S", "abc")
        monkeypatch.setenv("FUSION_LLM_TIMEOUT", "600")
        assert _llm_deadline_seconds() == 600.0


class TestStartShPersistence:
    def test_start_sh_exports_default(self):
        # Respawn paths (supervisor auto-recovery, launchd) run start.sh with
        # a clean env — the default must be exported there, not only read.
        script = (REPO / "start.sh").read_text()
        assert 'FUSION_LLM_TIMEOUT="${FUSION_LLM_TIMEOUT:-900}"' in script
