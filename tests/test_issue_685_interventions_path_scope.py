"""#685: intervention persistence + shared filesystem path scope.

Layers:
  - PathScope primitive: deny-by-default, `..` traversal rejection, bare
    roots + `**` globs, symlink resolution, relative rejection.
  - file_tools migration: FUSION_FILE_ROOTS routes through PathScope.
  - ToolIntervention persistence: AgentStore CRUD, SafetyGateway pending
    registration → approve/reject resolution with approver, reload
    re-surfacing of pending records after a "restart".
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent_runtime.path_scope import PathScope, PathScopeError
from agent_runtime.safety import SafetyLevel
from tools.file_tools import _is_write_blocked

# ---------------- PathScope ----------------

def _tmp_root(tmp_path, name="root"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return root


def test_deny_by_default(tmp_path):
    scope = PathScope(allowed=[], label="t")
    with pytest.raises(PathScopeError) as ei:
        scope.check(str(_tmp_root(tmp_path) / "f.txt"))
    assert "deny-by-default" in ei.value.reason


def test_relative_path_rejected():
    scope = PathScope(allowed=["/tmp/**"])
    with pytest.raises(PathScopeError) as ei:
        scope.check("relative/file.txt")
    assert "absolute required" in ei.value.reason


def test_bare_root_allows_beneath_rejects_outside(tmp_path):
    root = _tmp_root(tmp_path)
    other = _tmp_root(tmp_path, "other")
    scope = PathScope(allowed=[str(root)])
    assert scope.check(str(root / "sub" / "f.txt")) is not None
    assert scope.check(str(root)) is not None
    with pytest.raises(PathScopeError):
        scope.check(str(other / "f.txt"))


def test_parent_traversal_rejected(tmp_path):
    root = _tmp_root(tmp_path)
    scope = PathScope(allowed=[str(root) + "/**"])
    with pytest.raises(PathScopeError) as ei:
        scope.check(str(root / ".." / "etc" / "passwd"))
    assert "'..' rejected" in ei.value.reason


def test_glob_double_star_crosses_and_zero_depth(tmp_path):
    root = _tmp_root(tmp_path)
    scope = PathScope(allowed=[str(root) + "/**/*.txt"])
    assert scope.check(str(root / "a.txt")) is not None
    assert scope.check(str(root / "deep" / "a.txt")) is not None
    with pytest.raises(PathScopeError):
        scope.check(str(root / "a.md"))


def test_symlink_outside_rejected(tmp_path):
    root = _tmp_root(tmp_path)
    secret = _tmp_root(tmp_path, "secret")
    link = root / "leak"
    os.symlink(secret, link)
    scope = PathScope(allowed=[str(root) + "/**"])
    with pytest.raises(PathScopeError):
        scope.check(str(link))


def test_pattern_must_be_absolute():
    scope = PathScope(allowed=["relative/**"])
    with pytest.raises(PathScopeError):
        scope.check("/tmp/x")


# ---------------- file_tools migration ----------------

def test_file_write_roots_allow_and_block(tmp_path, monkeypatch):
    root = _tmp_root(tmp_path)
    monkeypatch.setenv("FUSION_FILE_ROOTS", str(root))
    monkeypatch.delenv("FUSION_FILE_ALLOW_SYSTEM", raising=False)
    assert _is_write_blocked(root / "ok.txt") is None
    blocked = _is_write_blocked(tmp_path / "outside.txt")
    assert blocked is not None and "FUSION_FILE_ROOTS" in blocked


def test_file_write_roots_traversal_blocked(tmp_path, monkeypatch):
    root = _tmp_root(tmp_path)
    monkeypatch.setenv("FUSION_FILE_ROOTS", str(root))
    blocked = _is_write_blocked(root / ".." / "escape.txt")
    assert blocked is not None and "'..'" in blocked


def test_file_write_no_roots_keeps_default_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSION_FILE_ROOTS", raising=False)
    monkeypatch.setenv("FUSION_FILE_ALLOW_SYSTEM", "1")
    assert _is_write_blocked(Path(tempfile.gettempdir()) / "whatever.txt") is None


# ---------------- ToolIntervention persistence ----------------

from agent_runtime.persistence import AgentStore  # noqa: E402
from tests._safety_mock import MockGuardClient  # noqa: E402


def _gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.guard_client._RULES_CACHE_PATH", str(tmp_path / "rc.json")
    )
    from agent_runtime.safety import SafetyGateway

    client = MockGuardClient()
    gw = SafetyGateway(level=SafetyLevel.L3, guard_client=client)
    store = AgentStore(str(tmp_path / "store.db"))
    gw.attach_intervention_store(store)
    return gw, store, client


def test_diff_preview_persists_pending(tmp_path, monkeypatch):
    gw, store, _ = _gateway(tmp_path, monkeypatch)
    req = gw.generate_diff_preview("orig", "proposed", "file_write")
    rec = store.get_intervention(req.action_id)
    assert rec is not None
    assert rec["status"] == "pending"
    assert rec["intervention_type"] == "diff_preview"
    assert rec["request"]["proposed"] == "proposed"
    pending = store.list_interventions(status="pending")
    assert [p["action_id"] for p in pending] == [req.action_id]


def test_approve_resolves_with_approver(tmp_path, monkeypatch):
    gw, store, client = _gateway(tmp_path, monkeypatch)
    req = gw.generate_diff_preview("orig", "proposed", "file_write")
    assert gw.approve_action(req.action_id, approver="alice") is True
    rec = store.get_intervention(req.action_id)
    assert rec["status"] == "approved"
    assert rec["approver"] == "alice"
    assert rec["resolved_at"] is not None
    assert client.confirm_calls[0]["action_id"] == req.action_id


def test_reject_resolves(tmp_path, monkeypatch):
    gw, store, _ = _gateway(tmp_path, monkeypatch)
    req = gw.generate_diff_preview("orig", "proposed", "file_write")
    assert gw.reject_action(req.action_id, approver="bob") is True
    assert store.get_intervention(req.action_id)["status"] == "rejected"
    assert gw.reject_action(req.action_id) is False


def test_reload_resurfaces_pending(tmp_path, monkeypatch):
    gw, store, _ = _gateway(tmp_path, monkeypatch)
    req = gw.generate_diff_preview("orig", "proposed", "file_write")
    req2 = gw.generate_diff_preview("o2", "p2", "file_write")
    gw.approve_action(req2.action_id, approver="carol")

    # "restart": fresh gateway, same store
    monkeypatch.setattr(
        "agent_runtime.guard_client._RULES_CACHE_PATH", str(tmp_path / "rc.json")
    )
    from agent_runtime.safety import SafetyGateway

    gw2 = SafetyGateway(level=SafetyLevel.L3, guard_client=MockGuardClient())
    reloaded = gw2.attach_intervention_store(store)
    assert reloaded == 1
    pending_ids = [a["action_id"] for a in gw2.get_pending_actions()]
    assert req.action_id in pending_ids
    assert req2.action_id not in pending_ids
    # pending record resolvable after reload
    assert gw2.approve_action(req.action_id, approver="dave") is True
    assert store.get_intervention(req.action_id)["status"] == "approved"


def test_persistence_failure_never_breaks_approval(tmp_path, monkeypatch):
    gw, store, _ = _gateway(tmp_path, monkeypatch)

    class _Boom:
        def record_intervention(self, *a, **k):
            raise RuntimeError("db down")

        def resolve_intervention(self, *a, **k):
            raise RuntimeError("db down")

        def list_interventions(self, *a, **k):
            return []

    gw.intervention_store = _Boom()
    req = gw.generate_diff_preview("orig", "proposed", "file_write")
    assert gw.approve_action(req.action_id) is True
    assert gw._pending_action_approvals[req.action_id]["status"] == "approved"


def test_store_crud_validation(tmp_path):
    store = AgentStore(str(tmp_path / "s.db"))
    store.record_intervention("a1", "safety_block", category="shell_exec", request={"content": "x"})
    with pytest.raises(ValueError):
        store.resolve_intervention("a1", "bogus")
    assert store.get_intervention("missing") is None
    assert store.list_interventions(status="approved") == []
