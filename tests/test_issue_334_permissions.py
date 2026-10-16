from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_runtime.permissions import (
    PermissionEngine,
    PermissionMode,
    PermissionRule,
)

logger = logging.getLogger(__name__)


class _Ctx:
    def __init__(self, session_id="s1", plan_mode=False):
        self.session_id = session_id
        self.plan_mode = plan_mode


def _deny_engine(tool):
    return PermissionEngine(rules=[PermissionRule(action="deny", tool=tool, layer="global", description="blocked")], mode="approved")


def _allow_engine(tool):
    return PermissionEngine(rules=[PermissionRule(action="allow", tool=tool, layer="global", description="ok")], mode="approved")


def test_mode_normalize_unknown_falls_back_default():
    eng = PermissionEngine(mode="nope")
    assert eng.mode == PermissionMode.DEFAULT
    assert eng.get_mode() == "default"
    eng.set_mode("bypass")
    assert eng.get_mode() == "bypass"
    eng.set_mode(PermissionMode.APPROVED)
    assert eng.get_mode() == "approved"


def test_rule_matches_regex_and_invalid_regex():
    rule = PermissionRule(action="allow", tool="file_.*")
    assert rule.matches("file_read") is True
    assert rule.matches("net_write") is False
    bad = PermissionRule(action="allow", tool="([bad")
    assert bad.matches("anything") is False


def test_add_rule_invalid_action_ignored():
    eng = PermissionEngine()
    eng.add_rule(PermissionRule(action="nuke", tool="t1"), layer="global")
    assert eng.rules_count() == 0
    eng.add_rule(PermissionRule(action="deny", tool="t2"), layer="bogus_layer")
    assert eng.rules_count() == 1
    d = eng.evaluate("t2", {})
    assert d.decision == "deny"


def test_layer_scan_order_node_beats_graph_beats_global():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool="t1", layer="global"))
    eng.add_rule(PermissionRule(action="deny", tool="t1", layer="node"), layer="node")
    d = eng.evaluate("t1", {})
    assert d.decision == "deny"
    assert d.rule_source == "rule:node:t1"

    eng2 = PermissionEngine(mode="approved")
    eng2.add_rule(PermissionRule(action="deny", tool="t2", layer="global"), layer="global")
    eng2.add_rule(PermissionRule(action="allow", tool="t2", layer="graph"), layer="graph")
    d2 = eng2.evaluate("t2", {})
    assert d2.decision == "deny"
    assert d2.rule_source == "rule:global:t2"


def test_deny_beats_ask_and_allow_same_layer():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool="t1"))
    eng.add_rule(PermissionRule(action="deny", tool="t1"))
    assert eng.evaluate("t1", {}).decision == "deny"
    eng2 = PermissionEngine(mode="approved")
    eng2.add_rule(PermissionRule(action="allow", tool="t2"))
    eng2.add_rule(PermissionRule(action="ask", tool="t2"))
    d = eng2.evaluate("t2", {})
    assert d.decision == "ask" and d.rule_source == "rule:ask:global"


def test_content_scan_blocks_dangerous_args():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="allow", tool=".*"))
    d = eng.evaluate("t1", {"cmd": "rm -rf /"})
    assert d.decision == "deny" and d.rule_source == "content_scan"


def test_plan_mode_gate_deny_and_readonly_passthrough():
    eng = PermissionEngine(mode="approved")
    ctx = _Ctx(plan_mode=True)
    d = eng.evaluate("write_tool", {}, ctx=ctx)
    assert d.decision == "deny" and d.rule_source == "plan_mode"
    d2 = eng.evaluate("write_tool", {}, ctx=ctx, readonly_hint=True)
    assert d2.decision == "allow" and d2.rule_source == "heuristic_readonly"
    eng2 = PermissionEngine(mode="approved", plan_readonly_tools={"file_read"})
    d3 = eng2.evaluate("file_read", {}, ctx=ctx)
    assert d3.decision == "allow"


def test_bypass_mode_allows_even_with_deny_rules():
    eng = PermissionEngine(rules=[PermissionRule(action="deny", tool="t1")], mode="bypass")
    d = eng.evaluate("t1", {})
    assert d.decision == "allow" and d.rule_source == "mode:bypass"


def test_approved_mode_allows_unmatched():
    eng = PermissionEngine(mode="approved")
    d = eng.evaluate("anything", {})
    assert d.decision == "allow" and d.rule_source == "mode:approved"


def test_armed_default_mode_asks(monkeypatch):
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    eng = PermissionEngine(rules=[PermissionRule(action="allow", tool="other")])
    d = eng.evaluate("unlisted", {})
    assert d.decision == "ask" and d.rule_source == "mode:default"


def test_unarmed_engine_legacy_passthrough(monkeypatch):
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    eng = PermissionEngine()
    assert eng.is_armed() is False
    d = eng.evaluate("anything", {})
    assert d.decision == "allow" and d.rule_source == "legacy_passthrough"


def test_env_armed_default_asks(monkeypatch):
    monkeypatch.setenv("FUSION_PERMISSION_MODE", "default")
    eng = PermissionEngine()
    assert eng.is_armed() is True
    d = eng.evaluate("anything", {})
    assert d.decision == "ask" and d.rule_source == "mode:default"


def test_save_and_load_file_roundtrip(tmp_path: Path):
    p = tmp_path / "perm.json"
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="deny", tool="secret_tool", layer="global", description="d1"), layer="global")
    assert eng.save_to_file(p) is True
    data = json.loads(p.read_text())
    assert data["rules"][0]["tool"] == "secret_tool"
    eng2 = PermissionEngine(mode="approved")
    loaded = eng2.load_file_rules(p)
    assert loaded == 1
    assert eng2.evaluate("secret_tool", {}).decision == "deny"


def test_load_missing_file_returns_zero(tmp_path: Path):
    eng = PermissionEngine(mode="approved")
    assert eng.load_file_rules(tmp_path / "nope.json") == 0


def test_list_rules_and_rule_to_from_dict():
    eng = PermissionEngine(mode="approved")
    eng.add_rule(PermissionRule(action="ask", tool="t9", layer="node", description="dd"), layer="node")
    listed = eng.list_rules()
    assert listed[0]["action"] == "ask" and listed[0]["layer"] == "node"
    d = PermissionRule.from_dict({"action": "deny", "tool": "x", "layer": "weird"})
    assert d.layer == "global"
