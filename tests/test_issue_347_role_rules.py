"""R2 role-scoped permission rules (issue #347)."""

from agent_runtime.context import AgentContext
from agent_runtime.permissions import PermissionEngine, PermissionRule


def _engine():
    e = PermissionEngine()
    e.is_armed = lambda: True
    return e


def test_role_scoped_deny_applies_only_to_matching_role():
    e = _engine()
    e.add_rule(PermissionRule(action="deny", tool="shell_exec", layer="global", roles=["viewer"]))
    ctx = AgentContext()
    ctx.role = "viewer"
    d = e.evaluate("shell_exec", {}, ctx=ctx)
    assert d.decision == "deny"
    ctx2 = AgentContext()
    ctx2.role = "admin"
    d2 = e.evaluate("shell_exec", {}, ctx=ctx2)
    assert d2.decision != "deny"


def test_rule_without_roles_applies_to_all():
    e = _engine()
    e.add_rule(PermissionRule(action="deny", tool="shell_exec", layer="global"))
    for role in ("viewer", "admin", "all"):
        ctx = AgentContext()
        ctx.role = role
        assert e.evaluate("shell_exec", {}, ctx=ctx).decision == "deny"


def test_roles_all_wildcard_matches_every_role():
    e = _engine()
    e.add_rule(PermissionRule(action="deny", tool="file_write", layer="global", roles=["all"]))
    ctx = AgentContext()
    ctx.role = "editor"
    assert e.evaluate("file_write", {}, ctx=ctx).decision == "deny"


def test_no_ctx_defaults_all_role():
    e = _engine()
    e.add_rule(PermissionRule(action="deny", tool="shell_exec", layer="global", roles=["viewer"]))
    d = e.evaluate("shell_exec", {}, ctx=None)
    assert d.decision != "deny"


def test_rule_roles_roundtrip():
    r = PermissionRule(action="ask", tool="git_push", layer="team", roles=["editor", "admin"])
    d = r.to_dict()
    assert d["roles"] == ["editor", "admin"]
    r2 = PermissionRule.from_dict(d)
    assert r2.roles == ["editor", "admin"]


def test_role_scoped_ask_flow():
    from agent_runtime.permissions import PermissionMode

    e = PermissionEngine()
    e.mode = PermissionMode.APPROVED
    e.add_rule(PermissionRule(action="ask", tool="git_push", layer="global", roles=["editor"]))
    ctx = AgentContext()
    ctx.role = "editor"
    assert e.evaluate("git_push", {}, ctx=ctx).decision == "ask"
    ctx.role = "viewer"
    d = e.evaluate("git_push", {}, ctx=ctx)
    assert d.decision == "allow" and d.rule_source == "mode:approved"
