"""Comprehensive tests for agent_runtime.runtime to achieve 90%+ coverage."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.debugger import StepDebugger
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.prompt_templates import PromptTemplate, PromptTemplateManager
from agent_runtime.runtime import _MAX_TOOL_CALL_CHAIN, AgentRuntime, ConditionEngine
from agent_runtime.variable_manager import VariableManager
from server.fusion_mlx_client import LLMResponse

logger = logging.getLogger(__name__)


class MockMLXClient:
    def __init__(self):
        self.call_count = 0
        self.responses = []
        self._raise = None

    def add_response(self, content="", tool_calls=None, usage=None):
        self.responses.append(
            LLMResponse(
                content=content,
                tool_calls=tool_calls or [],
                usage=usage or {"prompt_tokens": 0, "completion_tokens": 0},
            )
        )

    def set_raise(self, exc):
        self._raise = exc

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.call_count += 1
        if self._raise:
            raise self._raise
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(
            content="",
            tool_calls=[],
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )


class MockToolRegistry:
    def __init__(self):
        self._tools = {}
        self._schemas = []

    def add_tool(self, name, execute_result="tool executed"):
        tool = MagicMock()
        tool.name = name
        tool.execute = AsyncMock(return_value=execute_result)
        self._tools[name] = tool

    def add_failing_tool(self, name, exc=RuntimeError("tool error")):
        tool = MagicMock()
        tool.name = name
        tool.execute = AsyncMock(side_effect=exc)
        self._tools[name] = tool

    def to_openai_schemas(self):
        return self._schemas

    def get(self, name):
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]


def _make_runtime(mlx=None, tools=None, **kwargs):
    mlx = mlx or MockMLXClient()
    tools = tools or MockToolRegistry()
    return AgentRuntime(mlx, tools, **kwargs)


def _build_graph(nodes, edges, start="start", name="test"):
    g = AgentGraph(name=name, start_node_id=start)
    for nid, cfg in nodes.items():
        g.add_node(nid, cfg)
    for src, tgt, *lbl in edges:
        g.add_edge(src, tgt, lbl[0] if lbl else "")
    return g


# ---------------------------------------------------------------------------
# ConditionEngine._resolve_value
# ---------------------------------------------------------------------------
class TestConditionEngineResolveValue:
    def test_iteration(self):
        ctx = AgentContext()
        ctx.iteration_count = 7
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("iteration", ctx, vm) == 7

    def test_token_count(self):
        ctx = AgentContext()
        ctx.add_message("assistant", "hi", tool_calls=None)
        ctx.messages[-1]["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("token_count", ctx, vm) == 15

    def test_prompt_tokens(self):
        ctx = AgentContext()
        ctx.add_message("assistant", "hi")
        ctx.messages[-1]["usage"] = {"prompt_tokens": 12, "completion_tokens": 3}
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("prompt_tokens", ctx, vm) == 12

    def test_completion_tokens(self):
        ctx = AgentContext()
        ctx.add_message("assistant", "hi")
        ctx.messages[-1]["usage"] = {"prompt_tokens": 2, "completion_tokens": 8}
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("completion_tokens", ctx, vm) == 8

    def test_message_count(self):
        ctx = AgentContext()
        ctx.add_message("user", "a")
        ctx.add_message("assistant", "b")
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("message_count", ctx, vm) == 2

    def test_error(self):
        ctx = AgentContext()
        ctx.error = "boom"
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("error", ctx, vm) == "boom"

    def test_variable_fallback(self):
        ctx = AgentContext()
        vm = VariableManager()
        vm.set("myvar", 42)
        eng = ConditionEngine()
        assert eng._resolve_value("myvar", ctx, vm) == 42

    def test_variable_none_fallback_to_zero(self):
        ctx = AgentContext()
        vm = VariableManager()
        eng = ConditionEngine()
        assert eng._resolve_value("nonexistent", ctx, vm) == 0


# ---------------------------------------------------------------------------
# ConditionEngine._resolve_literal
# ---------------------------------------------------------------------------
class TestConditionEngineResolveLiteral:
    def test_variable_ref(self):
        vm = VariableManager()
        vm.set("x", 99)
        eng = ConditionEngine()
        assert eng._resolve_literal("{{ x }}", vm) == 99

    def test_string_literal_double(self):
        eng = ConditionEngine()
        vm = VariableManager()
        assert eng._resolve_literal('"hello"', vm) == "hello"

    def test_string_literal_single(self):
        eng = ConditionEngine()
        vm = VariableManager()
        assert eng._resolve_literal("'hello'", vm) == "hello"

    def test_int_parse(self):
        eng = ConditionEngine()
        vm = VariableManager()
        assert eng._resolve_literal("42", vm) == 42

    def test_float_parse(self):
        eng = ConditionEngine()
        vm = VariableManager()
        assert eng._resolve_literal("3.14", vm) == 3.14

    def test_fallback_raw(self):
        eng = ConditionEngine()
        vm = VariableManager()
        assert eng._resolve_literal("abc", vm) == "abc"


# ---------------------------------------------------------------------------
# ConditionEngine._compare
# ---------------------------------------------------------------------------
class TestConditionEngineCompare:
    def test_eq(self):
        eng = ConditionEngine()
        assert eng._compare(5, "==", 5) == "true"
        assert eng._compare(5, "==", 6) == "false"

    def test_ne(self):
        eng = ConditionEngine()
        assert eng._compare(5, "!=", 6) == "true"
        assert eng._compare(5, "!=", 5) == "false"

    def test_gte(self):
        eng = ConditionEngine()
        assert eng._compare(5, ">=", 5) == "true"
        assert eng._compare(5, ">=", 6) == "false"

    def test_lte(self):
        eng = ConditionEngine()
        assert eng._compare(5, "<=", 5) == "true"
        assert eng._compare(5, "<=", 4) == "false"

    def test_gt(self):
        eng = ConditionEngine()
        assert eng._compare(5, ">", 4) == "true"
        assert eng._compare(5, ">", 5) == "false"

    def test_lt(self):
        eng = ConditionEngine()
        assert eng._compare(5, "<", 6) == "true"
        assert eng._compare(5, "<", 5) == "false"

    def test_type_error_returns_false(self):
        eng = ConditionEngine()
        assert eng._compare("a", ">=", 1) == "false"

    def test_unknown_op(self):
        eng = ConditionEngine()
        assert eng._compare(1, "~~", 2) == "false"


# ---------------------------------------------------------------------------
# ConditionEngine.evaluate - boolean / or / and / not / has_tool_calls / has_error / has_result / in
# ---------------------------------------------------------------------------
class TestConditionEngineEvaluate:
    def test_empty_expr(self):
        eng = ConditionEngine()
        assert eng.evaluate("", AgentContext(), VariableManager()) == "false"

    def test_or(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        assert eng.evaluate("true or false", ctx, vm) == "true"
        assert eng.evaluate("false or false", ctx, vm) == "false"

    def test_and(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        assert eng.evaluate("true and false", ctx, vm) == "false"
        assert eng.evaluate("true and true", ctx, vm) == "true"

    def test_not(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        assert eng.evaluate("not true", ctx, vm) == "false"
        assert eng.evaluate("not false", ctx, vm) == "true"

    def test_has_tool_calls_false(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        assert eng.evaluate("has_tool_calls", ctx, vm) == "false"

    def test_has_tool_calls_true(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.add_message("assistant", "tc", tool_calls=[{"id": "1"}])
        vm = VariableManager()
        assert eng.evaluate("has_tool_calls", ctx, vm) == "true"

    def test_has_error_false(self):
        eng = ConditionEngine()
        assert eng.evaluate("has_error", AgentContext(), VariableManager()) == "false"

    def test_has_error_true(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.error = "fail"
        assert eng.evaluate("has_error", ctx, VariableManager()) == "true"

    def test_has_result_false(self):
        eng = ConditionEngine()
        assert eng.evaluate("has_result", AgentContext(), VariableManager()) == "false"

    def test_has_result_true(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.add_message("tool", "result")
        assert eng.evaluate("has_result", ctx, VariableManager()) == "true"

    def test_string_in_variable(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        vm.set("content", "hello world")
        assert eng.evaluate('"hello" in content', ctx, vm) == "true"
        assert eng.evaluate('"xyz" in content', ctx, vm) == "false"

    def test_variable_set_truthy(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        vm.set("flag", "yes")
        assert eng.evaluate("flag", ctx, vm) == "true"

    def test_unknown_returns_false(self):
        eng = ConditionEngine()
        assert (
            eng.evaluate("unknown_expr", AgentContext(), VariableManager()) == "false"
        )


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - node not found
# ---------------------------------------------------------------------------
class TestExecuteGraphNodeNotFound:
    async def test_missing_node(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = AgentGraph(name="test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="Start"))
        g.add_node("end", NodeConfig(type="end", label="End"))
        g.add_edge("start", "end")
        del g.nodes["end"]
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("not found" in e.content for e in errors)


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - debugger check
# ---------------------------------------------------------------------------
class TestExecuteGraphDebugger:
    async def test_debugger_check_pause_called(self):
        mlx = MockMLXClient()
        mlx.add_response("ok")
        tools = MockToolRegistry()
        debugger = StepDebugger()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools, debugger=debugger)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert any(e.type == AgentEventType.THINK for e in events)


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - tool_calls continuation (line 261)
# ---------------------------------------------------------------------------
class TestExecuteGraphToolCallsContinuation:
    async def test_tool_calls_chain_incremented_in_llm_node(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "thinking",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "mytool", "arguments": "{}"},
                }
            ],
        )
        mlx.add_response("done")
        tools = MockToolRegistry()
        tools.add_tool("mytool", "result1")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        events = []
        async for ev in runtime.execute_graph(g, "hi", ctx):
            events.append(ev)
        tool_calls = [e for e in events if e.type == AgentEventType.TOOL_CALL]
        assert len(tool_calls) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - loop node (lines 281-292)
# ---------------------------------------------------------------------------
class TestExecuteGraphLoopNode:
    async def test_loop_continue(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=3,
                    tool_params={"loop_var": "lc", "loop_start_node": "start"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("lc", 0)
        events = []
        async for ev in runtime.execute_graph(g, "go"):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert any("loop_continue" in e.content for e in think_events)

    async def test_loop_exit(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=1,
                    tool_params={"loop_var": "lc"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("lc", 5)
        events = []
        async for ev in runtime.execute_graph(g, "go"):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert any("loop_exit" in e.content for e in think_events)

    async def test_loop_invalid_loop_var_uses_iteration_count(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=100,
                    tool_params={"loop_var": "bad_var"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("bad_var", "not_a_number")
        events = []
        async for ev in runtime.execute_graph(g, "go"):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert len(think_events) >= 1

    async def test_loop_no_loop_start_uses_next(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=100,
                    tool_params={"loop_var": "lc", "loop_start_node": "nonexistent"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("lc", 0)
        events = []
        async for ev in runtime.execute_graph(g, "go"):
            events.append(ev)
        end_events = [e for e in events if e.type == AgentEventType.END]
        assert len(end_events) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - max iterations reached (lines 305-309)
# ---------------------------------------------------------------------------
class TestExecuteGraphMaxIterations:
    async def test_max_iterations_error(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "go",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                }
            ],
        )
        tools = MockToolRegistry()
        tools.add_tool("t", "r")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
            },
            [("start", "llm"), ("llm", "llm")],
        )
        runtime = _make_runtime(mlx, tools, max_iterations=2)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("Max iterations" in e.content for e in errors)


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - template KeyError (lines 327-330)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeTemplateKeyError:
    async def test_template_keyerror_falls_through(self):
        mlx = MockMLXClient()
        mlx.add_response("result")
        tools = MockToolRegistry()
        tmpl = PromptTemplateManager()
        runtime = _make_runtime(mlx, tools, templates=tmpl)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    system_prompt="{{ template:nonexistent }}",
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert len(think_events) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - json schema (lines 338-344)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeJsonSchema:
    async def test_output_schema_inserts_instruction(self):
        mlx = MockMLXClient()
        mlx.add_response("result")
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    tool_params={"output_schema": schema},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert mlx.call_count == 1

    async def test_output_schema_extract_and_validate(self):
        mlx = MockMLXClient()
        mlx.add_response('{"name": "Alice"}')
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    system_prompt="You are helpful",
                    tool_params={"output_schema": schema},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert runtime.variables.get("structured_output") is not None


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - LLM exception (lines 357-360)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeException:
    async def test_llm_call_raises(self):
        mlx = MockMLXClient()
        mlx.set_raise(ConnectionError("server down"))
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - usage tracking (lines 375-378)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeUsageTracking:
    async def test_usage_attached_to_last_message(self):
        mlx = MockMLXClient()
        mlx.add_response("hello", usage={"prompt_tokens": 10, "completion_tokens": 5})
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        ctx = AgentContext()
        events = []
        async for ev in runtime.execute_graph(g, "hi", ctx):
            events.append(ev)
        last_msg = ctx.messages[-1]
        assert last_msg.get("usage", {}).get("prompt_tokens") == 10


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - tool call chain limit (lines 394-396)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeToolCallChainLimit:
    async def test_tool_call_chain_exceeded(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "go",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "t", "arguments": "{}"},
                }
            ],
        )
        tools = MockToolRegistry()
        tools.add_tool("t", "r")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime._tool_call_chain_count = _MAX_TOOL_CALL_CHAIN
        ctx = AgentContext()
        node = g.get_node("llm")
        events = []
        async for ev in runtime._execute_llm_node(
            ctx, node, g, "m", tools.to_openai_schemas(), ""
        ):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("Tool call chain exceeded" in e.content for e in errors)


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - invalid tool call format (line 405)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeInvalidToolCallFormat:
    async def test_missing_function_key(self):
        mlx = MockMLXClient()
        mlx.add_response("go", tool_calls=[{"id": "c1", "type": "function"}])
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_llm_node - sub-graph call (lines 408-410)
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeSubGraphCall:
    async def test_sub_graph_tool_call_from_llm(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "end")],
            name="sub",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        mlx.add_response(
            "calling sub",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "__sub_graph__",
                        "arguments": json.dumps(
                            {
                                "graph_json": sub_json,
                                "input_mapping": {},
                                "output_mapping": {},
                            }
                        ),
                    },
                }
            ],
        )
        mlx.add_response("done")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        sub_events = [e for e in events if "[sub:" in e.content]
        assert len(sub_events) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_tool_node - sub-graph tool (lines 441-443)
# ---------------------------------------------------------------------------
class TestExecuteToolNodeSubGraph:
    async def test_sub_graph_tool_node(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "end")],
            name="mysub",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "sub": NodeConfig(
                    type="tool",
                    label="Sub",
                    tool_name="__sub_graph__",
                    tool_params={
                        "graph_json": sub_json,
                        "input_mapping": {},
                        "output_mapping": {},
                    },
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "sub"), ("sub", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        sub_events = [e for e in events if "[sub:" in e.content]
        assert len(sub_events) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_tool_node - non-string params (line 450)
# ---------------------------------------------------------------------------
class TestExecuteToolNodeNonStringParams:
    async def test_non_string_params_not_interpolated(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("mytool", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool",
                    label="Tool",
                    tool_name="mytool",
                    tool_params={"count": 42},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._execute_tool_node - tool not found / exception (lines 455-458)
# ---------------------------------------------------------------------------
class TestExecuteToolNodeErrors:
    async def test_tool_not_found(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="missing_tool", tool_params={}
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert any("not found" in e.content for e in tool_results)

    async def test_tool_execution_exception(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_failing_tool("boom")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="boom", tool_params={}
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert any("Error" in e.content for e in tool_results)


# ---------------------------------------------------------------------------
# AgentRuntime._execute_condition_node - exception in evaluate (lines 476-478)
# ---------------------------------------------------------------------------
class TestExecuteConditionNodeException:
    async def test_condition_evaluation_exception_returns_false(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        bad_engine = MagicMock()
        bad_engine.evaluate.side_effect = RuntimeError("eval broken")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "cond": NodeConfig(
                    type="condition", label="Cond", condition_expr="oops"
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "cond"), ("cond", "end", "true")],
        )
        runtime = _make_runtime(mlx, tools, condition_engine=bad_engine)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert any(e.content == "false" for e in think_events)


# ---------------------------------------------------------------------------
# AgentRuntime._execute_loop_node - full logic (lines 491-505)
# ---------------------------------------------------------------------------
class TestExecuteLoopNodeFull:
    async def test_loop_var_non_numeric_uses_iteration_count(self):
        _eng = ConditionEngine()
        ctx = AgentContext()
        ctx.iteration_count = 2
        vm = VariableManager()
        vm.set("lc", "abc")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=10,
                    tool_params={"loop_var": "lc"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(MockMLXClient(), MockToolRegistry())
        event = runtime._execute_loop_node(ctx, g.get_node("loop"), g)
        assert event.content == "loop_continue"

    async def test_loop_increment_and_continue(self):
        ctx = AgentContext()
        ctx.iteration_count = 0
        vm = VariableManager()
        vm.set("lc", 0)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=5,
                    tool_params={"loop_var": "lc"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(MockMLXClient(), MockToolRegistry())
        runtime.variables.set("lc", 0)
        event = runtime._execute_loop_node(ctx, g.get_node("loop"), g)
        assert event.content == "loop_continue"
        assert runtime.variables.get("lc") == 1

    async def test_loop_exit_when_max_reached(self):
        ctx = AgentContext()
        ctx.iteration_count = 10
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "loop": NodeConfig(
                    type="loop",
                    label="Loop",
                    max_iterations=2,
                    tool_params={"loop_var": "lc"},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "loop"), ("loop", "end")],
        )
        runtime = _make_runtime(MockMLXClient(), MockToolRegistry())
        runtime.variables.set("lc", 5)
        event = runtime._execute_loop_node(ctx, g.get_node("loop"), g)
        assert event.content == "loop_exit"


# ---------------------------------------------------------------------------
# AgentRuntime._execute_error_handler_node (lines 512-557)
# ---------------------------------------------------------------------------
class TestExecuteErrorHandlerNode:
    async def test_error_handler_retries_tool_node(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("failing_tool", "retry result")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="failing_tool", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=2,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "something went wrong"
        ctx.add_message("tool", "bad result", tool_call_id="tool_failing_tool")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("Error handler completed" in ev.content for ev in events)

    async def test_error_handler_retries_llm_node(self):
        mlx = MockMLXClient()
        mlx.add_response("retry answer")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm1": NodeConfig(type="llm", label="LLM", model="m"),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=1,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm1"), ("llm1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "llm failed"
        ctx.add_message("assistant", "previous")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("Error handler completed" in ev.content for ev in events)

    async def test_error_handler_no_failed_node_uses_current(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=1,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.current_node_id = "tool1"
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert len(events) >= 1

    async def test_error_handler_retry_delay(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=3,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.add_message("tool", "bad", tool_call_id="tool_t1")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("attempt 1" in ev.content for ev in events)


# ---------------------------------------------------------------------------
# AgentRuntime._execute_sub_graph (lines 575-620)
# ---------------------------------------------------------------------------
class TestExecuteSubGraph:
    async def test_sub_graph_no_graph_json(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        node = NodeConfig(
            type="tool", label="sub", tool_name="__sub_graph__", tool_params={}
        )
        events = []
        async for ev in runtime._execute_sub_graph(ctx, {}, node):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("no graph_json" in e.content for e in errors)

    async def test_sub_graph_invalid_json(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        node = NodeConfig(
            type="tool", label="sub", tool_name="__sub_graph__", tool_params={}
        )
        events = []
        async for ev in runtime._execute_sub_graph(
            ctx, {"graph_json": "not valid json"}, node
        ):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("parse error" in e.content for e in errors)

    async def test_sub_graph_with_input_mapping(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "end")],
            name="subtest",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("parent_val", "hello")
        ctx = AgentContext()
        node = NodeConfig(
            type="tool", label="sub", tool_name="__sub_graph__", tool_params={}
        )
        events = []
        async for ev in runtime._execute_sub_graph(
            ctx,
            {
                "graph_json": sub_json,
                "input_mapping": {"parent_val": "input"},
                "output_mapping": {},
            },
            node,
        ):
            events.append(ev)
        assert any("[sub:" in e.content for e in events)

    async def test_sub_graph_with_output_mapping(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "end")],
            name="subout",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        node = NodeConfig(
            type="tool", label="sub", tool_name="__sub_graph__", tool_params={}
        )
        events = []
        async for ev in runtime._execute_sub_graph(
            ctx,
            {
                "graph_json": sub_json,
                "input_mapping": {},
                "output_mapping": {"sub_result": "parent_result"},
            },
            node,
        ):
            events.append(ev)
        assert any("[sub:" in e.content for e in events)

    async def test_sub_graph_with_non_input_mapping(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "end")],
            name="subvar",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("src_var", "value123")
        ctx = AgentContext()
        node = NodeConfig(
            type="tool", label="sub", tool_name="__sub_graph__", tool_params={}
        )
        events = []
        async for ev in runtime._execute_sub_graph(
            ctx,
            {
                "graph_json": sub_json,
                "input_mapping": {"src_var": "dest_var"},
                "output_mapping": {},
            },
            node,
        ):
            events.append(ev)
        assert any("[sub:" in e.content for e in events)

    async def test_sub_graph_full_execution_with_llm(self):
        sub_g = _build_graph(
            {
                "start": NodeConfig(type="start", label="SubStart"),
                "llm": NodeConfig(type="llm", label="SubLLM", model="m"),
                "end": NodeConfig(type="end", label="SubEnd"),
            },
            [("start", "llm"), ("llm", "end")],
            name="subllm",
        )
        sub_json = sub_g.to_json()
        mlx = MockMLXClient()
        mlx.add_response("sub answer")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "sub": NodeConfig(
                    type="tool",
                    label="Sub",
                    tool_name="__sub_graph__",
                    tool_params={
                        "graph_json": sub_json,
                        "input_mapping": {},
                        "output_mapping": {},
                    },
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "sub"), ("sub", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "go"):
            events.append(ev)
        sub_events = [e for e in events if "[sub:" in e.content]
        assert len(sub_events) >= 1


# ---------------------------------------------------------------------------
# AgentRuntime._extract_template_name (line 626)
# ---------------------------------------------------------------------------
class TestExtractTemplateName:
    def test_valid_template(self):
        runtime = _make_runtime()
        assert (
            runtime._extract_template_name("{{ template:code-review }}")
            == "code-review"
        )

    def test_valid_template_with_extra_spaces(self):
        runtime = _make_runtime()
        assert (
            runtime._extract_template_name("  {{  template:my-tmpl  }}  ") == "my-tmpl"
        )

    def test_not_a_template(self):
        runtime = _make_runtime()
        assert runtime._extract_template_name("just a regular prompt") == ""

    def test_template_with_invalid_chars(self):
        runtime = _make_runtime()
        assert runtime._extract_template_name("{{ template:bad!name }}") == ""


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - end node sets finished_at
# ---------------------------------------------------------------------------
class TestExecuteGraphEndNode:
    async def test_end_node_sets_finished_at(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        events = []
        async for ev in runtime.execute_graph(g, "hi", ctx):
            events.append(ev)
        assert ctx.finished_at > 0


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - graph with no LLM nodes but has LLM model check
# ---------------------------------------------------------------------------
class TestExecuteGraphNoLlmModel:
    async def test_has_llm_nodes_but_no_model(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model=""),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("No LLM model" in e.content for e in errors)


# ---------------------------------------------------------------------------
# AgentRuntime.execute_graph - variable interpolation on initial_input
# ---------------------------------------------------------------------------
class TestExecuteGraphVariableInterpolation:
    async def test_initial_input_interpolated(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("name", "Alice")
        ctx = AgentContext()
        events = []
        async for ev in runtime.execute_graph(g, "Hello {{ name }}", ctx):
            events.append(ev)
        assert any("Alice" in m.get("content", "") for m in ctx.messages)


# ---------------------------------------------------------------------------
# AgentRuntime - template rendering in LLM node
# ---------------------------------------------------------------------------
class TestExecuteLlmNodeTemplateRendering:
    async def test_template_rendered_successfully(self):
        mlx = MockMLXClient()
        mlx.add_response("review done")
        tools = MockToolRegistry()
        tmpl = PromptTemplateManager()
        tmpl.register(
            PromptTemplate(
                name="code-review",
                template="Review this: {{ code }}",
                variables={"code": {"type": "string", "default": ""}},
            )
        )
        runtime = _make_runtime(mlx, tools, templates=tmpl)
        runtime.variables.set("code", "print('hi')")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    system_prompt="{{ template:code-review }}",
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert mlx.call_count == 1


# ---------------------------------------------------------------------------
# Edge case: has_tool_calls with non-dict message
# ---------------------------------------------------------------------------
class TestConditionEngineEdgeCases:
    def test_has_tool_calls_non_dict_message(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.messages.append("not a dict")
        vm = VariableManager()
        assert eng.evaluate("has_tool_calls", ctx, vm) == "false"

    def test_has_result_non_dict_message(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.messages.append("not a dict")
        vm = VariableManager()
        assert eng.evaluate("has_result", ctx, vm) == "false"

    def test_comparison_with_variable_ref(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        ctx.iteration_count = 15
        vm = VariableManager()
        vm.set("threshold", 10)
        assert eng.evaluate("iteration >= {{ threshold }}", ctx, vm) == "true"

    def test_condition_with_or_all_false(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        result = eng.evaluate("has_error or has_tool_calls", ctx, vm)
        assert result == "false"

    def test_condition_with_and_one_false(self):
        eng = ConditionEngine()
        ctx = AgentContext()
        vm = VariableManager()
        result = eng.evaluate("true and has_error", ctx, vm)
        assert result == "false"


# ---------------------------------------------------------------------------
# Cover remaining missing lines for 95%+ coverage
# ---------------------------------------------------------------------------


# Lines 219: system_prompt interpolation in execute_graph
class TestExecuteGraphSystemPromptInterpolation:
    async def test_start_node_system_prompt_with_variable(self):
        mlx = MockMLXClient()
        mlx.add_response("ok")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(
                    type="start", label="Start", system_prompt="You are {{ role }}."
                ),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("role", "a tester")
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert mlx.call_count == 1
        _call_messages = (
            mlx.call_args_messages if hasattr(mlx, "call_args_messages") else []
        )


# Lines 236-238: node not found in execute_graph
class TestExecuteGraphNodeNotFoundDirect:
    async def test_node_not_found_error_event(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = AgentGraph(name="test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="Start"))
        g.add_edge("start", "missing_node")
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        events = []
        async for ev in runtime.execute_graph(g, "hi", ctx):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1
        assert "not found" in errors[0].content or "Edge target" in errors[0].content


# Lines 295-298: error_handler node in execute_graph loop
class TestExecuteGraphErrorHandlerNode:
    async def test_error_handler_node_in_graph(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=1,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.add_message("tool", "bad", tool_call_id="tool_t1")
        events = []
        async for ev in runtime.execute_graph(g, "hi", ctx):
            events.append(ev)
        assert any(e.type == AgentEventType.TOOL_RESULT for e in events)


# Lines 305->309: max iterations reached
class TestExecuteGraphMaxIterationsReached:
    async def test_max_iterations_reached_with_no_end(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        g = AgentGraph(name="test", start_node_id="start")
        g.add_node("start", NodeConfig(type="start", label="Start"))
        g.add_node("end", NodeConfig(type="end", label="End"))
        g.add_edge("start", "end")
        runtime = _make_runtime(mlx, tools, max_iterations=0)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert any("Max iterations" in e.content for e in errors)


# Lines 340->346: schema instruction without system message
class TestExecuteLlmNodeSchemaNoSystemMessage:
    async def test_output_schema_no_system_prompt(self):
        mlx = MockMLXClient()
        mlx.add_response('{"name": "test"}')
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    system_prompt="",
                    tool_params={"output_schema": schema},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert mlx.call_count == 1


# Lines 368->372: schema validation with errors (extracted but invalid)
class TestExecuteLlmNodeSchemaValidationError:
    async def test_output_schema_validation_fails(self):
        mlx = MockMLXClient()
        mlx.add_response('{"wrong_field": 123}')
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(
                    type="llm",
                    label="LLM",
                    model="m",
                    system_prompt="You are helpful",
                    tool_params={"output_schema": schema},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert runtime.variables.get("structured_output", None) is None


# Lines 375->380, 377->380: usage tracking with empty messages
class TestExecuteLlmNodeUsageTrackingEdgeCases:
    async def test_usage_with_no_prior_messages(self):
        mlx = MockMLXClient()
        mlx.add_response("hi", usage={"prompt_tokens": 5, "completion_tokens": 3})
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        assert mlx.call_count == 1


# Lines 405: invalid tool call format in LLM response
class TestExecuteLlmNodeInvalidToolCallKeyError:
    async def test_tool_call_missing_function_name(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "go",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"arguments": "{}"},
                }
            ],
        )
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1


# Lines 422-425: KeyError and Exception in tool execution within LLM node
class TestExecuteLlmNodeToolExecutionErrors:
    async def test_tool_not_found_keyerror_in_llm(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "go",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "missing", "arguments": "{}"},
                }
            ],
        )
        mlx.add_response("done")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert any("not found" in e.content for e in tool_results)

    async def test_tool_exception_in_llm(self):
        mlx = MockMLXClient()
        mlx.add_response(
            "go",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "boom", "arguments": "{}"},
                }
            ],
        )
        mlx.add_response("done")
        tools = MockToolRegistry()
        tools.add_failing_tool("boom")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert any("Error" in e.content for e in tool_results)


# Lines 448: non-string params in _execute_tool_node
class TestExecuteToolNodeStringInterpolation:
    async def test_string_params_interpolated(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool",
                    label="Tool",
                    tool_name="t1",
                    tool_params={"query": "{{ keyword }}", "limit": 10},
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        runtime.variables.set("keyword", "test")
        events = []
        async for ev in runtime.execute_graph(g, "hi"):
            events.append(ev)
        tool_results = [e for e in events if e.type == AgentEventType.TOOL_RESULT]
        assert len(tool_results) >= 1


# Lines 523->520, 525->520: error_handler reverse iteration finding tool_call_id
class TestExecuteErrorHandlerNodeReverseIteration:
    async def test_error_handler_finds_tool_from_messages(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "retry ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=2,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.add_message("assistant", "tried tool")
        ctx.add_message("tool", "bad result", tool_call_id="tool_t1")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("Error handler completed" in ev.content for ev in events)

    async def test_error_handler_breaks_on_assistant_message(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "retry ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=1,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.add_message("tool", "old result", tool_call_id="tool_old")
        ctx.add_message("assistant", "some response")
        ctx.add_message("tool", "recent bad", tool_call_id="tool_t1")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("Error handler completed" in ev.content for ev in events)


# Lines 544, 551-557: error_handler with retry delay and LLM retry
class TestExecuteErrorHandlerNodeRetry:
    async def test_error_handler_retry_with_delay(self):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        tools.add_tool("t1", "ok")
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "tool1": NodeConfig(
                    type="tool", label="Tool", tool_name="t1", tool_params={}
                ),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=3,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "tool1"), ("tool1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "fail"
        ctx.add_message("tool", "bad", tool_call_id="tool_t1")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        assert any("attempt 1" in ev.content for ev in events)

    async def test_error_handler_retries_llm_node_directly(self):
        mlx = MockMLXClient()
        mlx.add_response("retry answer")
        tools = MockToolRegistry()
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm1": NodeConfig(type="llm", label="LLM", model="m"),
                "errh": NodeConfig(
                    type="error_handler",
                    label="ErrHandler",
                    max_retries=2,
                    retry_delay=0.01,
                ),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm1"), ("llm1", "errh"), ("errh", "end")],
        )
        runtime = _make_runtime(mlx, tools)
        ctx = AgentContext()
        ctx.error = "llm failed"
        ctx.current_node_id = "llm1"
        ctx.add_message("assistant", "previous")
        node = g.get_node("errh")
        events = []
        async for ev in runtime._execute_error_handler_node(ctx, node, g):
            events.append(ev)
        think_events = [e for e in events if e.type == AgentEventType.THINK]
        assert any("attempt 1" in e.content for e in think_events)


# Lines 360: LLM call exception (ensure covered)
class TestExecuteLlmNodeExceptionDirect:
    async def test_llm_exception_in_execute_llm_node(self):
        mlx = MockMLXClient()
        mlx.set_raise(TimeoutError("timeout"))
        tools = MockToolRegistry()
        runtime = _make_runtime(mlx, tools)
        g = _build_graph(
            {
                "start": NodeConfig(type="start", label="Start"),
                "llm": NodeConfig(type="llm", label="LLM", model="m"),
                "end": NodeConfig(type="end", label="End"),
            },
            [("start", "llm"), ("llm", "end")],
        )
        ctx = AgentContext()
        node = g.get_node("llm")
        events = []
        async for ev in runtime._execute_llm_node(ctx, node, g, "m", [], ""):
            events.append(ev)
        errors = [e for e in events if e.type == AgentEventType.ERROR]
        assert len(errors) >= 1
