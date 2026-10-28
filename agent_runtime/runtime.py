"""Agent Runtime Engine — state machine that drives the agent execution loop.

The runtime coordinates the LLM -> tool -> observe -> decide cycle,
calling fusion-mlx via HTTP API for LLM inference and executing tools
locally. Supports streaming, condition expressions, variable interpolation,
debugger hooks, structured output, and sub-graph execution.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from .compactor import Compactor
from .context import AgentContext, AgentEvent, AgentEventType
from .debugger import StepDebugger
from .graph import AgentGraph
from .llm_gateway import LLMGateway
from .prompt_templates import PromptTemplateManager
from .sub_graph import SubGraphRegistry
from .token_budget import TokenBudget
from .tool_pipeline import PipelineScopedFunnel
from .trajectory_writer import get_trajectory_writer
from .variable_manager import VariableManager

if TYPE_CHECKING:
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry

    from .memory_engine import MemoryEngine
    from .persistence import AgentStore
    from .safety import SafetyGateway

from ._runtime_checkpoint import _CheckpointMixin
from ._runtime_dynamic_tools import _DynamicToolsMixin

# 审计 0826 P2-4 god-object 拆分: 模块级 helper/常量/logger 迁至 _runtime_helpers,
# mixin 模块从该叶子模块导入以避免循环依赖. 此处重新导出保持向后兼容
# (外部 `from .runtime import _env_flag` 仍可用).
from ._runtime_helpers import (  # noqa: F401
    _MAX_RETRY_CONTEXT_MESSAGES,
    _MAX_TOOL_CALL_CHAIN,
    ConditionEngine,
    _env_flag,
    _max_sub_graph_depth,
    _parallel_branch_concurrency,
    logger,
)
from ._runtime_nodes import _NodeExecutorsMixin
from ._runtime_safety import _SafetyApprovalMixin


class _FunnelAuditAdapter:
    # #479: adapt the harness funnel audit contract (.log(actor, action,
    # resource, details)) onto the repo AuditLogger (log_action). Deny
    # decisions from any funnel gate land in ~/.fusion-agent-studio/audit.db.
    def __init__(self, audit_logger):
        self._audit_logger = audit_logger

    def log(self, actor, action, resource, details=None):
        try:
            self._audit_logger.log_action(
                actor_id=actor,
                action=action,
                resource_type="tool",
                resource_id=resource,
                details=details or {},
                result="deny",
            )
        except Exception as e:
            logger.warning("funnel audit adapter write failed action=%s err=%s", action, e)


class AgentRuntime(_SafetyApprovalMixin, _CheckpointMixin, _DynamicToolsMixin, _NodeExecutorsMixin):
    """Agent runtime engine — state machine driving the agent execution loop.

    Integrates debugger, variables, structured output, prompt templates,
    sub-graph execution, and streaming support.

    审计 0826 P2-4: 方法组按 mixin 拆分 (_SafetyApprovalMixin/_CheckpointMixin/
    _DynamicToolsMixin/_NodeExecutorsMixin), 方法体原样搬运, 公共接口不变.
    """

    def __init__(
        self,
        mlx_client: "FusionMLXClient | None" = None,
        tool_registry: "ToolRegistry | None" = None,
        max_iterations: int = 25,
        debugger: StepDebugger | None = None,
        variables: VariableManager | None = None,
        templates: PromptTemplateManager | None = None,
        sub_graphs: SubGraphRegistry | None = None,
        condition_engine: ConditionEngine | None = None,
        llm_gateway: LLMGateway | None = None,
        safety_gateway: "SafetyGateway | None" = None,
        store: "AgentStore | None" = None,
        auto_checkpoint: bool = False,
        memory_engine: "MemoryEngine | None" = None,
        artifact_manager: Any = None,
        telemetry_engine: Any = None,
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        self.max_iterations = max_iterations
        self.debugger = debugger
        self.variables = variables or VariableManager()
        self.templates = templates or PromptTemplateManager()
        self.sub_graphs = sub_graphs or SubGraphRegistry()
        self.condition_engine = condition_engine or ConditionEngine()
        self.safety_gateway = safety_gateway
        self.store = store
        self.auto_checkpoint = auto_checkpoint
        self.memory_engine = memory_engine
        self.artifact_manager = artifact_manager
        # C13: 结构化遥测引擎 (span/counter/latency). 运行时插桩
        # graph.execute/llm.call/tool.call span, end_span 增计数+延迟.
        self.telemetry_engine = telemetry_engine
        self.compactor = None
        # N1: hook engine wiring point. Backing attr + property below —
        # assigning rt.hooks = HookEngine() propagates to both funnels so
        # late daemon wiring (daemon_server._get_hooks) stays in sync.
        self._hooks_engine = None
        self._tool_call_chain_count = 0
        # 审计 P3-1: 连续 checkpoint 失败计数迁 per-exec ctx.checkpoint_fail_count
        # (原 singleton, 并发执行互踩致阈值误判). 见 _save_checkpoint.
        self._safety_futures: dict[str, asyncio.Future[bool]] = {}
        # 审计 P1-21/R-4: _safety_timeout 原硬编码 60.0, headless/CI 无人工审批
        # 必挂满 60s. 读 FUSION_SAFETY_TIMEOUT env (秒), 默认 60.
        try:
            self._safety_timeout = float(os.environ.get("FUSION_SAFETY_TIMEOUT", "60"))
        except ValueError:
            self._safety_timeout = 60.0
        # 审计 A-1 Tier1: exec_id -> {action_id/plan_id} 反向索引. future 创建时
        # 记录归属 exec, exec 结束/异常时 reap 该 exec 残留 future (timeout 路径
        # 异常 skip pop 的兜底). 不清别 exec 的 future (并发安全).
        self._exec_future_keys: dict[str, set[str]] = {}
        self.tool_configs: dict[str, dict[str, Any]] = {}
        # C6 plan-as-mode: when True, write tools are gated off (read-only
        # explore phase). Flipped to False by exit_plan_mode tool call or by
        # graph.plan_mode at dispatch entry. _plan_futures blocks planner
        # nodes awaiting approval (mirrors _safety_futures pattern).
        self.plan_mode: bool = False
        self._plan_futures: dict[str, asyncio.Future[bool]] = {}
        # N6/#343 ToolSearch: lazy tool schema loading. ON by default (#488):
        # schemas beyond TOOLSEARCH_INLINE_LIMIT are deferred out of the
        # prompt; the LLM finds them via the tool_search tool at need.
        # Opt-out: FUSION_TOOLSEARCH=0/false/no restores full inline schemas.
        self._memory_tools_registered = False
        self._toolsearch_enabled = os.environ.get("FUSION_TOOLSEARCH", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        # #498: LLM-facing SKILL.md surface — see _register_skill_tools().
        # NOT wired here: subagent/orchestrator pass caller-owned filtered
        # registries whose exact tool sets are asserted (allowlist/denylist
        # isolation); only the top-level LLM surfaces (daemon/api) register.
        self._skills_store = None
        # #284: pluggable post-action assertion fn for tool nodes. Signature:
        #   (ctx, node, tool_result, frame_b64, frame_w, frame_h) -> str
        # Returns "" on pass, an error message on fail. Set via SDK; None = off.
        self.post_action_assertion_fn: Any = None
        # 审计 E-16/P0-4: 子图递归深度计数器. 无上限 -> 含子图循环引用 (A 子图
        # 指向 B, B 指向 A, 含无意构建) 触发无限递归 RecursionError 栈溢出崩溃
        # 整个 runtime 进程. 顶层执行 depth=0, 每进一层子图 +1, 超 _MAX_SUB_GRAPH_DEPTH
        # (默认 8, FUSION_SUB_GRAPH_MAX_DEPTH 调) 挡并报错.
        self._sub_graph_depth: int = 0
        # Read-only tools allowed during plan_mode. Write tools are blocked.
        self._plan_readonly_tools: set[str] = {
            "file_read",
            "file_list",
            "file_grep",
            "file_glob",
            "text_search",
            "text_process",
            "exit_plan_mode",
        }
        # 审计 D-5: register_tool/unregister_tool 是写操作 (注册工具 + 经
        # tool.register_python RPC 可 exec 源码), 不属于只读探索. 已移出
        # _plan_readonly_tools, plan_mode 只读期一并被挡, 与其他写工具一致.
        # Issue #149: optional per-node model unload to lower peak memory on
        # multi-model workflow chains. Default OFF to preserve model reuse
        # across consecutive same-model nodes. Env:
        # FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE=1/true/yes enables it.
        self.unload_model_after_node = _env_flag(
            "FUSION_AGENT_UNLOAD_MODEL_AFTER_NODE", default=False
        )
        # 审计 P0-3: token_budget dead-wire 根治. 所有 execute_graph 调用点不传
        # token_budget -> 预算永不生效. 不在 11 处调用点散写 getattr, 改在
        # runtime 实例存 _default_token_budget, execute_graph 未显式传时回退之.
        # daemon budget.set 写此属性; 子 runtime 在 _execute_sub_graph 复制父值.
        self._default_token_budget: TokenBudget | None = None

        if llm_gateway:
            self.llm_gateway = llm_gateway
        elif mlx_client:
            gw = LLMGateway()
            gw.set_default_client(mlx_client)
            self.llm_gateway = gw
        else:
            self.llm_gateway = LLMGateway()

        # #478: wire the gateway so compaction-triggered summarize calls run
        # on the gateway LLM (default :11432) instead of degrading to the
        # deterministic extractor.
        self.compactor = Compactor(memory_engine=self.memory_engine, llm_gateway=self.llm_gateway)
        if hasattr(self.llm_gateway, "set_compactor"):
            self.llm_gateway.set_compactor(self.compactor)

        # N0 harness funnels (PRD 1016): single tool/LLM admission paths +
        # append-only lifecycle event source. Built after llm_gateway so the
        # LLMFunnel can carry the compactor; hooks wire late via the
        # AgentRuntime.hooks property (daemon attaches HookEngine post-init).
        from .funnels import LifecycleBus, LLMFunnel
        from .permissions import PermissionEngine

        # Registry may be a mock in tests — tolerate missing annotation API.
        def _ro_names() -> set:
            if self.tools is None:
                return set()
            fn = getattr(self.tools, "readonly_tool_names", None)
            return set(fn()) if callable(fn) else set()

        self.lifecycle_bus = LifecycleBus(
            jsonl_dir=os.environ.get("FUSION_LIFECYCLE_JSONL_DIR", "") or None
        )
        self.permission_engine = PermissionEngine(
            readonly_tools=_ro_names(),
            plan_readonly_tools=set(self._plan_readonly_tools),
        )
        # readonly set may grow (dynamic/plugin tools) — funnel re-reads via
        # callable so late registrations are honored.
        _ro = _ro_names()
        # #482: PersonaScopedFunnel 包一层 — ctx.persona_declarations 有内容时
        # 先过 persona gate (结构化 rules deny / memory_scope 改写 / skills),
        # 其余行为与裸 ToolFunnel 一致. 单一 choke point, 6 个 admit 调用点全覆盖.
        # #5 AC4 (fusion-harness): PipelineScopedFunnel — every admit runs the
        # kernel ToolPipeline (five-stage segmented admission); the funnel
        # chain (hooks -> permission -> safety -> budget) is the pipeline's
        # policy stage, interactive approval resolves inside stage 3.
        self.tool_funnel = PipelineScopedFunnel(
            runtime=self,
            hooks=self._hooks_engine,
            permissions=self.permission_engine,
            lifecycle_bus=self.lifecycle_bus,
            readonly_tools=lambda: _ro_names() | set(_ro),
            safety_gateway=self.safety_gateway,
        )
        self.llm_funnel = LLMFunnel(
            hooks=self._hooks_engine,
            lifecycle_bus=self.lifecycle_bus,
            compactor=self.compactor,
            telemetry_engine=self.telemetry_engine,
        )
        # PRD N4: subagent protocol. #487: subagent_spawn is ON by default
        # (opt-out FUSION_SUBAGENT_TOOLS=0/false/no) — delegation must be
        # visible to the model without an undocumented env var. Registration
        # happens on the top-level LLM surfaces only (see
        # _register_subagent_tools); runner/registry stay lazy so bare
        # runtimes and subagent registries keep exact tool-set isolation.
        self.subagent_runner = None
        self.subagent_registry = None
        self._subagent_tools_enabled = os.environ.get(
            "FUSION_SUBAGENT_TOOLS", "1"
        ).strip().lower() not in ("0", "false", "no")
        logger.info(
            "harness funnels wired: tool_funnel=%s llm_funnel=%s permissions=%d rules",
            "on",
            "on",
            self.permission_engine.rules_count(),
        )

        logger.info(
            "AgentRuntime init, mlx_client=%s, llm_gateway=%s",
            "provided" if mlx_client else "none",
            "provided" if llm_gateway else ("auto-from-mlx" if mlx_client else "empty"),
        )

    # #454: `tool_registry` alias over the `tools` backing attr — dispatchers
    # (mcp.py/plugin.py) historically read/wrote runtime.tool_registry while
    # __init__ stores the kwarg as self.tools; the mismatch made the first
    # mcp.register_server/plugin RPC raise AttributeError.
    @property
    def tool_registry(self):
        return self.tools

    @tool_registry.setter
    def tool_registry(self, value):
        self.tools = value

    # N1: late hook-engine wiring — daemon sets rt.hooks = HookEngine() after
    # construction; the property keeps both funnels pointing at the same
    # engine so funnel admission and hooks.* RPCs share one registry.
    @property
    def hooks(self):
        return self._hooks_engine

    @hooks.setter
    def hooks(self, value):
        self._hooks_engine = value
        tool_funnel = getattr(self, "tool_funnel", None)
        if tool_funnel is not None:
            tool_funnel.hooks = value
        llm_funnel = getattr(self, "llm_funnel", None)
        if llm_funnel is not None:
            llm_funnel.hooks = value

    def set_audit_logger(self, audit_logger) -> None:
        # #479: wire the funnel deny path onto AuditLogger. Every funnel
        # deny (permission/safety/hook/budget gate) becomes an audit row.
        adapter = _FunnelAuditAdapter(audit_logger)
        self.tool_funnel.audit_logger = adapter
        logger.info("funnel audit logger wired")

    def set_tool_configs(self, definition: Any) -> None:
        # 声明式工具配置注入 (#125): 从 AgentDefinition.tools[].config 构建
        # tool_name -> config dict 映射, 工具执行时作为默认参数合并.
        self.tool_configs = self.build_tool_configs(definition)
        logger.info("set_tool_configs: %d tools with config", len(self.tool_configs))

    @staticmethod
    def build_tool_configs(definition: Any) -> dict[str, dict[str, Any]]:
        # 审计 P0-1/P1-1: 提取 config 构建为静态方法, 供 daemon per-exec 注入 ctx
        # 避免写 singleton rt.tool_configs (并发 agent X 配置覆盖 Y).
        configs: dict[str, dict[str, Any]] = {}
        tools = getattr(definition, "tools", None) or []
        for tc in tools:
            name = getattr(tc, "name", "") or ""
            cfg = getattr(tc, "config", None) or {}
            if name and isinstance(cfg, dict) and cfg:
                configs[name] = cfg
        return configs

    def _merge_tool_config_defaults(
        self, tool_name: str, args: dict, ctx: AgentContext | None = None
    ) -> dict:
        # 把 manifest 声明的 tool.config 作为默认参数合并; 调用方显式传参优先.
        # 审计 P0-1/P1-1: 优先读 per-exec ctx.tool_configs (隔离), 无则回退 singleton.
        cfg = None
        if ctx is not None:
            per_exec = getattr(ctx, "tool_configs", None)
            if per_exec:
                cfg = per_exec.get(tool_name)
        if cfg is None:
            cfg = self.tool_configs.get(tool_name)
        if not cfg:
            return args
        merged = dict(cfg)
        if isinstance(args, dict):
            merged.update(args)
        logger.debug(
            "tool %s config defaults merged: %d keys, overrides=%d",
            tool_name,
            len(cfg),
            len(args) if isinstance(args, dict) else 0,
        )
        return merged

    async def execute_graph(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        max_iterations: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        # 审计 P1-18: max_iterations 调用点覆盖 (per-agent). None=用 runtime 默认.
        async for event in self._run_with_trajectory(
            graph,
            initial_input,
            context,
            token_budget,
            stream=False,
            max_iterations=max_iterations,
        ):
            yield event

    def _make_session_event_log(self, ctx: AgentContext, graph: AgentGraph):
        # #4 AC4 (fusion-harness): durable append-only session event log.
        # One JSONL per session under ~/.fusion-agent-studio/session-events
        # (override: FUSION_SESSION_EVENT_LOG_DIR; disable:
        # FUSION_SESSION_EVENT_LOG=off). Never breaks the run on failure.
        from fusion_harness import SessionEventLog

        if os.environ.get("FUSION_SESSION_EVENT_LOG", "").strip().lower() in (
            "0",
            "off",
            "false",
            "no",
        ):
            return None
        log_dir = os.environ.get("FUSION_SESSION_EVENT_LOG_DIR") or os.path.expanduser(
            "~/.fusion-agent-studio/session-events"
        )
        try:
            log = SessionEventLog(ctx.session_id, log_dir=log_dir)
            log.append(
                "session_start",
                {
                    "graph": getattr(graph, "name", ""),
                    "graph_id": getattr(ctx, "graph_id", ""),
                    "agent_id": getattr(ctx, "agent_id", ""),
                },
            )
            logger.info("session event log started session=%s dir=%s", ctx.session_id, log_dir)
            return log
        except Exception as e:
            logger.warning("session event log init failed session=%s err=%s", ctx.session_id, e)
            return None

    def _session_log(self, ctx, event_type: str, payload: dict | None = None) -> None:
        # #4: single append helper for node/tool paths; failures are logged
        # and never break the graph run.
        log = getattr(ctx, "session_event_log", None)
        if log is None:
            return
        try:
            log.append(event_type, payload or {})
        except Exception as e:
            logger.warning("session event log append failed type=%s err=%s", event_type, e)

    async def execute_graph_stream(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        max_iterations: int | None = None,
        show_thinking: bool | None = None,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._run_with_trajectory(
            graph,
            initial_input,
            context,
            token_budget,
            stream=True,
            max_iterations=max_iterations,
            show_thinking=show_thinking,
        ):
            yield event

    async def _fire_observability_hook(self, event: str, payload: dict) -> None:
        if self.hooks is None:
            return
        try:
            await self.hooks.fire(event, payload)
        except Exception as e:
            logger.warning("observability hook error event=%s err=%s", event, e)

    def _resolve_persona_declarations(self, ctx: AgentContext) -> None:
        # #482: per-exec 解析 persona declarations. 失败 fail-open (声明层
        # 只能收紧不能放大攻击面, 解析报错不应弄挂执行).
        try:
            agent_id = getattr(ctx, "agent_id", "") or ctx.metadata.get("agent_id", "")
            if not agent_id:
                return
            from .persona import resolve_persona

            result = resolve_persona(
                agent_id=agent_id,
                tenant_id=ctx.metadata.get("tenant_id", ""),
                team_id=ctx.metadata.get("team_id", ""),
            )
            declarations = result.get("declarations") or {}
            if any(declarations.get(k) for k in ("rules", "memory_scope", "skills")):
                ctx.persona_declarations = declarations
                logger.info(
                    "persona declarations resolved agent=%s layers=%s",
                    agent_id,
                    result.get("layers_applied", []),
                )
        except Exception as e:
            logger.warning("persona declaration resolve failed: %s", e)

    async def _run_with_trajectory(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        stream: bool = False,
        max_iterations: int | None = None,
        show_thinking: bool | None = None,
    ) -> AsyncIterator[AgentEvent]:
        # 审计 P0-3: 调用点未传 token_budget 时回退 runtime 实例默认预算.
        # 单点根治 dead-wire, 覆盖全部调用点 (daemon/api/orchestrator/sub-graph).
        if token_budget is None:
            token_budget = self._default_token_budget
        # N8: funnels share the effective budget (tool + LLM admission gates).
        if token_budget is not None:
            self.tool_funnel.set_budget_ref(token_budget)
            self.llm_funnel.set_budget_ref(token_budget)
        ctx = context or AgentContext()
        # N8 #391: expose graph identity so funnel metering can charge the
        # graph-level budget cap. Explicit caller graph_id wins.
        if not getattr(ctx, "graph_id", ""):
            ctx.graph_id = getattr(graph, "id", "") or ""
        # #482: 集中解析 persona declarations (per-ctx, 审计 A-1 模式) —
        # execute_graph / execute_graph_stream / daemon / api 全走这里, 单点.
        self._resolve_persona_declarations(ctx)
        # #4 AC4 (fusion-harness): durable append-only session event log —
        # one JSONL per session, replayable after crash/restart. Lives on
        # ctx so every node/tool path appends through the same instance.
        ctx.session_event_log = self._make_session_event_log(ctx, graph)
        # N9 #348: thinking channel visibility — run-level override seeds
        # ctx vars; nodes may still opt out via tool_params.thinking=false.
        if show_thinking is not None:
            self._seed_ctx_variables(ctx)
            ctx.variables.set("show_thinking", bool(show_thinking))
        self._ensure_memory_tools()
        writer = get_trajectory_writer()
        trace_id = writer.start(
            session_id=ctx.session_id,
            graph_id=getattr(graph, "graph_id", ""),
            graph_name=graph.name,
            agent_id=getattr(ctx, "agent_id", ""),
            max_iterations=self.max_iterations,
        )
        logger.debug(
            "trajectory trace=%s session=%s graph=%s stream=%s",
            trace_id,
            ctx.session_id,
            graph.name,
            stream,
        )
        # C13: 结构化遥测 — graph.execute span. trace_id 复用 trajectory 关联.
        tele_span = None
        if self.telemetry_engine is not None:
            try:
                tele_span = self.telemetry_engine.start_span(
                    "graph.execute",
                    trace_id=trace_id,
                    attributes={"graph_name": graph.name, "stream": stream},
                )
            except Exception as e:
                logger.warning("telemetry start_span graph.execute failed: %s", e)
        status = "completed"
        # #479: GRAPH_BEFORE — organic fire point at graph execution entry.
        await self._fire_observability_hook(
            "GRAPH_BEFORE",
            {
                "graph_id": getattr(graph, "id", ""),
                "graph_name": graph.name,
                "session_id": ctx.session_id,
            },
        )
        # N14 #353: resident reviewer — verify node with resident:true upgrades
        # to a standing reviewer on the event stream (deterministic checks).
        resident = None
        runtime_review = None
        try:
            from .resident_reviewer import (
                build_resident_reviewer,
                build_runtime_reviewer,
                graph_has_resident_verify,
                runtime_review_enabled,
            )

            if graph_has_resident_verify(graph):
                resident = build_resident_reviewer(graph)
                logger.info("resident reviewer attached graph=%s", graph.name)
            # N10 #349: runtime-level standing review when no resident verify
            # node exists (resident mode is the stronger node-level variant).
            if resident is None and runtime_review_enabled():
                runtime_review = build_runtime_reviewer(graph)
                logger.info("runtime reviewer attached graph=%s", graph.name)
        except ImportError as e:
            logger.warning("resident reviewer unavailable: %s", e)
        try:
            last_event_id = ""
            async for event in self._execute_graph_inner(
                graph,
                initial_input,
                ctx,
                token_budget,
                stream=stream,
                max_iterations=max_iterations,
            ):
                # #497: event tree — stamp event_id/parent_id chain so every
                # run carries a linked event history (fork lands on it). Flat
                # consumers ignore the extra fields.
                if not event.event_id:
                    event.event_id = f"evt-{uuid.uuid4().hex[:12]}"
                if not event.parent_id:
                    event.parent_id = last_event_id
                last_event_id = event.event_id
                writer.record_event(ctx.session_id, event.to_dict())
                if resident is not None:
                    for finding in resident.observe(event):
                        yield AgentEvent(
                            type=AgentEventType.VERIFY,
                            content=f"[resident] {finding.kind}: {finding.detail}",
                            name="resident_reviewer",
                            node_id=finding.node_id,
                            metadata={"resident": True, **finding.to_dict()},
                        )
                if runtime_review is not None:
                    for finding in runtime_review.observe(event):
                        yield AgentEvent(
                            type=AgentEventType.VERIFY,
                            content=f"[runtime] {finding.kind}: {finding.detail}",
                            name="runtime_reviewer",
                            node_id=finding.node_id,
                            metadata={"level": "runtime", **finding.to_dict()},
                        )
                if event.type == AgentEventType.ERROR:
                    status = "error"
                    await self._fire_observability_hook(
                        "ERROR",
                        {"node": event.node_id, "content": event.content},
                    )
                elif event.type == AgentEventType.CHECKPOINT:
                    await self._fire_observability_hook(
                        "CHECKPOINT",
                        {"node": event.node_id, "content": event.content},
                    )
                if event.type == AgentEventType.START:
                    writer.record_iteration(ctx.session_id, getattr(ctx, "iteration_count", 0))
                yield event
        except asyncio.CancelledError:
            # #477: hard cancel (graph.cancel bg_task.cancel / supervisor stop).
            # Mark the context so evidence/checkpoint serialization carries the
            # reason; daemon cancel path surfaces it in run_end + task writeback.
            ctx.stop_reason = "cancelled"
            ctx.finished_at = time.time()
            logger.info("graph cancelled: %s session=%s", graph.name, ctx.session_id)
            raise
        except Exception:
            status = "error"
            raise
        finally:
            # #479: GRAPH_AFTER — organic fire point covering success, error
            # and exception paths (finally). status carries the outcome.
            await self._fire_observability_hook(
                "GRAPH_AFTER",
                {
                    "graph_id": getattr(graph, "id", ""),
                    "graph_name": graph.name,
                    "session_id": ctx.session_id,
                    "status": status,
                },
            )
            # N11 #350: session duration feeds SLO baselines.
            if self.telemetry_engine is not None and getattr(ctx, "started_at", 0):
                try:
                    self.telemetry_engine.record_slo("session", time.time() - ctx.started_at)
                except Exception as e:
                    logger.warning("session SLO record failed: %s", e)
            if resident is not None:
                try:
                    summary = resident.summary()
                    if ctx.variables is not None:
                        ctx.variables.set("review_findings", summary["findings"])
                        ctx.variables.set("review_counts", summary["counts"])
                    logger.info(
                        "resident review complete graph=%s findings=%s",
                        graph.name,
                        summary["counts"],
                    )
                except Exception as e:
                    logger.warning("resident review summary failed: %s", e)
            if runtime_review is not None:
                try:
                    summary = runtime_review.summary()
                    if ctx.variables is not None:
                        ctx.variables.set("runtime_review_findings", summary["findings"])
                        ctx.variables.set("runtime_review_counts", summary["counts"])
                    logger.info(
                        "runtime review complete graph=%s findings=%s",
                        graph.name,
                        summary["counts"],
                    )
                except Exception as e:
                    logger.warning("runtime review summary failed: %s", e)
            try:
                # 审计 P1-9/P0-1: 统一用 ctx (context or AgentContext()), 不再 context or ctx.
                # 之前传 context 给 inner 致轨迹-ctx 与执行-ctx 是两个对象 (context=None 时双建),
                # reaper/telemetry 用 ctx 而执行用另一 ctx, 状态断裂. 现统一.
                ctx_ref = ctx
                # 审计 A-1 Tier1: 清本 exec 残留 future (兜底异常路径).
                try:
                    self._reap_exec_futures(ctx_ref.session_id)
                except Exception as e:
                    logger.warning("A-1 reap_exec_futures failed: %s", e)
                writer.record_messages(
                    ctx_ref.session_id,
                    [m if isinstance(m, dict) else dict(m) for m in ctx_ref.messages],
                )
                usage = ctx_ref.token_usage() if hasattr(ctx_ref, "token_usage") else {}
                if usage:
                    writer.record_token_usage(ctx_ref.session_id, usage)
                writer.flush(ctx_ref.session_id, status=status)
            except (OSError, ValueError, TypeError) as e:
                logger.warning("trajectory flush failed: %s", e)
            # #4: durable session log — session boundary + fsync-close.
            # Outside the trajectory try so a flush failure still closes it.
            _session_log = getattr(ctx_ref, "session_event_log", None)
            if _session_log is not None:
                try:
                    _session_log.append("session_end", {"status": status})
                    _session_log.close()
                except Exception as e:
                    logger.warning("session event log close failed: %s", e)
            if tele_span is not None and self.telemetry_engine is not None:
                try:
                    self.telemetry_engine.end_span(tele_span.span_id, status=status)
                except Exception as e:
                    logger.warning("telemetry end_span graph.execute failed: %s", e)

    async def _execute_graph_inner(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        stream: bool = False,
        max_iterations: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        ctx = context or AgentContext()
        ctx.started_at = time.time()
        # 审计 P1-18: per-agent max_iterations 覆盖. None/0 用 runtime 默认.
        ctx.max_iterations = max_iterations if max_iterations else self.max_iterations
        # 审计 A-1: per-exec 状态迁 ctx, 不在 singleton 实例上 reset.
        # _tool_call_chain_count per-exec -> ctx (见 Tier 2).
        ctx.tool_call_chain_count = 0
        # 审计 A-1 Tier1: 跨 RPC future 注册表 keyed by globally-unique
        # action_id/plan_id, 并发安全本自带. 原代码 .clear() 会清掉并发
        # 执行 A 在途的 approval future (B 一启动把 A 的挂死). 移除 clear,
        # future 自身 pop 自清 (resolve/timeout/complete). exec 级 reaper
        # 守卫异常路径残留 (见 _register_exec_future/_reap_exec_futures).
        exec_id = ctx.session_id
        self._exec_future_keys.setdefault(exec_id, set())
        # C6: honor graph-level plan_mode flag at dispatch entry.
        # 审计 A-1 Tier2: plan_mode per-exec -> ctx, 不写 singleton.
        # graph.plan_mode True -> 强制只读 (子图也逃不掉). False 且 context
        # 由调用方提供 (子 runtime/并行分支) -> 继承调用方 ctx.plan_mode
        # (父只读期子分支也只读). False 且无 context (顶层 exec) -> 默认 False.
        if getattr(graph, "plan_mode", False):
            ctx.plan_mode = True
            logger.info("Graph %s entered plan_mode (read-only explore)", graph.id)
        elif context is None:
            ctx.plan_mode = False
        # #499: graph-level confirmation policy per-exec -> ctx.metadata,
        # mirroring plan_mode (A-1 Tier2: never on the singleton). The policy
        # layer in _await_safety_approval reads it via policy_from_ctx().
        _cp = getattr(graph, "confirmation_policy", None)
        if _cp:
            ctx.metadata["confirmation_policy"] = _cp
            logger.info("Graph %s confirmation policy active: %s", graph.id, _cp)
        # 审计 A-1/A-2 Tier3: variables per-exec. ctx.variables 为空时从
        # runtime seed (self.variables) 快照 copy — 隔离不共享, 并发/sub
        # 互不污染. sub runtime 把 sub_vars 传进 ctor (self.variables=sub_vars),
        # 子 dispatch 再 copy 进 sub_ctx, 父 self.variables 全程不被写.
        self._seed_ctx_variables(ctx)

        # #240: trigger_id 从 variables 透传到运行日志 (fusion-event 跨进程可追溯).
        _trig_id = ctx.variables.get("trigger_id") if ctx.variables else ""
        if _trig_id:
            logger.info("execute_graph %s trigger_id=%s", graph.id, _trig_id)

        errors = graph.validate()
        if errors:
            ctx.error = "; ".join(errors)
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(type=AgentEventType.START, content=f"Starting: {graph.name}")

        # Issue #175: lifecycle hook — session start.
        await self._fire_tool_hooks(
            "SESSION_START",
            "",
            {"graph_id": graph.id, "graph_name": graph.name},
        )

        initial_input = ctx.variables.interpolate(initial_input)
        ctx.add_message("user", initial_input)

        # Issue #175: lifecycle hook — user prompt submitted.
        await self._fire_tool_hooks(
            "USER_PROMPT_SUBMIT",
            "",
            {"input": initial_input, "graph_id": graph.id},
        )

        if self.memory_engine and initial_input:
            # N15/#354: recall injection token budget hard cap — memory must
            # not crowd out working context.
            inject_cap = self._memory_inject_tokens()
            mem_ctx = await asyncio.to_thread(self.memory_engine.recall_relevant, initial_input, 5)
            if mem_ctx:
                if len(mem_ctx) > inject_cap:
                    mem_ctx = mem_ctx[:inject_cap]
                    logger.info("memory injection truncated to %d chars (budget)", inject_cap)
                ctx.add_message("system", f"[Relevant memory]: {mem_ctx}")
                logger.info("Auto-loaded memory for input")

        start_node = graph.get_node(graph.start_node_id)
        system_prompt = ""
        if start_node:
            system_prompt = start_node.system_prompt
            if system_prompt:
                system_prompt = ctx.variables.interpolate(system_prompt)

        has_llm_nodes = any(n.type == "llm" for n in graph.nodes.values())
        model = graph.find_llm_model()
        if has_llm_nodes and not model:
            ctx.error = "No LLM model configured in graph"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        tools_schema = self.tools.to_openai_schemas(role=getattr(ctx, "role", "all"))
        if any(n.allow_dynamic_tools for n in graph.nodes.values()):
            tools_schema.extend(self._dynamic_tool_schemas())
        # N6/#343: defer schemas past the inline limit — tool stays callable
        # by name (registry resolves), schema discoverable via tool_search.
        if self._toolsearch_enabled:
            inline_cap = int(os.environ.get("TOOLSEARCH_INLINE_LIMIT", "25"))
            if len(tools_schema) > inline_cap:
                deferred = [s["function"]["name"] for s in tools_schema[inline_cap:]]
                tools_schema = tools_schema[:inline_cap]
                logger.info(
                    "ToolSearch deferral: %d schemas deferred (inline cap %d)",
                    len(deferred),
                    inline_cap,
                )

        current_node_id = graph.start_node_id
        ctx.current_node_id = current_node_id

        while current_node_id and not ctx.is_max_iterations_reached():
            node = graph.get_node(current_node_id)
            if not node:
                ctx.error = f"Node '{current_node_id}' not found"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            ctx.iteration_count += 1
            ctx.current_node_id = current_node_id

            if self.debugger:
                await self.debugger.check_pause(current_node_id, ctx.variables.to_dict())

            # #479: NODE_BEFORE — organic fire point per node visit.
            await self._fire_observability_hook(
                "NODE_BEFORE",
                {
                    "node_id": current_node_id,
                    "node_type": node.type,
                    "iteration": ctx.iteration_count,
                },
            )
            try:
                if node.type == "start":
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""
                    continue

                elif node.type == "llm":
                    async for event in self._execute_llm_node(
                        ctx, node, graph, model, tools_schema, system_prompt, stream=stream
                    ):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return

                    if self.auto_checkpoint:
                        await self._save_checkpoint(ctx, graph)

                    if token_budget:
                        usage = ctx.token_usage()
                        token_budget.record_usage(
                            usage.get("prompt_tokens", 0),
                            usage.get("completion_tokens", 0),
                            reasoning_tokens=usage.get("reasoning_tokens", 0),
                        )
                        # Proactive context pruning at 70% budget
                        if (
                            token_budget.max_tokens > 0
                            and token_budget.spent_tokens >= int(token_budget.max_tokens * 0.7)
                            and self.compactor
                        ):
                            if not getattr(ctx, "_pruning_done", False):
                                ctx._pruning_done = True
                                artifact_tokens = 0
                                if self.artifact_manager:
                                    try:
                                        budget_info = self.artifact_manager.get_context_budget(
                                            agent_id=getattr(ctx, "agent_id", "")
                                        )
                                        artifact_tokens = budget_info.get("total_tokens", 0)
                                    except (ValueError, TypeError, RuntimeError, OSError):
                                        pass
                                logger.info(
                                    "Proactive context pruning at %d/%d tokens (70%% threshold), artifact_tokens=%d",
                                    token_budget.spent_tokens,
                                    token_budget.max_tokens,
                                    artifact_tokens,
                                )
                                await self._fire_tool_hooks(
                                    "PRE_COMPACT",
                                    "",
                                    {
                                        "graph_id": graph.id,
                                        "node_id": node.label or "",
                                        "reason": "token_budget_pruning",
                                    },
                                )
                                # 审计修复: compact() 同步返回 list[dict] (compactor.py:74),
                                # 不能 await (TypeError 'list' object can't be awaited), 且
                                # 须赋回 ctx.messages (原丢弃结果致压缩未生效). 对齐 L742 用法.
                                ctx.messages = self.compactor.compact(ctx.messages)
                                yield AgentEvent(
                                    type=AgentEventType.THINK,
                                    content="Context proactively pruned at 70% budget",
                                    metadata={
                                        "pruning_threshold": 0.7,
                                        "artifact_tokens": artifact_tokens,
                                    },
                                )
                        if token_budget.is_exceeded():
                            mode = "stream" if stream else "batch"
                            logger.warning(
                                "Token budget exceeded (%s): %d/%d",
                                mode,
                                token_budget.spent_tokens,
                                token_budget.max_tokens,
                            )
                            yield AgentEvent(
                                type=AgentEventType.TOKEN_BUDGET_EXCEEDED,
                                content=f"Token budget exceeded: {token_budget.spent_tokens}/{token_budget.max_tokens}",
                                metadata=token_budget.status(),
                            )
                            ctx.stop_reason = "budget_exceeded"
                            ctx.finished_at = time.time()
                            return

                    # Agent Loop: 内生多轮工具回灌 (loop_mode=="agent")
                    # 末条为 tool 结果 => stop_reason=tool_use, 回灌 LLM 继续推理
                    if node.loop_mode == "agent":
                        max_iter = node.max_loop_iterations or 25
                        for loop_i in range(max_iter):
                            last_msg = ctx.messages[-1] if ctx.messages else {}
                            if last_msg.get("role") != "tool":
                                break
                            # 审计 P1-10/R-5: 内生 agent loop 独立计数器 loop_i, 不累加
                            # ctx.iteration_count -> is_max_iterations_reached 盲区, 无上限
                            # 防护. 每轮累加会话级计数, 达 max 即停.
                            ctx.iteration_count += 1
                            if ctx.is_max_iterations_reached():
                                logger.warning(
                                    "agent loop session max iterations reached: %d node=%s",
                                    ctx.iteration_count,
                                    current_node_id,
                                )
                                if not ctx.stop_reason:
                                    ctx.stop_reason = "max_iterations"
                                break
                            # Compaction 接入点 (M2): 超阈值先压缩再回灌
                            if self.compactor is not None:
                                level = self.compactor.should_compact(ctx.messages)
                                if level != "none":
                                    before = len(ctx.messages)
                                    # Issue #175: lifecycle hook — pre-compact.
                                    await self._fire_tool_hooks(
                                        "PRE_COMPACT",
                                        "",
                                        {
                                            "graph_id": graph.id,
                                            "node_id": current_node_id,
                                            "before": before,
                                            "level": level,
                                        },
                                    )
                                    ctx.messages = self.compactor.compact(ctx.messages, level)
                                    logger.info(
                                        "compaction applied level=%s before=%d after=%d node=%s",
                                        level,
                                        before,
                                        len(ctx.messages),
                                        current_node_id,
                                    )
                                    # N11 #350: compaction count feeds SLO baselines.
                                    if self.telemetry_engine is not None:
                                        self.telemetry_engine.record_slo("compaction")
                            # Hooks 接入点 (M3)
                            ctx.tool_call_chain_count = 0
                            logger.info(
                                "agent loop iter=%d/%d node=%s msgs=%d",
                                loop_i + 1,
                                max_iter,
                                current_node_id,
                                len(ctx.messages),
                            )
                            async for event in self._execute_llm_node(
                                ctx,
                                node,
                                graph,
                                model,
                                tools_schema,
                                system_prompt,
                                stream=stream,
                            ):
                                yield event
                                if event.type == AgentEventType.ERROR:
                                    return
                            if self.auto_checkpoint:
                                await self._save_checkpoint(ctx, graph)
                        else:
                            logger.warning(
                                "agent loop max iterations reached: %d node=%s",
                                max_iter,
                                current_node_id,
                            )
                            # Issue #175: lifecycle hook — stop (agent-loop cap).
                            await self._fire_tool_hooks(
                                "STOP",
                                "",
                                {
                                    "graph_id": graph.id,
                                    "node_id": current_node_id,
                                    "reason": "agent_loop_max",
                                },
                            )

                    last_msg = ctx.messages[-1] if ctx.messages else {}
                    if last_msg.get("tool_calls"):
                        pass
                    else:
                        ctx.tool_call_chain_count = 0
                        # Issue #149: optional per-node model unload. Only fire
                        # when the node is fully done (advancing to the next
                        # node), never during tool-call re-entry on the same
                        # node. Non-fatal: a failed/already-evicted unload is a
                        # warning, not a workflow error.
                        if self.unload_model_after_node:
                            served_model = node.model or model
                            if served_model:
                                await self.llm_gateway.unload_model(served_model)
                        next_id = graph.get_next_node(current_node_id)
                        current_node_id = next_id or ""

                elif node.type == "tool":
                    async for event in self._execute_tool_node(ctx, node, graph):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return

                    if self.auto_checkpoint:
                        await self._save_checkpoint(ctx, graph)

                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "condition":
                    event = self._execute_condition_node(ctx, node)
                    yield event
                    current_node_id = (
                        graph.get_next_node(current_node_id, condition_result=event.content) or ""
                    )

                elif node.type == "loop":
                    event = self._execute_loop_node(ctx, node, graph)
                    yield event
                    if event.content == "loop_continue":
                        loop_start = node.tool_params.get("loop_start_node", "")
                        if loop_start and graph.get_node(loop_start):
                            current_node_id = loop_start
                        else:
                            next_id = graph.get_next_node(current_node_id)
                            current_node_id = next_id or ""
                    else:
                        next_id = graph.get_next_node(current_node_id)
                        current_node_id = next_id or ""

                elif node.type == "error_handler":
                    async for event in self._execute_error_handler_node(ctx, node, graph):
                        yield event
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "rag":
                    async for event in self._execute_rag_node(
                        ctx, node, graph, model, tools_schema, system_prompt, stream=stream
                    ):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "planner":
                    async for event in self._execute_planner_node(ctx, node, graph):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "verify":
                    async for event in self._execute_verify_node(ctx, node, graph):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "acp":
                    # #496: external agent CLI via ACP — events stream onto
                    # the SSE surface like any other node.
                    async for event in self._execute_acp_node(ctx, node, graph):
                        yield event
                        if event.type == AgentEventType.ERROR:
                            return
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

                elif node.type == "parallel":
                    async for ev in self._execute_parallel_node(
                        ctx, node, graph, current_node_id, stream=stream
                    ):
                        yield ev
                        if ev.type == AgentEventType.ERROR:
                            return
                    next_id = ev.metadata.get("next_id", "") if ev.metadata else ""
                    current_node_id = next_id

                elif node.type == "end":
                    if not ctx.stop_reason:
                        ctx.stop_reason = "end_turn"
                    yield AgentEvent(
                        type=AgentEventType.END,
                        content="Graph execution complete",
                        metadata={"stop_reason": ctx.stop_reason},
                    )
                    ctx.finished_at = time.time()
                    await self._auto_store_memory(ctx, graph)
                    # Issue #175: lifecycle hook — session end (clean completion).
                    await self._fire_tool_hooks(
                        "SESSION_END",
                        "",
                        {"graph_id": graph.id, "status": "completed"},
                    )
                    return

                else:
                    # 审计 L-1: 未知 node.type 原本无 else 分支 — 不匹配任何
                    # elif 时静默跳过, current_node_id 不更新, while 循环空转
                    # 至 max_iterations 才报错, 既浪费又误导 (报 "max iterations"
                    # 而非 "unknown node type"). 现显式报错并终止.
                    ctx.error = (
                        f"Unknown node type '{node.type}' on node '{node.label}' "
                        f"(id={current_node_id})"
                    )
                    logger.error(
                        "unknown node type=%s node=%s id=%s", node.type, node.label, current_node_id
                    )
                    yield AgentEvent(
                        type=AgentEventType.ERROR, content=ctx.error, node_id=node.label
                    )
                    return
            finally:
                # #479: NODE_AFTER — fires on advance, continue and
                # early return alike (finally on the node dispatch).
                await self._fire_observability_hook(
                    "NODE_AFTER",
                    {
                        "node_id": current_node_id,
                        "node_type": node.type,
                        "iteration": ctx.iteration_count,
                    },
                )

        if ctx.is_max_iterations_reached():
            if not ctx.stop_reason:
                ctx.stop_reason = "max_iterations"
            ctx.error = "Max iterations exceeded"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            # Issue #175: lifecycle hook — stop (iteration cap reached).
            await self._fire_tool_hooks(
                "STOP",
                "",
                {"graph_id": graph.id, "reason": "max_iterations"},
            )

        ctx.finished_at = time.time()
        await self._auto_store_memory(ctx, graph)

    def _memory_inject_tokens(self) -> int:
        # N15: recall injection budget (chars ~ 4x tokens hard cap).
        try:
            return int(os.environ.get("FUSION_MEMORY_INJECT_TOKENS", "2000")) * 4
        except ValueError:
            return 8000

    def _ensure_memory_tools(self) -> None:
        # N15/#354: memory tool group registered once when engine wired.
        if self._memory_tools_registered or self.memory_engine is None or self.tools is None:
            return
        try:
            from tools.memory_tools import create_memory_tools

            for t in create_memory_tools(lambda: self.memory_engine):
                self.tools.register(t)
            self._memory_tools_registered = True
            logger.info("memory tool group registered (4 tools)")
        except Exception as e:
            logger.warning("memory tools registration failed: %s", e)

    def _get_skills_store(self):
        # #498: lazy SkillsStore for the skill tool providers — same dirs as
        # the daemon's skills RPC surface (user + project + plugins).
        if self._skills_store is None:
            from .skills_store import SkillsStore

            self._skills_store = SkillsStore.with_project_dirs()
        return self._skills_store

    def _register_toolsearch(self) -> None:
        # N6/#343/#488: lazy tool-schema loading — tool_search registered on
        # the top-level LLM surfaces (daemon / api_server) only, never by
        # subagent runtimes whose registries are caller-filtered (allowlist /
        # denylist isolation asserts exact tool sets).
        if not self._toolsearch_enabled or self.tools is None:
            return
        try:
            from tools.tool_search import ToolSearchTool

            self.tools.register(ToolSearchTool(lambda: self.tools))
            logger.info("ToolSearch enabled: tool_search registered")
        except Exception as e:
            logger.warning("ToolSearch init failed: %s", e)
            self._toolsearch_enabled = False

    def _register_subagent_tools(self) -> None:
        # #487: LLM-facing subagent_spawn tool — ON by default (opt-out via
        # FUSION_SUBAGENT_TOOLS=0/false/no). Same rule as toolsearch/skill
        # tools: registered on the top-level LLM surfaces (daemon/api_server)
        # only, never by subagent runtimes whose registries are
        # caller-filtered (allowlist/denylist isolation asserts exact sets).
        if not self._subagent_tools_enabled or self.tools is None:
            return
        if self.tools.has("subagent_spawn"):
            return
        try:
            from tools.subagent_tool import SubAgentTool

            from .subagent import SubAgentRegistry, SubAgentRunner
            from .subagent_defs import get_definition_registry

            if self.subagent_runner is None:
                self.subagent_runner = SubAgentRunner(self)
            if self.subagent_registry is None:
                self.subagent_registry = SubAgentRegistry()
            self.tools.register(
                SubAgentTool(
                    runner_provider=lambda: self.subagent_runner,
                    definition_provider=get_definition_registry().get,
                )
            )
            logger.info("subagent tools registered (#487 default-on)")
        except Exception as e:
            logger.warning("subagent tools init failed: %s", e)

    def _register_skill_tools(self) -> None:
        # #498: LLM-facing SKILL.md surface — skill_search (trigger match +
        # rule registration) and skill_invoke (rendered prompt). Providers keep
        # the store/engine lazy so wiring stays cheap. Called by the top-level
        # LLM surfaces (daemon / api_server), never by subagent runtimes whose
        # registries are caller-filtered.
        if self.tools is None:
            return
        try:
            from tools.skill_tools import SkillInvokeTool, SkillSearchTool

            self.tools.register(
                SkillSearchTool(self._get_skills_store, lambda: self.permission_engine)
            )
            self.tools.register(
                SkillInvokeTool(self._get_skills_store, lambda: self.permission_engine)
            )
            logger.info("skill tools registered (issue #498)")
        except Exception as e:
            logger.warning("skill tools init failed: %s", e)

    def _register_exec_future(self, exec_id: str, key: str) -> None:
        # 审计 A-1 Tier1: future 创建时登记归属 exec_id, 供 reap 索引.
        self._exec_future_keys.setdefault(exec_id, set()).add(key)

    def _reap_exec_futures(self, exec_id: str) -> None:
        # 审计 A-1 Tier1: exec 结束/异常时清本 exec 残留 future (timeout 路径
        # 异常 skip pop 的兜底). 仅清本 exec 注册的 key, 不动并发别 exec.
        keys = self._exec_future_keys.pop(exec_id, None)
        if not keys:
            return
        reaped = 0
        for key in keys:
            for registry in (self._safety_futures, self._plan_futures):
                fut = registry.pop(key, None)
                if fut is not None and not fut.done():
                    fut.cancel()
                    reaped += 1
        if reaped:
            logger.warning("A-1 reaped %d stranded futures for exec_id=%s", reaped, exec_id)

    def _seed_ctx_variables(self, ctx: AgentContext) -> None:
        # 审计 A-1/A-2 Tier3: ctx.variables 播种. dispatch 入口 + 直接调用
        # 节点 handler (测试/并行分支) 共用. None 时从 runtime seed
        # (self.variables) 快照 copy — 隔离不共享. 已播种 (None check) 跳过.
        if ctx.variables is None:
            ctx.variables = VariableManager()
            ctx.variables.load_from(self.variables.to_dict())

    def set_knowledge_engine(self, engine: Any) -> None:
        if hasattr(engine, "embedding_fn") and engine.embedding_fn is None and self.llm_gateway:
            import asyncio
            import concurrent.futures

            def _sync_embed(text: str) -> list[float]:
                try:
                    asyncio.get_running_loop()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(asyncio.run, self.llm_gateway.aembed(text)).result()
                except RuntimeError:
                    return asyncio.run(self.llm_gateway.aembed(text))

            try:
                test_emb = _sync_embed("test")
                if test_emb and len(test_emb) > 0:
                    engine.embedding_fn = _sync_embed
                    logger.info("Wired real embedding_fn to KnowledgeEngine")
            except Exception as e:
                logger.warning("Could not wire embedding_fn: %s", e)

        self._knowledge_engine = engine
        logger.info("Knowledge engine set on runtime")
