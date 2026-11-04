import os
import tempfile

import pytest

# #591: redirect the whole studio data home to a session temp dir BEFORE any
# test module (or production module constant) is imported, so no test may
# touch the real ~/.fusion-agent-studio. studio_home() and every derived
# path read this env var; subprocess-spawned daemons inherit it too.
if not os.environ.get("FUSION_AGENT_STUDIO_HOME"):
    os.environ["FUSION_AGENT_STUDIO_HOME"] = tempfile.mkdtemp(
        prefix="fas-test-home-"
    )


@pytest.fixture(autouse=True)
def _isolated_subagent_db(monkeypatch, tmp_path):
    # #423: subagent transcripts persist to SQLite; keep the suite off the
    # real ~/.fusion-agent-studio/subagents.db.
    monkeypatch.setenv("FUSION_SUBAGENT_DB", str(tmp_path / "subagents.db"))


@pytest.fixture(autouse=True)
def _isolated_session_and_checkpoint_stores(monkeypatch, tmp_path):
    # #425/#426/#429: keep session store + file checkpoints off real user dirs.
    monkeypatch.setenv("FUSION_SESSION_STORE", "sqlite")
    monkeypatch.setenv("FUSION_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("FUSION_FILE_CHECKPOINT_DIR", str(tmp_path / "file_checkpoints"))
    from agent_runtime.session_store import reset_session_store

    reset_session_store()
    yield
    reset_session_store()


@pytest.fixture(autouse=True)
def _rpc_auth_enforce_default_off(monkeypatch):
    # #326: RPC auth enforcement defaults ON in production. The legacy suite
    # exercises pre-auth RPC contracts over UDS, so default it OFF here;
    # the #326 contract itself is covered by test_issue_326_uds_auth.py,
    # which sets FUSION_AUTH_ENFORCE=1 explicitly per test (applied after
    # this autouse fixture, so the test-level value wins).
    monkeypatch.setenv("FUSION_AUTH_ENFORCE", "0")


def pytest_collection_modifyitems(config, items):
    # #591: run the data-home pollution guard last, after every other test.
    guard_first = None
    for i, item in enumerate(items):
        if item.fspath.strpath.endswith("test_data_home_guard.py"):
            guard_first = i
            break
    if guard_first is not None:
        guard = items.pop(guard_first)
        items.append(guard)
