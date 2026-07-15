"""Agent Runtime Engine — state machine that drives the agent execution loop.

The runtime coordinates the LLM → tool → observe → decide cycle,
calling fusion-mlx via HTTP API for LLM inference and executing tools
locally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, AsyncIterator

from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph, NodeConfig

if TYPE_CHECKING:
    from server.fusion_mlx_client import FusionMLXClient
    from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Agent runtime engine — state machine driving the agent execution loop."""

    def __init__(
        self,
        mlx_client: "FusionMLXClient",
        tool_registry: "ToolRegistry",
        max_iterations: int = 25,
    ):
        self.mlx = mlx_client
        self.tools = tool_registry
        self.max_iterations = max_iterations

    async def execute_graph(
        self,
        graph: AgentGraph,
        initial_input: str = "",
        context: AgentContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a complete agent graph, yielding events as they occur.

        Args:
            graph: The agent workflow graph to execute.
            initial_input: The initial user input to start the agent.
            context: Optional existing context to resume from.

        Yields:
            AgentEvent events for each step of execution.
        """
        ctx = context or AgentContext()
        ctx.started_at = time.time()
        ctx.max_iterations = self.max_iterations

        # Validate graph
        errors = graph.validate()
        if errors:
            ctx.error = "; ".join(errors)
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        yield AgentEvent(type=AgentEventType.START, content=f"Starting: {graph.name}")

        # Build initial messages
        ctx.add_message("user", initial_input)

        # Get system prompt from start node
        start_node = graph.get_node(graph.start_node_id)
        system_prompt = ""
        if start_node:
            system_prompt = start_node.system_prompt

        # Check if graph has LLM nodes that need a model
        has_llm_nodes = any(n.type == "llm" for n in graph.nodes.values())
        model = graph.find_llm_model()
        if has_llm_nodes and not model:
            ctx.error = "No LLM model configured in graph"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        # Get tools schema
        tools_schema = self.tools.to_openai_schemas()

        # Main execution loop
        current_node_id = graph.start_node_id
        ctx.current_node_id = current_node_id

        while current_node_id and not ctx.is_max_iterations_reached():
            node = graph.get_node(current_node_id)
            if not node:
                ctx.error = f"Node '{current_node_id}' not found"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            ctx.iteration_count += 1

            if node.type == "start":
                # Start node: just pass through
                next_id = graph.get_next_node(current_node_id)
                current_node_id = next_id or ""
                continue

            elif node.type == "llm":
                async for event in self._execute_llm_node(
                    ctx, node, model, tools_schema, system_prompt
                ):
                    yield event
                    if event.type == AgentEventType.ERROR:
                        return

                # After LLM, check if we need to continue (tool calls) or end
                last_msg = ctx.messages[-1] if ctx.messages else {}
                if last_msg.get("tool_calls"):
                    # Tool calls were made — stay on this node for next iteration
                    pass
                else:
                    next_id = graph.get_next_node(current_node_id)
                    current_node_id = next_id or ""

            elif node.type == "tool":
                async for event in self._execute_tool_node(ctx, node):
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
                event = self._execute_loop_node(ctx, node)
                yield event
                next_id = graph.get_next_node(current_node_id)
                current_node_id = next_id or ""

            elif node.type == "error_handler":
                async for event in self._execute_error_handler_node(ctx, node):
                    yield event
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
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        """Execute an LLM node — call fusion-mlx via HTTP API."""
        # Build messages with system prompt
        messages = []
        if system_prompt or node.system_prompt:
            messages.append({
                "role": "system",
                "content": node.system_prompt or system_prompt,
            })
        messages.extend(ctx.messages)

        try:
            response = await asyncio.wait_for(
                self.mlx.chat(
                    model=model,
                    messages=messages,
                    tools=tools_schema if tools_schema else None,
                    temperature=node.temperature,
                    max_tokens=node.max_tokens,
                ),
                timeout=120.0,  # 2-minute timeout per LLM call
            )
        except Exception as e:
            ctx.error = f"LLM call failed: {e}"
            yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
            return

        # Add assistant message to context
        ctx.add_message("assistant", response.content, tool_calls=response.tool_calls or None)

        # Track token usage
        if response.usage.get("prompt_tokens") or response.usage.get("completion_tokens"):
            if ctx.messages:
                last_msg = ctx.messages[-1]
                if isinstance(last_msg, dict):
                    last_msg["usage"] = response.usage

        # Emit think event
        yield AgentEvent(
            type=AgentEventType.THINK,
            content=response.content,
            node_id=node.label,
            metadata={
                "model": model,
                "temperature": node.temperature,
                "usage": response.usage,
            },
        )

        # Emit tool call events
        if response.tool_calls:
            for tc in response.tool_calls:
                try:
                    func_name = tc["function"]["name"]
                    func_args = json.loads(tc["function"]["arguments"])
                except (KeyError, json.JSONDecodeError) as e:
                    ctx.error = f"Invalid tool call: {e}"
                    yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                    return

                yield AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    name=func_name,
                    args=func_args,
                    node_id=node.label,
                )

                # Execute the tool
                try:
                    tool = self.tools.get(func_name)
                    result = await tool.execute(**func_args)
                except KeyError:
                    result = f"Error: Tool '{func_name}' not found"
                except Exception as e:
                    result = f"Error: {e}"

                ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))

                yield AgentEvent(
                    type=AgentEventType.TOOL_RESULT,
                    content=str(result),
                    name=func_name,
                    node_id=node.label,
                )

    async def _execute_tool_node(
        self, ctx: AgentContext, node: NodeConfig
    ) -> AsyncIterator[AgentEvent]:
        """Execute a standalone tool node."""
        try:
            tool = self.tools.get(node.tool_name)
            result = await tool.execute(**node.tool_params)
        except KeyError:
            result = f"Error: Tool '{node.tool_name}' not found"
        except Exception as e:
            result = f"Error: {e}"

        ctx.add_message("tool", str(result), tool_call_id=f"tool_{node.tool_name}")

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=str(result),
            name=node.tool_name,
            node_id=node.label,
        )

    def _execute_condition_node(
        self, ctx: AgentContext, node: NodeConfig
    ) -> AgentEvent:
        """Evaluate a condition node."""
        try:
            # Simple condition evaluation
            result = self._evaluate_condition(node.condition_expr, ctx)
        except Exception as e:
            result = "false"

        return AgentEvent(
            type=AgentEventType.THINK,
            content=result,
            node_id=node.label,
            metadata={"condition": node.condition_expr, "result": result},
        )

    def _evaluate_condition(self, expr: str, ctx: AgentContext) -> str:
        """Evaluate a simple condition expression against context."""
        expr = expr.strip().lower()
        if expr == "true":
            return "true"
        if expr == "false":
            return "false"

        # Check for common patterns
        if "has_tool_calls" in expr:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("tool_calls"):
                    return "true"
            return "false"

        if "has_error" in expr:
            return "true" if ctx.error else "false"

        if "iteration" in expr:
            # Check iteration count conditions
            parts = expr.split()
            for i, part in enumerate(parts):
                if part == "iteration" and i + 2 < len(parts):
                    try:
                        threshold = int(parts[i + 2])
                        if parts[i + 1] == ">=":
                            return "true" if ctx.iteration_count >= threshold else "false"
                        if parts[i + 1] == "<=":
                            return "true" if ctx.iteration_count <= threshold else "false"
                        if parts[i + 1] == ">":
                            return "true" if ctx.iteration_count > threshold else "false"
                        if parts[i + 1] == "<":
                            return "true" if ctx.iteration_count < threshold else "false"
                        if parts[i + 1] == "==":
                            return "true" if ctx.iteration_count == threshold else "false"
                    except (ValueError, IndexError):
                        pass

        return "false"

    def _execute_loop_node(
        self, ctx: AgentContext, node: NodeConfig
    ) -> AgentEvent:
        """Execute a loop node — check if loop should continue."""
        return AgentEvent(
            type=AgentEventType.THINK,
            content=f"Loop iteration {ctx.iteration_count}/{node.max_iterations}",
            node_id=node.label,
            metadata={"max_iterations": node.max_iterations, "current": ctx.iteration_count},
        )

    async def _execute_error_handler_node(
        self, ctx: AgentContext, node: NodeConfig
    ) -> AsyncIterator[AgentEvent]:
        """Execute an error handler node — retry the last failed operation."""
        max_retries = node.max_retries
        retry_delay = node.retry_delay

        for attempt in range(1, max_retries + 1):
            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"Retry attempt {attempt}/{max_retries}",
                node_id=node.label,
                metadata={"attempt": attempt, "max_retries": max_retries},
            )
            if attempt < max_retries:
                import asyncio
                await asyncio.sleep(retry_delay)

        ctx.error = ""  # Clear error after retries
        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Error handler completed after {max_retries} retries",
            node_id=node.label,
        )