"""N0 harness funnels — structural control points for tool/LLM/lifecycle.

Three architecture funnels (PRD 1016):
- LifecycleBus: append-only session event source (DSH-informed). All state
  changes flow through it; replayable; three event domains (session/agent/
  capability).
- ToolFunnel: THE single tool admission path — lifecycle -> hooks ->
  permission (deny-first) -> budget -> execute. Node code has no bypass
  route: they call funnel.run_tool or funnel.admit; the scattered
  safety_gateway.evaluate_action call sites in the node executors are
  collapsed into this chain.
- LLMFunnel: single LLM admission path — lifecycle -> hooks -> compaction ->
  metering.

Fail-closed: a failing security-gate hook or permission check blocks.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .permissions import (
    PermissionDecision,
    PermissionEngine,
)

logger = logging.getLogger(__name__)


class EventDomain(str, Enum):
    SESSION = "session"
    AGENT = "agent"
    CAPABILITY = "capability"


@dataclass
class LifecycleEvent:
    seq: int
    ts: float
    domain: str
    event: str = ""
    payload: dict = field(default_factory=dict)
    session_id: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "domain": self.domain
            if isinstance(self.domain, str)
            else self.domain.value,
            "event": self.event,
            "payload": self.payload,
            "session_id": self.session_id,
        }


class LifecycleBus:
    """Append-only lifecycle event source (three domains, replayable).

    In-memory bounded ring per session plus optional JSONL persistence.
    Subscribers get called synchronously on emit (hook consumption point);
    subscriber failures never break the emit (logged, fail-open for notify
    only — security gates are enforced by ToolFunnel, not here).
    """

    def __init__(self, maxlen: int = 10000, jsonl_dir: str | None = None):
        self._lock = threading.Lock()
        self._seq = 0
        self._events: dict[str, deque] = {}
        self._maxlen = maxlen
        self._jsonl_dir = jsonl_dir
        self._subscribers: list = []

    def subscribe(self, fn) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn) -> None:
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass

    def emit(self, domain: EventDomain | str, event: str, payload: dict | None = None, session_id: str = "") -> LifecycleEvent:
        with self._lock:
            self._seq += 1
            ev = LifecycleEvent(
                seq=self._seq,
                ts=time.time(),
                domain=domain.value if isinstance(domain, EventDomain) else str(domain),
                event=event,
                payload=payload or {},
                session_id=session_id,
            )
            dq = self._events.setdefault(ev.session_id, deque(maxlen=self._maxlen))
            dq.append(ev)
        if self._jsonl_dir:
            self._append_jsonl(ev)
        for fn in list(self._subscribers):
            try:
                fn(ev)
            except Exception as e:
                logger.warning("lifecycle subscriber error: %s", e)
        return ev

    def replay(self, session_id: str | None = None, domain: str | None = None, limit: int = 0) -> list[LifecycleEvent]:
        with self._lock:
            if session_id:
                events = list(self._events.get(session_id, deque()))
            else:
                events = [e for dq in self._events.values() for e in dq]
        if domain:
            events = [e for e in events if e.domain == domain]
        if limit:
            events = events[-limit:]
        return events

    def session_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._events.get(session_id, ()))

    def _append_jsonl(self, ev: LifecycleEvent) -> None:
        try:
            from pathlib import Path

            d = Path(self._jsonl_dir)
            d.mkdir(parents=True, exist_ok=True)
            name = ev.session_id or "global"
            path = d / f"{name}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as e:
            logger.warning("lifecycle jsonl append failed: %s", e)


@dataclass
class FunnelOutcome:
    """Result of the tool admission chain (decision log preserved)."""

    allowed: bool
    blocked_by: str = ""
    reason: str = ""
    args: dict = field(default_factory=dict)
    needs_approval: bool = False
    safety_result: Any = None
    decision_log: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "needs_approval": self.needs_approval,
            "decision_log": self.decision_log,
        }


class ToolFunnel:
    """Single tool admission + execution path.

    Chain (order IS the contract, deny-first at permission stage):
        lifecycle(tool_before) -> hooks(PRE_TOOL_USE) -> permission(deny-first)
        -> [needs_approval -> runtime handles future] -> budget -> execute
        -> lifecycle(tool_after) -> hooks(POST_TOOL_USE)
    """

    def __init__(
        self,
        hooks=None,
        permissions: PermissionEngine | None = None,
        lifecycle_bus: LifecycleBus | None = None,
        budget_ref=None,
        audit_logger=None,
        readonly_tools=None,
        safety_gateway=None,
    ):
        self.hooks = hooks
        self.permissions = permissions
        self.lifecycle = lifecycle_bus or LifecycleBus()
        self.budget_ref = budget_ref
        self.audit_logger = audit_logger
        # readonly_tools may be a set (static) or a zero-arg callable returning
        # a set (dynamic — registry can gain tools after funnel construction).
        self.readonly_tools = readonly_tools or set()
        self.safety_gateway = safety_gateway
        self._executions = 0

    def set_budget_ref(self, budget_ref) -> None:
        self.budget_ref = budget_ref

    async def admit(
        self,
        ctx=None,
        tool_name: str = "",
        args: dict | None = None,
        category: str = "tool_call",
        node_label: str = "",
        content_repr: str = "",
    ) -> FunnelOutcome:
        """Run the admission chain. Does NOT execute the tool."""
        args = args or {}
        log: list = []

        def _mark(stage: str, verdict: str, reason: str = "") -> None:
            log.append({"stage": stage, "verdict": verdict, "reason": reason})

        self._emit("capability", "tool_before", {"tool": tool_name, "args": args, "node": node_label}, ctx)

        # Stage 1: hooks (PRE_TOOL_USE) — rewrite or block.
        hook_out = None
        if self.hooks is not None:
            try:
                hook_out = await self.hooks.fire("PRE_TOOL_USE", {"tool_name": tool_name, "args": args}, tool_name=tool_name)
            except Exception as e:
                logger.warning("funnel hook fire error tool=%s err=%s", tool_name, e)
                hook_out = None
            if hook_out is not None:
                if getattr(hook_out, "decision", "approve") == "block":
                    _mark("hook", "deny", getattr(hook_out, "reason", "") or "pre_tool_use block")
                    return self._deny(ctx, tool_name, args, "hook", log, hook_out.reason)
                if getattr(hook_out, "updated_input", ""):
                    try:
                        args = json.loads(hook_out.updated_input) if isinstance(hook_out.updated_input, str) else dict(hook_out.updated_input)
                        _mark("hook", "rewrite", "updated_input applied")
                    except (TypeError, ValueError):
                        _mark("hook", "passthrough", "updated_input not JSON args, ignored")

        # Stage 2: permission engine (deny-first). plan_mode gate lives in the
        # engine's plan gate (ctx.plan_mode + not readonly -> deny).
        readonly = self._readonly_names()
        perm_decision: PermissionDecision | None = None
        if self.permissions is not None:
            try:
                perm_decision = self.permissions.evaluate(
                    tool_name, args, ctx=ctx, readonly_hint=tool_name in readonly
                )
            except Exception as e:
                logger.warning("permission evaluate error tool=%s err=%s", tool_name, e)
                perm_decision = PermissionDecision(decision="ask", rule_source="error_fallback", reason=f"permission error: {e}")
            if perm_decision.decision == "deny":
                _mark("permission", "deny", perm_decision.reason)
                return self._deny(ctx, tool_name, args, "permission", log, perm_decision.reason)
            _mark("permission", perm_decision.decision, perm_decision.reason)
            if perm_decision.decision == "ask":
                return FunnelOutcome(
                    allowed=False,
                    blocked_by="permission",
                    reason=perm_decision.reason,
                    args=args,
                    needs_approval=True,
                    decision_log=log,
                )

        # Stage 2b: SafetyGateway evaluation (legacy backend evaluator — the
        # funnel is the only entry; the gateway remains the approval-queue
        # implementation, R1 归一 = entry unification). Block -> deny;
        # requires_approval -> outcome carries the safety_result so the
        # runtime approval flow (futures/timeout/disposition) stays intact.
        if self.safety_gateway is not None:
            try:
                safety_result = self.safety_gateway.evaluate_action(
                    category=category,
                    content=content_repr or f"{tool_name}({args})",
                    context=f"tool={tool_name} node={node_label} path=funnel",
                )
            except Exception as e:
                logger.warning("safety evaluate error tool=%s err=%s", tool_name, e)
                safety_result = None
            if safety_result is not None:
                if safety_result.action.value == "block" and not safety_result.requires_approval:
                    _mark("safety", "deny", safety_result.reason)
                    return self._deny(ctx, tool_name, args, "safety", log, safety_result.reason)
                if safety_result.requires_approval:
                    _mark("safety", "ask", safety_result.reason)
                    return FunnelOutcome(
                        allowed=False,
                        blocked_by="safety",
                        reason=safety_result.reason,
                        args=args,
                        needs_approval=True,
                        decision_log=log,
                        safety_result=safety_result,
                    )

        # Stage 3: token budget.
        budget = None
        if self.budget_ref is not None:
            budget = self.budget_ref
        if budget is not None and getattr(budget, "is_exceeded", lambda: False)():
            _mark("budget", "deny", "token budget exceeded")
            return self._deny(ctx, tool_name, args, "budget", log, "token budget exceeded")

        _mark("admit", "allow", "all gates passed")
        out = FunnelOutcome(allowed=True, args=args, decision_log=log)
        if perm_decision is not None:
            out.decision_log.append({
                "stage": "permission",
                "verdict": "allow",
                "rule_source": perm_decision.rule_source,
            })
        return out

    def _readonly_names(self) -> set:
        # Support both static sets and a callable provider (registry may gain
        # tools after funnel construction — dynamic/plugin tools).
        ro = self.readonly_tools
        if callable(ro):
            try:
                return ro() or set()
            except Exception as e:
                logger.warning("readonly_tools provider failed: %s", e)
                return set()
        return ro or set()

    def _deny(self, ctx, tool_name: str, args: dict, gate: str, log: list, reason: str) -> FunnelOutcome:
        out = FunnelOutcome(allowed=False, blocked_by=gate, reason=reason, args=args, decision_log=log)
        self._emit("capability", "tool_deny", {"tool": tool_name, "gate": gate, "reason": reason, "args": args}, ctx)
        if self.audit_logger is not None:
            try:
                self.audit_logger.log(
                    actor="runtime",
                    action=f"funnel.deny.{gate}",
                    resource=tool_name,
                    details={"reason": reason, "args": args},
                )
            except Exception as e:
                logger.warning("funnel audit log failed: %s", e)
        return out

    def _emit(self, domain, event: str, payload: dict, ctx) -> None:
        try:
            self.lifecycle.emit(domain, event, payload, session_id=getattr(ctx, "session_id", "") or "")
        except Exception as e:
            logger.warning("lifecycle emit failed event=%s err=%s", event, e)

    async def run_tool(self, ctx, tool_name: str, args: dict, executor, node_label: str = "") -> tuple[str, bool]:
        """Admit then execute; returns (result_str, is_error).

        `executor` is a callable (tool, validated_args) -> awaitable str/ToolResult —
        supplied by the runtime so validation/config-merge stay where they are.
        """
        outcome = await self.admit(ctx=ctx, tool_name=tool_name, args=args, node_label=node_label)
        if outcome.needs_approval:
            return (
                f"Error: tool '{tool_name}' requires approval and no approval handler "
                f"is wired to this funnel; call from the runtime approval path",
                True,
            )
        if not outcome.allowed:
            blocked = f"Blocked by {outcome.blocked_by}: {outcome.reason or 'denied'}"
            logger.info("funnel blocked tool=%s gate=%s reason=%s", tool_name, outcome.blocked_by, outcome.reason)
            return blocked, True

        try:
            result = await executor(outcome.args)
        except Exception as e:
            result = f"Error: {e}"
        result_str = str(result)
        is_error = result_str.startswith("Error:")
        self._executions += 1
        await self.record_result(ctx, tool_name, args, result_str, is_error, node_label)
        return result_str, is_error

    async def record_result(
        self, ctx, tool_name: str, args: dict, result: str, is_error: bool, node_label: str = ""
    ) -> None:
        payload = {"tool_name": tool_name, "args": args, "result": result}
        if self.hooks is not None:
            try:
                await self.hooks.fire("POST_TOOL_USE", payload, tool_name=tool_name)
            except Exception as e:
                logger.warning("post hook error tool=%s err=%s", tool_name, e)
        self._emit(
            "capability",
            "tool_after",
            {"tool": tool_name, "node": node_label, "is_error": is_error, "result_preview": (result or "")[:200]},
            ctx,
        )


class LLMFunnel:
    """Single LLM admission path: lifecycle -> hooks -> budget -> compaction."""

    def __init__(
        self,
        hooks=None,
        lifecycle_bus: LifecycleBus | None = None,
        compactor=None,
        telemetry_engine=None,
        budget_ref=None,
    ):
        self.hooks = hooks
        self.lifecycle = lifecycle_bus or LifecycleBus()
        self.compactor = compactor
        self.telemetry_engine = telemetry_engine
        self.budget_ref = budget_ref
        self.calls = 0

    def set_budget_ref(self, budget_ref) -> None:
        self.budget_ref = budget_ref

    async def pre_llm(self, ctx, node_label: str = "", model: str = "", messages: list | None = None) -> tuple[bool, str, list]:
        """Gate the call. Returns (proceed, reason, possibly-compacted messages)."""
        messages = messages or []
        self._emit("agent", "llm_before", {"node": node_label, "model": model, "messages": len(messages)}, ctx)
        # N8: hard budget cap — exceed = graceful stop + BUDGET_EXCEED signal,
        # never silent truncation.
        budget = self.budget_ref
        if budget is not None and getattr(budget, "is_exceeded", lambda: False)():
            logger.warning("llm funnel budget exceeded node=%s spent=%s", node_label, getattr(budget, "spent_tokens", "?"))
            self._emit("agent", "budget_exceed", {"node": node_label, "spent_tokens": getattr(budget, "spent_tokens", 0)}, ctx)
            if self.hooks is not None:
                try:
                    await self.hooks.fire("BUDGET_EXCEED", {"node": node_label, "stage": "pre_llm"}, tool_name="")
                except Exception as e:
                    logger.warning("budget_exceed hook error: %s", e)
            return False, "token budget exceeded", messages
        if self.hooks is not None:
            try:
                out = await self.hooks.fire("LLM_BEFORE", {"node": node_label, "model": model}, tool_name="")
                if out is not None and out.decision == "block":
                    return False, f"blocked by hook: {out.reason or 'llm_before'}", messages
            except Exception as e:
                logger.warning("llm funnel hook error: %s", e)
        if self.compactor is not None and messages:
            try:
                level = self.compactor.should_compact(messages)
            except Exception as e:
                logger.warning("compactor.should_compact error: %s", e)
                level = "none"
            if level != "none":
                self._emit("agent", "compact_before", {"level": level, "messages": len(messages)}, ctx)
                try:
                    messages = self.compactor.compact(messages, level)
                except Exception as e:
                    logger.warning("compaction failed, continuing with original: %s", e)
                self._emit("agent", "compact_after", {"level": level, "messages": len(messages)}, ctx)
        return True, "", messages

    def post_llm(self, ctx, node_label: str = "", model: str = "", usage: dict | None = None, content: str = "") -> None:
        self.calls += 1
        # N8 metering: feed per-call usage into the wired budget.
        budget = self.budget_ref
        if budget is not None and usage:
            try:
                budget.record_usage(
                    int(usage.get("prompt_tokens", 0) or 0),
                    int(usage.get("completion_tokens", 0) or 0),
                )
            except Exception as e:
                logger.warning("budget record_usage failed: %s", e)
        self._emit("agent", "llm_after", {"node": node_label, "model": model, "usage": usage or {}}, ctx)
        if self.hooks is not None:
            try:
                import asyncio

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(
                        self.hooks.fire("LLM_AFTER", {"node": node_label, "model": model, "usage": usage or {}}, tool_name="")
                    )
                else:
                    loop.run_until_complete(self.hooks.fire("LLM_AFTER", {"node": node_label, "model": model, "usage": usage or {}}, tool_name=""))
            except Exception as e:
                logger.warning("llm_after hook error: %s", e)

    def _emit(self, domain, event: str, payload: dict, ctx) -> None:
        try:
            self.lifecycle.emit(domain, event, payload, session_id=getattr(ctx, "session_id", "") or "")
        except Exception as e:
            logger.warning("lifecycle emit failed event=%s err=%s", event, e)
