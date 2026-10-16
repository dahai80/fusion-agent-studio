"""N4 subagent protocol (issue #341) — isolated sub-run execution engine.

SubAgentRunner builds a fresh AgentRuntime per run (own ToolRegistry limited
to the spec allowlist, own AgentContext/session) sharing only the parent
LLM gateway. The isolation contract is report-only: the parent receives the
final assistant text of the sub run, never tool outputs or intermediate
messages. Depth-1 hard isolation: the sub runtime pins _sub_graph_depth
above the recursion gate so any __sub_graph__ call inside the subagent is
denied by the existing E-16 depth gate.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph, NodeConfig
from .token_budget import TokenBudget

logger = logging.getLogger(__name__)

_FORK_MESSAGE_WINDOW = 20
_CONTROL_FLOW_TOOL_NAMES = ("register_tool", "unregister_tool")


@dataclass
class SubAgentSpec:
    name: str
    task: str
    model: str = ""
    tools: list[str] = field(default_factory=list)
    system_prompt: str = ""
    max_turns: int = 8
    budget_tokens: int = 0
    fork: bool = False
    team: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "task": self.task,
            "model": self.model,
            "tools": list(self.tools),
            "system_prompt": self.system_prompt,
            "max_turns": self.max_turns,
            "budget_tokens": self.budget_tokens,
            "fork": self.fork,
            "team": self.team,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubAgentSpec":
        return cls(
            name=data.get("name", ""),
            task=data.get("task", ""),
            model=data.get("model", ""),
            tools=list(data.get("tools", []) or []),
            system_prompt=data.get("system_prompt", ""),
            max_turns=int(data.get("max_turns", 8)),
            budget_tokens=int(data.get("budget_tokens", 0)),
            fork=bool(data.get("fork", False)),
            team=data.get("team", ""),
        )


@dataclass
class SubAgentReport:
    name: str
    ok: bool
    report: str
    turns: int = 0
    tokens_used: int = 0
    error: str = ""
    team: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "report": self.report,
            "turns": self.turns,
            "tokens_used": self.tokens_used,
            "error": self.error,
            "team": self.team,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SubAgentReport":
        return cls(
            name=data.get("name", ""),
            ok=bool(data.get("ok", False)),
            report=data.get("report", ""),
            turns=int(data.get("turns", 0)),
            tokens_used=int(data.get("tokens_used", 0)),
            error=data.get("error", ""),
            team=data.get("team", ""),
            duration_ms=int(data.get("duration_ms", 0)),
        )


class SubAgentRegistry:
    """Thread-safe store of named SubAgentSpec templates for reuse."""

    def __init__(self):
        self._lock = threading.Lock()
        self._specs: dict[str, SubAgentSpec] = {}

    def register(self, spec: SubAgentSpec) -> None:
        if not spec.name:
            raise ValueError("SubAgentSpec.name is required")
        with self._lock:
            self._specs[spec.name] = spec
        logger.info("subagent template registered name=%s", spec.name)

    def get(self, name: str) -> SubAgentSpec | None:
        with self._lock:
            return self._specs.get(name)

    def list(self) -> list[str]:
        with self._lock:
            return sorted(self._specs.keys())


class SubAgentRunner:
    """Runs isolated sub-agent executions against a parent runtime's LLM gateway."""

    def __init__(self, parent_runtime: Any):
        self.parent_runtime = parent_runtime
        self.last_events: list[AgentEvent] = []
        self.last_ctx: AgentContext | None = None
        self.last_runtime: Any = None

    def _resolve_model(self, spec: SubAgentSpec) -> str:
        if spec.model:
            return spec.model
        gw = getattr(self.parent_runtime, "llm_gateway", None)
        if gw is not None:
            for attr in ("default_model", "_default_model"):
                v = getattr(gw, attr, "")
                if v:
                    return str(v)
            try:
                cfg = gw.route()
                if cfg is not None and cfg.name:
                    return str(cfg.name)
            except Exception as e:
                logger.warning("subagent model route fallback failed: %s", e)
        return "default"

    def _build_registry(self, allowlist: list[str], spec_name: str = "") -> tuple[Any, list[str]]:
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        parent_tools = getattr(self.parent_runtime, "tools", None)
        missing: list[str] = []
        for name in allowlist:
            if name in _CONTROL_FLOW_TOOL_NAMES:
                logger.warning(
                    "subagent allowlist filtered control-flow tool=%s spec=%s",
                    name,
                    spec_name,
                )
                continue
            if parent_tools is None or not parent_tools.has(name):
                missing.append(name)
                continue
            reg.register(parent_tools.get(name))
        return reg, missing

    def _build_graph(self, spec: SubAgentSpec, model: str) -> AgentGraph:
        framing = (
            f"You are subagent '{spec.name}'. Complete the assigned task and reply "
            f"with your final report as plain text."
        )
        system_prompt = spec.system_prompt
        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{framing}"
        else:
            system_prompt = framing
        graph = AgentGraph(name=f"subagent:{spec.name}")
        graph.add_node("start", NodeConfig(type="start", label="start"))
        llm = NodeConfig(
            type="llm",
            label=f"sub_llm:{spec.name}",
            model=model,
            system_prompt=system_prompt,
            loop_mode="agent",
            max_loop_iterations=max(1, spec.max_turns),
        )
        if not spec.tools:
            llm.disable_tools = True
        graph.add_node("llm1", llm)
        graph.add_node("end", NodeConfig(type="end", label="end"))
        graph.add_edge("start", "llm1")
        graph.add_edge("llm1", "end")
        return graph

    def _build_sub_context(self, spec: SubAgentSpec, parent_ctx: AgentContext | None) -> AgentContext:
        if parent_ctx is not None:
            session_id = f"{parent_ctx.session_id}:sub:{spec.name}"
        else:
            session_id = f"sub:{spec.name}"
        ctx = AgentContext(
            session_id=session_id,
            metadata={"subagent": spec.name, "team": spec.team, "fork": spec.fork},
        )
        if spec.fork and parent_ctx is not None:
            seeded = [dict(m) for m in parent_ctx.messages if isinstance(m, dict)]
            ctx.messages = seeded[-_FORK_MESSAGE_WINDOW:]
        return ctx

    def _extract_report_text(self, ctx: AgentContext, seeded_count: int) -> str:
        for msg in reversed(ctx.messages[seeded_count:]):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                return str(msg.get("content", "") or "")
        return ""

    def _emit_lifecycle(self, event: str, payload: dict, session_id: str) -> None:
        bus = getattr(self.parent_runtime, "lifecycle_bus", None)
        if bus is None:
            return
        try:
            bus.emit("agent", event, payload, session_id=session_id)
        except Exception as e:
            logger.warning("subagent lifecycle emit failed event=%s err=%s", event, e)

    async def _fire_hook(self, event: str, payload: dict) -> None:
        hooks = getattr(self.parent_runtime, "hooks", None)
        if hooks is None:
            return
        try:
            await hooks.fire(event, payload)
        except Exception as e:
            logger.warning("subagent hook fire failed event=%s err=%s", event, e)

    async def run(self, spec: SubAgentSpec, parent_ctx: AgentContext | None = None) -> SubAgentReport:
        started = time.monotonic()
        model = self._resolve_model(spec)
        session_id = (
            f"{parent_ctx.session_id}:sub:{spec.name}"
            if parent_ctx is not None
            else f"sub:{spec.name}"
        )
        self._emit_lifecycle(
            "subagent_start",
            {
                "name": spec.name,
                "task_preview": spec.task[:120],
                "model": model,
                "fork": spec.fork,
                "team": spec.team,
            },
            session_id,
        )
        await self._fire_hook("SUBAGENT_START", {"name": spec.name, "task": spec.task, "team": spec.team})
        report: SubAgentReport | None = None
        try:
            report = await self._run_inner(spec, parent_ctx, model)
        except Exception as e:
            logger.error("subagent run failed name=%s err=%s", spec.name, e)
            report = SubAgentReport(name=spec.name, ok=False, report="", error=str(e), team=spec.team)
        report.duration_ms = int((time.monotonic() - started) * 1000)
        self._emit_lifecycle(
            "subagent_stop",
            {
                "name": report.name,
                "ok": report.ok,
                "turns": report.turns,
                "tokens_used": report.tokens_used,
                "duration_ms": report.duration_ms,
            },
            session_id,
        )
        await self._fire_hook(
            "SUBAGENT_STOP",
            {
                "name": report.name,
                "ok": report.ok,
                "turns": report.turns,
                "tokens_used": report.tokens_used,
            },
        )
        return report

    async def _run_inner(
        self,
        spec: SubAgentSpec,
        parent_ctx: AgentContext | None,
        model: str,
    ) -> SubAgentReport:
        from .runtime import AgentRuntime

        reg, missing = self._build_registry(list(spec.tools), spec_name=spec.name)
        if missing:
            logger.warning(
                "subagent allowlist missing tools=%s name=%s", missing, spec.name
            )
            return SubAgentReport(
                name=spec.name,
                ok=False,
                report="",
                error=f"missing tools not in parent registry: {', '.join(missing)}",
                team=spec.team,
            )

        sub_ctx = self._build_sub_context(spec, parent_ctx)
        seeded_count = len(sub_ctx.messages)

        sub_runtime = AgentRuntime(
            llm_gateway=getattr(self.parent_runtime, "llm_gateway", None),
            tool_registry=reg,
            max_iterations=max(1, spec.max_turns),
            safety_gateway=getattr(self.parent_runtime, "safety_gateway", None),
        )
        sub_runtime._sub_graph_depth = 999
        budget: TokenBudget | None = None
        if spec.budget_tokens > 0:
            budget = TokenBudget(max_tokens=spec.budget_tokens)
            sub_runtime._default_token_budget = budget

        graph = self._build_graph(spec, model)
        events: list[AgentEvent] = []
        async for event in sub_runtime.execute_graph(
            graph,
            spec.task,
            context=sub_ctx,
            token_budget=budget,
            max_iterations=max(1, spec.max_turns),
        ):
            events.append(event)
        self.last_events = events
        self.last_ctx = sub_ctx
        self.last_runtime = sub_runtime

        turns = sum(
            1
            for m in sub_ctx.messages[seeded_count:]
            if isinstance(m, dict) and m.get("role") == "assistant"
        )
        tokens_used = sub_ctx.token_usage().get("total", 0)
        report_text = self._extract_report_text(sub_ctx, seeded_count)
        error_text = sub_ctx.error
        ok = not error_text
        if budget is not None and any(
            e.type == AgentEventType.TOKEN_BUDGET_EXCEEDED for e in events
        ):
            ok = True
            logger.info(
                "subagent stopped by token budget name=%s spent=%d cap=%d",
                spec.name,
                budget.spent_tokens,
                budget.max_tokens,
            )
        logger.info(
            "subagent run done name=%s ok=%s turns=%d tokens=%d report_len=%d",
            spec.name,
            ok,
            turns,
            tokens_used,
            len(report_text),
        )
        return SubAgentReport(
            name=spec.name,
            ok=ok,
            report=report_text,
            turns=turns,
            tokens_used=tokens_used,
            error=error_text,
            team=spec.team,
        )
