import pytest


@pytest.fixture(autouse=True)
def _isolated_subagent_db(monkeypatch, tmp_path):
    # #423: subagent transcripts persist to SQLite; keep the suite off the
    # real ~/.fusion-agent-studio/subagents.db.
    monkeypatch.setenv("FUSION_SUBAGENT_DB", str(tmp_path / "subagents.db"))


@pytest.fixture(autouse=True)
def _rpc_auth_enforce_default_off(monkeypatch):
    # #326: RPC auth enforcement defaults ON in production. The legacy suite
    # exercises pre-auth RPC contracts over UDS, so default it OFF here;
    # the #326 contract itself is covered by test_issue_326_uds_auth.py,
    # which sets FUSION_AUTH_ENFORCE=1 explicitly per test (applied after
    # this autouse fixture, so the test-level value wins).
    monkeypatch.setenv("FUSION_AUTH_ENFORCE", "0")
