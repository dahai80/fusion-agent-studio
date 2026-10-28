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

import fnmatch
import logging
import os
import socket
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

# #389: explicit nesting limit. A subagent may not spawn another subagent.
MAX_SUBAGENT_DEPTH = 1


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
    # #421/#422: declarative subagent definition facets.
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    permission_mode: str = ""
    # #428: per-subagent compaction window + reasoning effort.
    effort: str = ""
    auto_compact_window: int = 0
    # #434: user / project / local memory scope.
    memory_scope: str = ""
    # #424: async-first spawn; background=True stays non-blocking.
    background: bool = False

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
            "disallowed_tools": list(self.disallowed_tools),
            "mcp_servers": list(self.mcp_servers),
            "skills": list(self.skills),
            "permission_mode": self.permission_mode,
            "effort": self.effort,
            "auto_compact_window": self.auto_compact_window,
            "memory_scope": self.memory_scope,
            "background": self.background,
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
            disallowed_tools=list(
                data.get("disallowed_tools", data.get("disallowedTools", [])) or []
            ),
            mcp_servers=list(data.get("mcp_servers", data.get("mcpServers", [])) or []),
            skills=list(data.get("skills", []) or []),
            permission_mode=str(
                data.get("permission_mode", data.get("permissionMode", "")) or ""
            ),
            effort=str(data.get("effort", "") or ""),
            auto_compact_window=int(
                data.get("auto_compact_window", data.get("autoCompactWindow", 0)) or 0
            ),
            memory_scope=str(data.get("memory_scope", data.get("memory", "")) or ""),
            background=bool(data.get("background", False)),
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
    agent_id: str = ""
    partial: bool = False

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
            "agent_id": self.agent_id,
            "partial": self.partial,
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
            agent_id=str(data.get("agent_id", "") or ""),
            partial=bool(data.get("partial", False)),
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
        self.last_seeded_count = 0
        self._transcripts = None
        self._task_manager = None

    def transcripts(self):
        if self._transcripts is None:
            from .subagent_sessions import SubAgentTranscriptStore

            self._transcripts = SubAgentTranscriptStore()
        return self._transcripts

    def task_manager(self):
        if self._task_manager is None:
            from .subagent_sessions import SubAgentTaskManager

            self._task_manager = SubAgentTaskManager(self, self.transcripts())
        return self._task_manager

    def spawn(self, spec: SubAgentSpec, parent_ctx: AgentContext | None = None) -> dict:
        return self.task_manager().spawn(spec, parent_ctx)

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

    def _build_registry(self, spec: SubAgentSpec) -> tuple[Any, list[str]]:
        from tools.registry import ToolRegistry

        reg = ToolRegistry()
        parent_tools = getattr(self.parent_runtime, "tools", None)
        missing: list[str] = []
        denied: list[str] = []
        for name in spec.tools:
            if name in _CONTROL_FLOW_TOOL_NAMES:
                logger.warning(
                    "subagent allowlist filtered control-flow tool=%s spec=%s",
                    name,
                    spec.name,
                )
                continue
            if parent_tools is None or not parent_tools.has(name):
                missing.append(name)
                continue
            tool = parent_tools.get(name)
            if self._denied_by_patterns(name, tool, spec.disallowed_tools):
                denied.append(name)
                continue
            if spec.mcp_servers and not self._server_allowed(tool, spec.mcp_servers):
                denied.append(name)
                continue
            reg.register(tool)
        if denied:
            logger.info(
                "subagent registry denied tools=%s name=%s", denied, spec.name
            )
        return reg, missing

    @staticmethod
    def _denied_by_patterns(name: str, tool: Any, patterns: list[str]) -> bool:
        for pattern in patterns or []:
            if not pattern:
                continue
            if pattern == name or fnmatch.fnmatch(name, pattern):
                return True
            if pattern.startswith("mcp__"):
                server = pattern[len("mcp__"):].split("__", 1)[0]
                tool_server = str(getattr(tool, "mcp_server", "") or "")
                if tool_server and tool_server == server:
                    return True
                if name.startswith(f"mcp__{server}__"):
                    return True
                if name.startswith(f"{server}:"):
                    return True
        return False

    @staticmethod
    def _server_allowed(tool: Any, servers: list[str]) -> bool:
        tool_server = str(getattr(tool, "mcp_server", "") or "")
        if not tool_server:
            return True
        return tool_server in servers

    def _load_skill_prompts(self, names: list[str]) -> str:
        if not names:
            return ""
        try:
            from .skills_store import SkillsStore

            store = SkillsStore()
        except Exception as e:
            logger.warning("subagent skill store unavailable: %s", e)
            return ""
        parts: list[str] = []
        for name in names:
            skill = store.load_skill(name)
            if skill is None:
                logger.warning("subagent skill not found name=%s", name)
                continue
            parts.append(f"## Skill: {name}\n{skill['body']}")
        if parts:
            logger.info("subagent skills preloaded count=%d", len(parts))
        return "\n\n".join(parts)

    @staticmethod
    def _memory_scope_key(scope: str, project_dir: str = "") -> str:
        if scope == "user":
            return "user"
        if scope == "local":
            return f"local:{socket.gethostname()}"
        if scope == "project":
            return f"project:{os.path.abspath(project_dir or os.getcwd())}"
        return ""

    def _memory_engine(self):
        return getattr(self.parent_runtime, "memory_engine", None)

    def _inject_scoped_memory(self, spec: SubAgentSpec, ctx: AgentContext) -> None:
        engine = self._memory_engine()
        scope = self._memory_scope_key(spec.memory_scope, spec.team or os.getcwd())
        if engine is None or not scope:
            if spec.memory_scope:
                logger.warning(
                    "subagent memory scope=%s requested but no memory engine, skipped",
                    spec.memory_scope,
                )
            return
        try:
            context = engine.recall_relevant(query=spec.task, limit=5, scope=scope)
        except Exception as e:
            logger.warning("subagent memory recall failed scope=%s: %s", scope, e)
            return
        if not context:
            logger.info("subagent memory recall empty scope=%s name=%s", scope, spec.name)
            return
        ctx.messages.insert(
            0, {"role": "system", "content": f"[Scoped memory {scope}]\n{context}"}
        )
        logger.info(
            "subagent memory injected scope=%s name=%s chars=%d",
            scope,
            spec.name,
            len(context),
        )

    def _commit_scoped_memory(self, spec: SubAgentSpec, report: SubAgentReport) -> None:
        engine = self._memory_engine()
        scope = self._memory_scope_key(spec.memory_scope, spec.team or os.getcwd())
        if engine is None or not scope or not report.ok or not report.report:
            return
        try:
            engine.store(
                content=f"[{spec.name}] task={spec.task[:200]}\noutcome={report.report[:800]}",
                scope=scope,
                tags="subagent",
                importance=6,
                memory_type="episodic",
            )
        except Exception as e:
            logger.warning("subagent memory commit failed scope=%s: %s", scope, e)
            return
        logger.info("subagent memory committed scope=%s name=%s", scope, spec.name)

    def _build_graph(self, spec: SubAgentSpec, model: str) -> AgentGraph:
        framing = (
            f"You are subagent '{spec.name}'. Complete the assigned task and reply "
            f"with your final report as plain text."
        )
        system_prompt = spec.system_prompt
        skill_prompt = self._load_skill_prompts(spec.skills)
        if skill_prompt:
            system_prompt = f"{system_prompt}\n\n{skill_prompt}" if system_prompt else skill_prompt
        if system_prompt:
            system_prompt = f"{system_prompt}\n\n{framing}"
        else:
            system_prompt = framing
        graph = AgentGraph(name=f"subagent:{spec.name}")
        graph.add_node("start", NodeConfig(type="start", label="start"))
        if spec.effort:
            logger.info(
                "subagent effort forwarded name=%s effort=%s model=%s",
                spec.name,
                spec.effort,
                model,
            )
        llm = NodeConfig(
            type="llm",
            label=f"sub_llm:{spec.name}",
            model=model,
            system_prompt=system_prompt,
            loop_mode="agent",
            max_loop_iterations=max(1, spec.max_turns),
            effort=spec.effort,
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

    async def run(
        self,
        spec: SubAgentSpec,
        parent_ctx: AgentContext | None = None,
        agent_id: str = "",
        seed_messages: list[dict] | None = None,
    ) -> SubAgentReport:
        started = time.monotonic()
        model = self._resolve_model(spec)
        session_id = (
            f"{parent_ctx.session_id}:sub:{spec.name}"
            if parent_ctx is not None
            else f"sub:{spec.name}"
        )
        transcripts = self.transcripts()
        if not agent_id:
            agent_id = transcripts.create(
                parent_session=getattr(parent_ctx, "session_id", "") if parent_ctx else "",
                name=spec.name,
                team=spec.team,
                model=model,
                task=spec.task,
                spec=spec.to_dict(),
            )
        # #487: spawned subagents auto-register into the DurableRoster so
        # team queries see live subagents immediately. Best-effort — roster
        # failures never break the run.
        try:
            from .durable_roster import DurableRoster

            DurableRoster().register(
                agent_id,
                name=spec.name,
                team=spec.team,
                capabilities=spec.tools,
                status="running",
            )
        except Exception as e:
            logger.warning(
                "roster register failed agent=%s name=%s err=%s",
                agent_id, spec.name, e,
            )
        self._emit_lifecycle(
            "subagent_start",
            {
                "name": spec.name,
                "agent_id": agent_id,
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
            report = await self._run_inner(
                spec, parent_ctx, model, seed_messages=seed_messages
            )
        except Exception as e:
            logger.error("subagent run failed name=%s err=%s", spec.name, e)
            report = SubAgentReport(name=spec.name, ok=False, report="", error=str(e), team=spec.team)
        report.agent_id = agent_id
        report.duration_ms = int((time.monotonic() - started) * 1000)
        if self.last_ctx is not None:
            transcripts.append(
                agent_id, self.last_ctx.messages[self.last_seeded_count:]
            )
        transcripts.finish(
            agent_id,
            ok=report.ok,
            partial=report.partial,
            turns=report.turns,
            tokens_used=report.tokens_used,
            report=report.report,
            error=report.error,
        )
        self._commit_scoped_memory(spec, report)
        # #487: final roster status — done/failed, best-effort.
        try:
            from .durable_roster import DurableRoster

            DurableRoster().set_status(agent_id, "done" if report.ok else "failed")
        except Exception as e:
            logger.warning("roster status update failed agent=%s err=%s", agent_id, e)
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
        seed_messages: list[dict] | None = None,
    ) -> SubAgentReport:
        from .runtime import AgentRuntime

        reg, missing = self._build_registry(spec)
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

        parent_depth = int(getattr(self.parent_runtime, "_sub_graph_depth", 0) or 0)
        if parent_depth >= MAX_SUBAGENT_DEPTH:
            logger.warning(
                "subagent depth limit hit name=%s depth=%d max=%d",
                spec.name,
                parent_depth,
                MAX_SUBAGENT_DEPTH,
            )
            return SubAgentReport(
                name=spec.name,
                ok=False,
                report="",
                error=(
                    f"subagent nesting depth {parent_depth} >= "
                    f"MAX_SUBAGENT_DEPTH={MAX_SUBAGENT_DEPTH}"
                ),
                team=spec.team,
            )
        sub_ctx = self._build_sub_context(spec, parent_ctx)
        if seed_messages:
            base_len = len(sub_ctx.messages)
            sub_ctx.messages = list(seed_messages) + sub_ctx.messages[base_len:]
            logger.info(
                "subagent resume seeded name=%s messages=%d", spec.name, len(seed_messages)
            )
        self._inject_scoped_memory(spec, sub_ctx)
        seeded_count = len(sub_ctx.messages)
        self.last_seeded_count = seeded_count

        sub_runtime = AgentRuntime(
            llm_gateway=getattr(self.parent_runtime, "llm_gateway", None),
            tool_registry=reg,
            # #460 fix: max_turns caps LLM turns (node max_loop_iterations +
            # agent-loop counter). The graph-level iteration cap counts NODE
            # VISITS (start + llm + end + re-entries) — without headroom a
            # complete single-answer run exhausted the cap before the end
            # node ran and reported "Max iterations exceeded" as a failure.
            max_iterations=max(1, spec.max_turns) + 2,
            safety_gateway=getattr(self.parent_runtime, "safety_gateway", None),
        )
        # #389: explicit depth semantics — one subagent level only. Nested
        # SubAgentRunner calls hit the MAX_SUBAGENT_DEPTH guard above; the
        # sub-graph gate is additionally pinned past its own limit so a
        # subagent can never recurse through __sub_graph__ either.
        from ._runtime_helpers import _max_sub_graph_depth

        sub_runtime._sub_graph_depth = max(parent_depth + 1, _max_sub_graph_depth())
        if spec.auto_compact_window > 0:
            from .compactor import CompactionConfig, Compactor

            sub_runtime.compactor = Compactor(
                config=CompactionConfig(context_window=spec.auto_compact_window),
                llm_gateway=getattr(self.parent_runtime, "llm_gateway", None),
                memory_engine=getattr(self.parent_runtime, "memory_engine", None),
            )
            logger.info(
                "subagent compaction window name=%s window=%d",
                spec.name,
                spec.auto_compact_window,
            )
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
            max_iterations=max(1, spec.max_turns) + 2,
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
        partial = str(getattr(sub_ctx, "stop_reason", "") or "") == "max_iterations"
        if partial:
            logger.warning(
                "subagent hit max_turns name=%s turns=%d max=%d, output marked partial",
                spec.name,
                turns,
                spec.max_turns,
            )
        logger.info(
            "subagent run done name=%s ok=%s turns=%d tokens=%d report_len=%d partial=%s",
            spec.name,
            ok,
            turns,
            tokens_used,
            len(report_text),
            partial,
        )
        return SubAgentReport(
            name=spec.name,
            ok=ok,
            report=report_text,
            turns=turns,
            tokens_used=tokens_used,
            error=error_text,
            team=spec.team,
            partial=partial,
        )

    async def resume(
        self,
        agent_id: str,
        task: str = "",
        parent_ctx: AgentContext | None = None,
        max_turns: int = 0,
    ) -> SubAgentReport | None:
        record = self.transcripts().get(agent_id)
        if record is None:
            logger.warning("subagent resume missing transcript agent_id=%s", agent_id)
            return None
        spec_data = dict(record.get("spec") or {})
        if not spec_data:
            logger.warning("subagent resume has no spec agent_id=%s", agent_id)
            return None
        spec = SubAgentSpec.from_dict(spec_data)
        spec.task = task or str(record.get("task") or "")
        if max_turns > 0:
            spec.max_turns = int(max_turns)
        history = [m for m in record.get("messages") or [] if isinstance(m, dict)]
        if spec.task:
            history.append({"role": "user", "content": spec.task})
        logger.info(
            "subagent resume agent_id=%s name=%s history=%d", agent_id, spec.name, len(history)
        )
        return await self.run(
            spec,
            parent_ctx,
            agent_id=agent_id,
            seed_messages=history,
        )
