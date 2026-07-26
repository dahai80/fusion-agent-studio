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

from .context import AgentContext, AgentEvent, AgentEventType
from .debugger import StepDebugger
from .graph import AgentGraph, NodeConfig
from .json_schema import JsonSchemaValidator
from .llm_gateway import LLMGateway
from .prompt_templates import PromptTemplateManager
from .sub_graph import SubGraphRegistry
from .variable_manager import VariableManager

if TYPE_CHECKING:
    from tools.base import BaseTool
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_TOOL_CALL_CHAIN = 10


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
            return "true" if any(self.evaluate(p, ctx, variables) == "true" for p in parts) else "false"

        if re.search(r"\band\b", expr_lower):
            parts = re.split(r"\s+and\s+", expr, flags=re.IGNORECASE)
            return "true" if all(self.evaluate(p, ctx, variables) == "true" for p in parts) else "false"

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
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        self.max_iterations = max_iterations
        self.debugger = debugger
        self.variables = variables or VariableManager()
        self.templates = templates or PromptTemplateManager()
        self.sub_graphs = sub_graphs or SubGraphRegistry()
        self.condition_engine = condition_engine or ConditionEngine()
        self._tool_call_chain_count = 0

        if llm_gateway:
            self.llm_gateway = llm_gateway
        elif mlx_client:
            gw = LLMGateway()
            gw.set_default_client(mlx_client)
            self.llm_gateway = gw
        else:
            self.llm_gateway = LLMGateway()

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
    ) -> AsyncIterator[AgentEvent]:
        """Execute a complete agent graph, yielding events as they occur."""
        ctx = context or AgentContext()
        ctx.started_at = time.time()
        ctx.max_iterations = self.max_iterations
        self._tool_call_chain_count = 0

        errors = graph.validate()
        if errors:
            ctx.error = "; ".join(errors)
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(type=AgentEventType.START, content=f"Starting: {graph.name}")

        initial_input = self.variables.interpolate(initial_input)
        ctx.add_message("user", initial_input)

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
                await self.debugger.check_pause(current_node_id, self.variables.to_dict())

            if node.type == "start":
                next_id = graph.get_next_node(current_node_id)
                current_node_id = next_id or ""
                continue

            elif node.type == "llm":
                async for event in self._execute_llm_node(
                    ctx, node, graph, model, tools_schema, system_prompt
                ):
                    yield event
                    if event.type == AgentEventType.ERROR:
                        return

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
                next_id = graph.get_next_node(current_node_id)
                current_node_id = next_id or ""

            elif node.type == "condition":
                event = self._execute_condition_node(ctx, node)
                yield event
                current_node_id = graph.get_next_node(
                    current_node_id, condition_result=event.content
                ) or ""

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
                async for event in self._execute_rag_node(ctx, node, graph, model, tools_schema, system_prompt):
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

            elif node.type == "end":
                yield AgentEvent(type=AgentEventType.END, content="Graph execution complete")
                ctx.finished_at = time.time()
                return

        if ctx.is_max_iterations_reached():
            ctx.error = "Max iterations exceeded"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)

        ctx.finished_at = time.time()

    async def _execute_llm_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        """Execute an LLM node — call fusion-mlx via HTTP API."""
        messages = []

        node_prompt = node.system_prompt or system_prompt
        if node_prompt:
            template_name = self._extract_template_name(node_prompt)
            if template_name:
                try:
                    node_prompt = self.templates.render(template_name, **self.variables.to_dict())
                except KeyError:
                    pass
            node_prompt = self.variables.interpolate(node_prompt)
            messages.append({"role": "system", "content": node_prompt})

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

        try:
            logger.debug("LLM call via gateway, model=%s", model)
            capability = node.tool_params.get("capability", "")
            gw_resp = await asyncio.wait_for(
                self.llm_gateway.chat(
                    messages=messages,
                    model=model,
                    capability=capability,
                    tools=tools_schema if tools_schema else None,
                    max_tokens=node.max_tokens,
                    temperature=node.temperature,
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
                    logger.warning("Structured output schema validation failed: %s", errors)
            else:
                logger.warning("Structured output: could not extract JSON from LLM response")

        ctx.add_message("assistant", content, tool_calls=tool_calls or None)

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

                yield AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    name=func_name,
                    args=func_args,
                    node_id=node.label,
                )

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

                yield AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    content=str(result),
                    name=func_name,
                    node_id=node.label,
                )

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
                    logger.warning("Tool '%s': could not coerce arg '%s' to integer", tool.name, key)
            elif expected_type == "boolean" and not isinstance(value, bool):
                validated[key] = bool(value)
                logger.warning("Tool '%s': coerced arg '%s' to boolean", tool.name, key)
            else:
                validated[key] = value
        for req_key in (tool.openai_schema().get("function", {}).get("parameters", {}).get("required", [])):
            if req_key not in validated:
                logger.warning("Tool '%s': missing required arg '%s'", tool.name, req_key)
        return validated

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

        try:
            tool = self.tools.get(node.tool_name)
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
                metadata={"attempt": attempt, "max_retries": max_retries, "error": error_msg},
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
            yield AgentEvent(type=AgentEventType.ERROR, content="Sub-graph: no graph_json provided")
            return

        try:
            sub_graph = AgentGraph.from_json(graph_json)
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=f"Sub-graph parse error: {e}")
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
                m.get("content", "") for m in ctx.messages
                if isinstance(m, dict) and m.get("role") == "user"
            )[:500]

        rag_config_dict = node.tool_params.get("rag_config", {})
        rag_config = RAGConfig(**{k: v for k, v in rag_config_dict.items()
                                   if k in ("top_k", "similarity_threshold", "rerank", "mode", "scope", "max_context_tokens")})

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
                ctx, augmented_node, graph, augmented_node.model or model, tools_schema, ""
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

        self.variables.set("current_plan_id", plan.id)
        self.variables.set("plan_step_count", len(plan.steps))
        self.variables.set("plan_risk", plan.overall_risk)

        step_summaries = []
        for step in plan.steps:
            step_summaries.append({
                "id": step.id,
                "description": step.description,
                "action": step.action,
                "target_files": step.target_files,
                "complexity": step.estimated_complexity,
            })

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

    def set_knowledge_engine(self, engine: Any) -> None:
        """Set the knowledge engine for RAG nodes."""
        self._knowledge_engine = engine
        logger.info("Knowledge engine set on runtime")
