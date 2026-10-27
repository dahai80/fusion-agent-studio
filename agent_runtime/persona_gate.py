"""#482: persona declarations consumed at runtime.

PersonaGate turns the declarations merged by agent_runtime.persona into
runtime enforcement:
- structured rule fragments ("allow|deny|ask: <tool-regex>") become
  PermissionRule objects fed to a per-context PermissionEngine (harness
  _VALID_LAYERS has no "persona" layer, so rules land in "global" with a
  "persona rule:" description tag — deny-first, only deny blocks here);
- memory_scope forces memory_read/memory_write scope to the declared scope,
  confining the agent's visible memory surface;
- free-text rule fragments are advisory (surfaced via agent.resolve_persona
  and logged, not silently dropped).

PersonaScopedFunnel wraps ToolFunnel.admit as the single choke point — every
funnel call site (tool node, LLM tool calls, parallel, register_tool,
self-repair) passes through it.
"""

from __future__ import annotations

import logging
import re

from fusion_harness.funnels import FunnelOutcome, ToolFunnel

logger = logging.getLogger(__name__)

_RULE_RE = re.compile(r"^(allow|deny|ask)\s*[:\s]\s*(.+?)\s*$", re.IGNORECASE)
MEMORY_SCOPE_TOOLS = {"memory_read", "memory_write"}


class PersonaGate:
    def __init__(self, declarations: dict):
        self.memory_scope = str(declarations.get("memory_scope", "") or "").strip()
        self.skills = [
            str(s).strip() for s in declarations.get("skills", []) if str(s).strip()
        ]
        self.advisory_rules: list[str] = []
        self.engine = None
        structured: list[tuple[str, str]] = []
        for raw in declarations.get("rules", []):
            fragment = str(raw).strip()
            if not fragment:
                continue
            m = _RULE_RE.match(fragment)
            if m and m.group(2).strip():
                structured.append((m.group(1).lower(), m.group(2).strip()))
            else:
                self.advisory_rules.append(fragment)
        if structured:
            from .permissions import PermissionEngine, PermissionRule

            engine = PermissionEngine(mode="approved")
            for action, tool in structured:
                engine.add_rule(
                    PermissionRule(
                        action=action,
                        tool=tool,
                        layer="global",
                        description=f"persona rule: {action} {tool}",
                    )
                )
            self.engine = engine
        if self.advisory_rules:
            logger.info(
                "persona advisory rules (non-structured, prompt-level): %s",
                "; ".join(self.advisory_rules),
            )

    @property
    def active(self) -> bool:
        return bool(self.memory_scope or self.skills or self.engine is not None)

    def skill_allowed(self, name: str) -> bool:
        if not self.skills:
            return True
        return name in self.skills

    def apply(self, tool_name: str, args: dict | None) -> tuple[bool, dict, str]:
        args = dict(args or {})
        if self.engine is not None:
            decision = self.engine.evaluate(tool_name, args)
            if decision.decision == "deny":
                logger.info(
                    "persona rule denied tool=%s reason=%s", tool_name, decision.reason
                )
                return False, args, decision.reason or "persona rule denied"
        if tool_name in MEMORY_SCOPE_TOOLS and self.memory_scope:
            if str(args.get("scope", "") or "") != self.memory_scope:
                logger.info(
                    "persona memory_scope forced tool=%s scope=%s",
                    tool_name,
                    self.memory_scope,
                )
                args["scope"] = self.memory_scope
        return True, args, ""


class PersonaScopedFunnel(ToolFunnel):
    async def admit(
        self, ctx=None, tool_name: str = "", args: dict | None = None, **kwargs
    ) -> FunnelOutcome:
        gate = self._persona_gate_for(ctx)
        if gate is not None:
            allowed, rewritten, reason = gate.apply(tool_name, args)
            if not allowed:
                return FunnelOutcome(
                    allowed=False,
                    blocked_by="persona",
                    reason=reason,
                    args=dict(args or {}),
                    needs_approval=False,
                    decision_log=[
                        {"stage": "persona", "verdict": "deny", "reason": reason}
                    ],
                )
            args = rewritten
        return await super().admit(
            ctx=ctx, tool_name=tool_name, args=args, **kwargs
        )

    @staticmethod
    def _persona_gate_for(ctx) -> PersonaGate | None:
        if ctx is None:
            return None
        cached = getattr(ctx, "_persona_gate", None)
        if cached is not None:
            return cached if cached.active else None
        declarations = getattr(ctx, "persona_declarations", None)
        if not declarations:
            return None
        try:
            gate = PersonaGate(declarations)
        except Exception as e:
            logger.warning("persona gate build failed: %s", e)
            return None
        try:
            ctx._persona_gate = gate
        except AttributeError:
            pass
        return gate if gate.active else None
