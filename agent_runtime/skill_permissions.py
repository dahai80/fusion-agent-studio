"""#498: map SKILL.md allowed-tools onto the permission engine (role-scoped)."""

from __future__ import annotations

import logging
import re

from .permissions import PermissionRule

logger = logging.getLogger(__name__)

SKILL_ROLE_PREFIX = "skill:"
# Skill surface tools stay allowed for every role once skill rules arm the
# engine — otherwise skill_search / skill_invoke would themselves escalate
# to "ask" and the whole skill flow would lock up.
SKILL_SURFACE_TOOLS = ("skill_search", "skill_invoke", "skill.invoke")


def skill_role(name: str) -> str:
    return f"{SKILL_ROLE_PREFIX}{name}"


def permission_rules_for_skill(skill: dict) -> list[PermissionRule]:
    name = str(skill.get("name", "") or "")
    allowed = [str(t).strip() for t in (skill.get("allowed_tools") or []) if str(t).strip()]
    return [
        PermissionRule(
            action="allow",
            tool=re.escape(tool),
            layer="graph",
            description=f"skill:{name} allowed-tools entry",
            roles=[skill_role(name)],
        )
        for tool in allowed
    ]


def _surface_rules() -> list[PermissionRule]:
    return [
        PermissionRule(
            action="allow",
            tool=re.escape(tool),
            layer="global",
            description="skill surface tool (always allowed, #498)",
        )
        for tool in SKILL_SURFACE_TOOLS
    ]


def register_skill_permission_rules(engine, skill: dict) -> int:
    # Idempotent: same (tool, roles) pair never registers twice.
    if engine is None:
        return 0
    layers = getattr(engine, "_rules_by_layer", None)
    if layers is None:
        logger.warning("register_skill_permission_rules: engine has no _rules_by_layer")
        return 0
    existing = {
        (r.tool, tuple(r.roles))
        for rules in layers.values()
        for r in rules
    }
    added = 0
    for rule in permission_rules_for_skill(skill) + _surface_rules():
        key = (rule.tool, tuple(rule.roles))
        if key in existing:
            continue
        engine.add_rule(rule, layer=rule.layer)
        existing.add(key)
        added += 1
    if added:
        logger.info(
            "skill permission rules registered skill=%s added=%d",
            skill.get("name", ""),
            added,
        )
    return added


def register_matched_skill_rules(engine, store, text: str = "", path: str = "") -> list[dict]:
    # Trigger match + rule registration in one step — the caller (skills.match
    # RPC / skill_search tool) gets the matched list back for LLM exposure.
    matched = store.match_skills(text=text, path=path)
    for skill in matched:
        register_skill_permission_rules(engine, skill)
    return matched
