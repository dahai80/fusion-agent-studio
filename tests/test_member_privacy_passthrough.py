# Per-worker privacy passthrough (biz-team pipeline follow-up to #813).
#
# - teams.register accepts optional per-member privacy ('local-only' or '')
#   validated against harness VALID_PRIVACY_MODES before any kernel call
# - TeamRuntime.run_member carries it into spec.metadata['privacy'];
#   enforcement is fail-closed at kernel model resolve (subagent.py) —
#   covered by test_privacy_routing.py and harness test_member_privacy.py
from __future__ import annotations

import pytest

from agent_runtime.daemon_server import DaemonServer
from tests.test_issue_707_teams_dispatcher import _definition, _run, _runtime


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


def _def_with_privacy(privacy: str) -> dict:
    d = _definition()
    d["members"][0]["privacy"] = privacy
    return d


class TestMemberPrivacyPassthrough:
    async def test_local_only_accepted_and_carried(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_privacy("local-only")})
        assert out["status"] == "ok"
        assert rt.teams_registry["pair"].definition.member("worker-a").privacy == "local-only"

    async def test_empty_privacy_defaults(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _definition()})
        assert out["status"] == "ok"
        assert rt.teams_registry["pair"].definition.member("worker-a").privacy == ""

    async def test_invalid_privacy_typed_error_before_kernel(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        out = await _run(daemon, "teams.register", {"definition": _def_with_privacy("best-effort")})
        assert out["status"] == "error"
        assert "unknown privacy mode" in out["message"]
        assert "pair" not in rt.teams_registry, "bad privacy must not register the team"
