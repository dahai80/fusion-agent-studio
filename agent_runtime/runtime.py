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
from typing import TYPE_CHECKING, Any, AsyncIterator

from .compactor import Compactor
from .context import AgentContext, AgentEvent, AgentEventType
from .debugger import StepDebugger
from .graph import AgentGraph
from .llm_gateway import LLMGateway
from .prompt_templates import PromptTemplateManager
from .sub_graph import SubGraphRegistry
from .token_budget import TokenBudget
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
        self.hooks = None
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

        self.compactor = Compactor(memory_engine=self.memory_engine)
        if hasattr(self.llm_gateway, "set_compactor"):
            self.llm_gateway.set_compactor(self.compactor)

        logger.info(
            "AgentRuntime init, mlx_client=%s, llm_gateway=%s",
            "provided" if mlx_client else "none",
            "provided" if llm_gateway else ("auto-from-mlx" if mlx_client else "empty"),
        )

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
            graph, initial_input, context, token_budget,
            stream=False, max_iterations=max_iterations,
        ):
            yield event

    async def execute_graph_stream(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        max_iterations: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._run_with_trajectory(
            graph, initial_input, context, token_budget,
            stream=True, max_iterations=max_iterations,
        ):
            yield event

    async def _run_with_trajectory(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        stream: bool = False,
        max_iterations: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        # 审计 P0-3: 调用点未传 token_budget 时回退 runtime 实例默认预算.
        # 单点根治 dead-wire, 覆盖全部调用点 (daemon/api/orchestrator/sub-graph).
        if token_budget is None:
            token_budget = self._default_token_budget
        ctx = context or AgentContext()
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
        try:
            async for event in self._execute_graph_inner(
                graph, initial_input, ctx, token_budget, stream=stream,
                max_iterations=max_iterations,
            ):
                writer.record_event(ctx.session_id, event.to_dict())
                if event.type == AgentEventType.ERROR:
                    status = "error"
                if event.type == AgentEventType.START:
                    writer.record_iteration(ctx.session_id, getattr(ctx, "iteration_count", 0))
                yield event
        except Exception:
            status = "error"
            raise
        finally:
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
            mem_ctx = await asyncio.to_thread(self.memory_engine.recall_relevant, initial_input, 5)
            if mem_ctx:
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

        tools_schema = self.tools.to_openai_schemas()
        if any(n.allow_dynamic_tools for n in graph.nodes.values()):
            tools_schema.extend(self._dynamic_tool_schemas())

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
                        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
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
                yield AgentEvent(type=AgentEventType.END, content="Graph execution complete")
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
                logger.error("unknown node type=%s node=%s id=%s", node.type, node.label, current_node_id)
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error, node_id=node.label)
                return

        if ctx.is_max_iterations_reached():
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
            logger.warning(
                "A-1 reaped %d stranded futures for exec_id=%s", reaped, exec_id
            )

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
