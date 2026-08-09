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

logger = logging.getLogger(__name__)

_MAX_TOOL_CALL_CHAIN = 10
_MAX_RETRY_CONTEXT_MESSAGES = 20


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

    def _resolve_value(
        self, name: str, ctx: AgentContext, variables: VariableManager
    ) -> Any:
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
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw

    def _compare(self, left: Any, op: str, right: Any) -> str:
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
        self.compactor = None
        self.hooks = None
        self._tool_call_chain_count = 0
        self._safety_futures: dict[str, asyncio.Future[bool]] = {}
        self._safety_timeout: float = 60.0

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
            trace_id, ctx.session_id, graph.name, stream,
        )
        status = "completed"
        try:
            async for event in self._execute_graph_inner(
                graph, initial_input, context, token_budget, stream=stream
            ):
                writer.record_event(ctx.session_id, event.to_dict())
                if event.type == AgentEventType.ERROR:
                    status = "error"
                if event.type == AgentEventType.START:
                    writer.record_iteration(
                        ctx.session_id, getattr(ctx, "iteration_count", 0)
                    )
                yield event
        except Exception:
            status = "error"
            raise
        finally:
            try:
                ctx_ref = context or ctx
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
        self._tool_call_chain_count = 0
        self._safety_futures.clear()

        errors = graph.validate()
        if errors:
            ctx.error = "; ".join(errors)
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(type=AgentEventType.START, content=f"Starting: {graph.name}")

        initial_input = self.variables.interpolate(initial_input)
        ctx.add_message("user", initial_input)

        if self.memory_engine and initial_input:
            mem_ctx = await asyncio.to_thread(
                self.memory_engine.recall_relevant, initial_input, 5
            )
            if mem_ctx:
                ctx.add_message("system", f"[Relevant memory]: {mem_ctx}")
                logger.info("Auto-loaded memory for input")

        start_node = graph.get_node(graph.start_node_id)
        system_prompt = ""
        if start_node:
            system_prompt = start_node.system_prompt
            if system_prompt:
                system_prompt = self.variables.interpolate(system_prompt)

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
                await self.debugger.check_pause(
                    current_node_id, self.variables.to_dict()
                )

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
                        and token_budget.spent_tokens
                        >= int(token_budget.max_tokens * 0.7)
                        and self.compactor
                    ):
                        if not getattr(ctx, "_pruning_done", False):
                            ctx._pruning_done = True
                            artifact_tokens = 0
                            if self.artifact_manager:
                                try:
                                    budget_info = (
                                        self.artifact_manager.get_context_budget(
                                            agent_id=getattr(ctx, "agent_id", "")
                                        )
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
                                ctx.messages = self.compactor.compact(
                                    ctx.messages, level
                                )
                                logger.info(
                                    "compaction applied level=%s before=%d after=%d node=%s",
                                    level,
                                    before,
                                    len(ctx.messages),
                                    current_node_id,
                                )
                        # Hooks 接入点 (M3)
                        self._tool_call_chain_count = 0
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

                last_msg = ctx.messages[-1] if ctx.messages else {}
                if last_msg.get("tool_calls"):
                    pass
                else:
                    self._tool_call_chain_count = 0
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

            elif node.type == "tool":
                async for event in self._execute_tool_node(ctx, node, graph):
                    yield event

                if self.auto_checkpoint:
                    await self._save_checkpoint(ctx, graph)

                next_id = graph.get_next_node(current_node_id)
                current_node_id = next_id or ""

            elif node.type == "condition":
                event = self._execute_condition_node(ctx, node)
                yield event
                current_node_id = (
                    graph.get_next_node(current_node_id, condition_result=event.content)
                    or ""
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
                yield AgentEvent(
                    type=AgentEventType.NODE_START,
                    content=f"Parallel fan-out from {current_node_id}",
                    node_id=current_node_id,
                )
                outgoing = graph.get_outgoing_edges(current_node_id)
                if outgoing:
                    next_id = outgoing[0].target_id
                    current_node_id = next_id or ""
                else:
                    current_node_id = ""

            elif node.type == "end":
                yield AgentEvent(
                    type=AgentEventType.END, content="Graph execution complete"
                )
                ctx.finished_at = time.time()
                await self._auto_store_memory(ctx, graph)
                return

        if ctx.is_max_iterations_reached():
            ctx.error = "Max iterations exceeded"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)

        ctx.finished_at = time.time()
        await self._auto_store_memory(ctx, graph)

    @staticmethod
    def _detect_unclosed_artifacts(content: str) -> list[str]:
        opens = re.findall(r'<artifact[^>]*\bid=["\']([^"\']+)["\']', content)
        closes = re.findall(r'</artifact>', content)
        if len(opens) > len(closes):
            return opens[len(closes):]
        tag_opens = len(re.findall(r'<artifact-ref[^>]*>', content))
        tag_closes = len(re.findall(r'</artifact-ref>', content))
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
                    "variables": self.variables.to_dict(),
                    "tool_call_chain_count": self._tool_call_chain_count,
                },
            )
            logger.debug(
                "Checkpoint saved: graph=%s node=%s", graph.name, ctx.current_node_id
            )
        except Exception as e:
            logger.warning("Checkpoint save failed: %s", e)

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

        checkpoint = self.store.load_latest_checkpoint(
            graph_id=graph.name, session_id=session_id
        )
        if not checkpoint:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"No checkpoint found for graph={graph.name} session={session_id}",
            )
            return

        ctx = AgentContext(session_id=session_id)
        state = checkpoint.get("state", {})
        ctx.messages = state.get("messages", [])
        ctx.iteration_count = state.get("iteration_count", 0)
        ctx.current_node_id = checkpoint.get("node_id", graph.start_node_id)
        self._tool_call_chain_count = state.get("tool_call_chain_count", 0)

        saved_vars = state.get("variables", {})
        for k, v in saved_vars.items():
            self.variables.set(k, v)

        logger.info(
            "Resumed from checkpoint: graph=%s node=%s iteration=%d",
            graph.name,
            ctx.current_node_id,
            ctx.iteration_count,
        )

        yield AgentEvent(
            type=AgentEventType.CHECKPOINT,
            content=f"Resumed from checkpoint at node '{ctx.current_node_id}'",
            metadata={"checkpoint": checkpoint},
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
        messages = []

        node_prompt = node.system_prompt or system_prompt
        if node_prompt:
            template_name = self._extract_template_name(node_prompt)
            if template_name:
                try:
                    node_prompt = self.templates.render(
                        template_name, **self.variables.to_dict()
                    )
                except KeyError:
                    pass
            node_prompt = self.variables.interpolate(node_prompt)
            messages.append({"role": "system", "content": node_prompt})

        if self.artifact_manager and hasattr(ctx, "agent_id"):
            # WF-1: anti-forgetting turn counter
            ctx.artifact_turn_count += 1
            if ctx.artifact_turn_count % 5 == 0 and ctx.artifact_turn_count > 0:
                summary = self.artifact_manager.get_active_artifacts_context(
                    ctx.agent_id, limit=10
                )
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

            context_window = 32768
            if node.model:
                pass
            context_window = getattr(self, "_context_window_override", 32768)
            artifact_result = (
                self.artifact_manager.get_active_artifacts_context_budget_aware(
                    ctx.agent_id,
                    context_window=context_window,
                )
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
                        artifact_count, artifact_mode,
                    )
                except (KeyError, ValueError) as e:
                    logger.warning("artifact-long-text template render failed: %s", e)

            if self.compactor and artifact_result.get("utilization", 0.0) > 0.7:
                compact_level = self.compactor.should_compact(ctx.messages)
                if compact_level != "none":
                    before_count = len(ctx.messages)
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
                            "artifact_utilization": artifact_result.get(
                                "utilization", 0.0
                            ),
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
                messages.insert(
                    0, {"role": "system", "content": artifact_system_suffix.strip()}
                )

        messages.extend(ctx.messages)

        schema_validator = None
        if node.tool_params.get("output_schema"):
            schema_validator = JsonSchemaValidator(node.tool_params["output_schema"])
            schema_instruction = schema_validator.to_instruction()
            if schema_instruction:
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += "\n\n" + schema_instruction
                else:
                    messages.insert(
                        0, {"role": "system", "content": schema_instruction}
                    )

        if self.safety_gateway:
            safety_result = self.safety_gateway.evaluate_action(
                category="llm_call",
                content=str(messages[-1]) if messages else "",
                context=f"model={model} node={node.label}",
            )
            if (
                safety_result.action.value == "block"
                and not safety_result.requires_approval
            ):
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

                    if finish_reason:
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        if chunk.get("model"):
                            resp_model = chunk["model"]

                content = "".join(content_parts)
                tool_calls = (
                    list(current_tool_calls.values()) if current_tool_calls else []
                )
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
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
            return

        if schema_validator and content:
            extracted = schema_validator.extract_from_text(content)
            if extracted:
                errors = schema_validator.validate(extracted)
                if not errors:
                    content = json.dumps(extracted, ensure_ascii=False)
                    self.variables.set("structured_output", extracted)
                else:
                    logger.warning(
                        "Structured output schema validation failed: %s", errors
                    )
            else:
                logger.warning(
                    "Structured output: could not extract JSON from LLM response"
                )

        ctx.add_message("assistant", content, tool_calls=tool_calls or None)

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
                    unclosed, bp[:80],
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
            self._tool_call_chain_count += 1
            if self._tool_call_chain_count > _MAX_TOOL_CALL_CHAIN:
                ctx.error = f"Tool call chain exceeded {_MAX_TOOL_CALL_CHAIN}"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            tool_errors: list[str] = []

            for tc in tool_calls:
                try:
                    func_name = tc["function"]["name"]
                    func_args = json.loads(tc["function"]["arguments"])
                except (KeyError, json.JSONDecodeError) as e:
                    ctx.error = f"Invalid tool call: {e}"
                    yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                    return

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
                        "tool blocked by hook tool=%s reason=%s", func_name, pre.reason
                    )
                    ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        content=result,
                        name=func_name,
                        node_id=node.label,
                    )
                    continue

                try:
                    tool = self.tools.get(func_name)
                    if tool is None:
                        raise KeyError(func_name)
                    validated_args = self._validate_tool_args(tool, func_args)
                    result = await tool.execute(**validated_args)
                except KeyError:
                    result = f"Error: Tool '{func_name}' not found"
                except Exception as e:
                    result = f"Error: {e}"

                ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                ctx.messages[-1]["_node_id"] = node.label

                post_event = (
                    "POST_TOOL_USE_FAILURE"
                    if str(result).startswith("Error:")
                    else "POST_TOOL_USE"
                )
                await self._fire_tool_hooks(
                    post_event, func_name, func_args, str(result)
                )

                yield AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    content=str(result),
                    name=func_name,
                    node_id=node.label,
                )

                if str(result).startswith("Error:"):
                    tool_errors.append(f"{func_name}: {result}")

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

                    try:
                        gw_resp = await asyncio.wait_for(
                            self.llm_gateway.chat(
                                messages=messages
                                + ctx.messages[-_MAX_RETRY_CONTEXT_MESSAGES:],
                                model=model,
                                capability=node.tool_params.get("capability", ""),
                                tools=tools_schema if tools_schema else None,
                                max_tokens=node.max_tokens,
                                temperature=node.temperature,
                                effort=node.effort or None,
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
                    ctx.add_message(
                        "assistant", retry_content, tool_calls=retry_tool_calls or None
                    )

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
            logger.warning(
                "hook fire error event=%s tool=%s err=%s", event, tool_name, e
            )
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
                    logger.warning(
                        "Tool '%s': could not coerce arg '%s' to number", tool.name, key
                    )
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
            tool.openai_schema()
            .get("function", {})
            .get("parameters", {})
            .get("required", [])
        ):
            if req_key not in validated:
                logger.warning(
                    "Tool '%s': missing required arg '%s'", tool.name, req_key
                )
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

        params = {}
        for k, v in node.tool_params.items():
            if isinstance(v, str):
                params[k] = self.variables.interpolate(v)
            else:
                params[k] = v

        if self.safety_gateway:
            safety_result = self.safety_gateway.evaluate_action(
                category="tool_call",
                content=f"{node.tool_name}({params})",
                context=f"tool={node.tool_name} node={node.label}",
            )
            if (
                safety_result.action.value == "block"
                and not safety_result.requires_approval
            ):
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
            result = await tool.execute(**params)
        except KeyError:
            result = f"Error: Tool '{node.tool_name}' not found"
        except Exception as e:
            result = f"Error: {e}"

        ctx.add_message("tool", str(result), tool_call_id=f"tool_{node.tool_name}")
        ctx.messages[-1]["_node_id"] = node.label

        output_mapping = node.tool_params.get("output_mapping", {})
        if output_mapping:
            self._apply_tool_output_mapping(output_mapping, result, node.label)

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=str(result),
            name=node.tool_name,
            node_id=node.label,
        )

    def _apply_tool_output_mapping(
        self, output_mapping: dict, result: Any, node_label: str
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
                self.variables.set(target_var, result)
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
                self.variables.set(target_var, parsed[source_key])
            else:
                self.variables.set(target_var, result)

    def _execute_condition_node(
        self, ctx: AgentContext, node: NodeConfig
    ) -> AgentEvent:
        """Evaluate a condition node using the condition engine."""
        expr = self.variables.interpolate(node.condition_expr)
        try:
            result = self.condition_engine.evaluate(expr, ctx, self.variables)
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
        max_iter = node.max_iterations
        loop_var = node.tool_params.get("loop_var", "loop_count")
        current = self.variables.get(loop_var, 0)
        try:
            current = int(current)
        except (ValueError, TypeError):
            current = ctx.iteration_count

        if current < max_iter:
            self.variables.set(loop_var, current + 1)
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
            logger.warning(
                "Retry %d/%d still has error: %s", attempt, max_retries, ctx.error
            )

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Error handler completed after {attempt} attempt(s)",
            node_id=node.label,
        )

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

        if not graph_json:
            yield AgentEvent(
                type=AgentEventType.ERROR, content="Sub-graph: no graph_json provided"
            )
            return

        try:
            sub_graph = AgentGraph.from_json(graph_json)
        except Exception as e:
            yield AgentEvent(
                type=AgentEventType.ERROR, content=f"Sub-graph parse error: {e}"
            )
            return

        sub_input = ""
        for parent_var, sub_var in input_mapping.items():
            val = self.variables.get(parent_var, "")
            if sub_var == "input":
                sub_input = str(val)
            else:
                self.variables.set(sub_var, val)

        sub_vars = VariableManager()
        sub_vars.load_from(self.variables.to_dict())

        sub_runtime = AgentRuntime(
            tool_registry=self.tools,
            max_iterations=self.max_iterations,
            variables=sub_vars,
            llm_gateway=self.llm_gateway,
        )

        sub_ctx = AgentContext()
        async for event in sub_runtime.execute_graph(sub_graph, sub_input, sub_ctx):
            yield AgentEvent(
                type=event.type,
                content=f"[sub:{sub_graph.name}] {event.content}",
                name=event.name,
                args=event.args,
                node_id=event.node_id,
                metadata=event.metadata,
            )

        for sub_var, parent_var in output_mapping.items():
            val = sub_vars.get(sub_var, "")
            self.variables.set(parent_var, val)

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
            yield AgentEvent(
                type=AgentEventType.ERROR, content="RAG pipeline not available"
            )
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

        pipeline = RAGPipeline(
            knowledge_engine=knowledge_engine, gateway=self.llm_gateway
        )

        try:
            rag_result = pipeline.retrieve(query, config=rag_config)
        except Exception as e:
            logger.warning("RAG retrieve failed: %s", e)
            rag_result = None

        if rag_result and rag_result.context_text:
            context_block = f"\n\n[Retrieved Context]\n{rag_result.context_text}\n[/Retrieved Context]\n\n"
            node_prompt = node.system_prompt or system_prompt or ""
            if node_prompt:
                node_prompt = self.variables.interpolate(node_prompt)
                node_prompt += context_block
            else:
                node_prompt = f"Use the following context to answer the user's question.{context_block}"

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
            yield AgentEvent(
                type=AgentEventType.ERROR, content="Planner engine not available"
            )
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

        self.variables.set("current_plan_id", plan.id)
        self.variables.set("plan_step_count", len(plan.steps))
        self.variables.set("plan_risk", plan.overall_risk)

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

    async def _execute_verify_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        try:
            from .verifier import VerificationEngine
        except ImportError:
            yield AgentEvent(
                type=AgentEventType.ERROR, content="Verification engine not available"
            )
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

        self.variables.set("verify_passed", result.passed)
        self.variables.set("verify_score", result.score)
        self.variables.set("verify_attempt", result.attempt)

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
        user_msgs = [
            m.get("content", "") for m in ctx.messages if m.get("role") == "user"
        ]
        assistant_msgs = [
            m.get("content", "") for m in ctx.messages if m.get("role") == "assistant"
        ]
        if not user_msgs and not assistant_msgs:
            return
        last_user = user_msgs[-1] if user_msgs else ""
        last_assistant = assistant_msgs[-1] if assistant_msgs else ""
        scope = f"graph:{graph.name}"
        await asyncio.to_thread(
            self.memory_engine.store,
            content=f"Q: {last_user[:200]} A: {last_assistant[:500]}",
            scope=scope,
            tags="auto-store",
            importance=7 if not ctx.error else 3,
            metadata={
                "graph_id": graph.id,
                "error": ctx.error,
                "iterations": ctx.iteration_count,
            },
        )
        logger.info("Auto-stored execution result to memory (scope=%s)", scope)

    def set_knowledge_engine(self, engine: Any) -> None:
        if (
            hasattr(engine, "embedding_fn")
            and engine.embedding_fn is None
            and self.llm_gateway
        ):
            import asyncio
            import concurrent.futures

            def _sync_embed(text: str) -> list[float]:
                try:
                    asyncio.get_running_loop()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        return pool.submit(
                            asyncio.run, self.llm_gateway.aembed(text)
                        ).result()
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
