import pytest
from unittest.mock import AsyncMock, MagicMock

from agent_runtime.verifier import VerificationEngine, VerificationResult
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.context import AgentContext, AgentEventType
from agent_runtime.runtime import AgentRuntime
from server.fusion_mlx_client import LLMResponse


class MockMLXClient:
    def __init__(self):
        self.call_count = 0
        self.responses = []

    def add_response(self, content="", tool_calls=None, usage=None):
        self.responses.append(
            LLMResponse(
                content=content,
                tool_calls=tool_calls or [],
                usage=usage or {"prompt_tokens": 0, "completion_tokens": 0},
            )
        )

    async def chat(
        self, model, messages, tools=None, temperature=0.7, max_tokens=4096, **kwargs
    ):
        self.call_count += 1
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

    def to_openai_schemas(self):
        return self._schemas

    def get(self, name):
        raise KeyError(name)


class TestVerificationResult:
    def test_default_values(self):
        r = VerificationResult()
        assert r.passed is False
        assert r.score == 0.0
        assert r.issues == []
        assert r.attempt == 1
        assert r.max_attempts == 3
        assert r.id.startswith("verify_")

    def test_to_dict(self):
        r = VerificationResult(passed=True, score=0.9, issues=[], attempt=1)
        d = r.to_dict()
        assert d["passed"] is True
        assert d["score"] == 0.9
        assert d["attempt"] == 1

    def test_from_dict(self):
        data = {
            "id": "v1",
            "passed": True,
            "score": 0.85,
            "issues": [],
            "suggestion": "",
            "attempt": 2,
            "max_attempts": 3,
            "verified_at": 123.0,
            "metadata": {},
        }
        r = VerificationResult.from_dict(data)
        assert r.id == "v1"
        assert r.passed is True
        assert r.score == 0.85
        assert r.attempt == 2


class TestVerificationEngine:
    def _make_gateway(self, content: str) -> MagicMock:
        gw = MagicMock()
        response = MagicMock()
        response.content = content
        gw.chat = AsyncMock(return_value=response)
        return gw

    @pytest.mark.asyncio
    async def test_verify_passed(self):
        gw = self._make_gateway(
            '{"passed": true, "score": 0.95, "issues": [], "suggestion": ""}'
        )
        engine = VerificationEngine(gateway=gw, max_attempts=3)
        result = await engine.verify(
            task="summarize", output="summary text", criteria="complete"
        )
        assert result.passed is True
        assert result.score == 0.95

    @pytest.mark.asyncio
    async def test_verify_failed_then_passed(self):
        fail_resp = MagicMock()
        fail_resp.content = '{"passed": false, "score": 0.3, "issues": ["missing key point"], "suggestion": "add intro"}'
        pass_resp = MagicMock()
        pass_resp.content = (
            '{"passed": true, "score": 0.9, "issues": [], "suggestion": ""}'
        )
        gw = MagicMock()
        gw.chat = AsyncMock(side_effect=[fail_resp, pass_resp])
        engine = VerificationEngine(gateway=gw, max_attempts=3)
        result = await engine.verify(
            task="summarize", output="bad summary", criteria="complete"
        )
        assert result.passed is True
        assert result.attempt == 2

    @pytest.mark.asyncio
    async def test_verify_all_attempts_fail(self):
        gw = self._make_gateway(
            '{"passed": false, "score": 0.2, "issues": ["incomplete"], "suggestion": "rewrite"}'
        )
        engine = VerificationEngine(gateway=gw, max_attempts=2)
        result = await engine.verify(task="test", output="bad", criteria="good")
        assert result.passed is False
        assert result.attempt == 2

    @pytest.mark.asyncio
    async def test_verify_no_gateway(self):
        engine = VerificationEngine(gateway=None)
        result = await engine.verify(task="test", output="out")
        assert result.passed is False
        assert "No LLM gateway configured" in result.issues

    @pytest.mark.asyncio
    async def test_verify_invalid_json(self):
        gw = self._make_gateway("not json at all")
        engine = VerificationEngine(gateway=gw, max_attempts=1)
        result = await engine.verify(task="test", output="out")
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_re_verify(self):
        gw = self._make_gateway(
            '{"passed": true, "score": 0.88, "issues": [], "suggestion": ""}'
        )
        engine = VerificationEngine(gateway=gw)
        result = await engine.re_verify(
            task="fix bug",
            original_output="buggy",
            new_output="fixed",
            fix_description="added null check",
            criteria="no crash",
        )
        assert result.passed is True


class TestVerifyNodeInRuntime:
    def _make_gateway(self, content: str) -> MagicMock:
        gw = MagicMock()
        response = MagicMock()
        response.content = content
        gw.chat = AsyncMock(return_value=response)
        return gw

    def _make_runtime(self, gw):
        mlx = MockMLXClient()
        tools = MockToolRegistry()
        runtime = AgentRuntime(mlx, tools)
        runtime.llm_gateway = gw
        return runtime

    @pytest.mark.asyncio
    async def test_verify_node_dispatch(self):
        gw = self._make_gateway(
            '{"passed": true, "score": 0.9, "issues": [], "suggestion": ""}'
        )
        runtime = self._make_runtime(gw)

        graph = AgentGraph(name="test_verify")
        graph.add_node("start", NodeConfig(type="start", label="start"))
        graph.add_node(
            "verify1",
            NodeConfig(
                type="verify",
                label="verify1",
                tool_params={
                    "task": "check output",
                    "output": "some output",
                    "criteria": "must be correct",
                    "max_attempts": 1,
                },
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="end"))
        graph.add_edge("start", "verify1")
        graph.add_edge("verify1", "end")

        ctx = AgentContext()
        events = []
        async for event in runtime.execute_graph(graph, context=ctx):
            events.append(event)

        verify_events = [e for e in events if e.type == AgentEventType.VERIFY]
        assert len(verify_events) == 1
        assert "passed" in verify_events[0].content
        assert verify_events[0].metadata["passed"] is True

    @pytest.mark.asyncio
    async def test_verify_node_sets_variables(self):
        gw = self._make_gateway(
            '{"passed": false, "score": 0.4, "issues": ["bad"], "suggestion": "fix it"}'
        )
        runtime = self._make_runtime(gw)

        graph = AgentGraph(name="test_verify_fail")
        graph.add_node("start", NodeConfig(type="start", label="start"))
        graph.add_node(
            "v1",
            NodeConfig(
                type="verify",
                label="v1",
                tool_params={
                    "task": "t",
                    "output": "o",
                    "criteria": "c",
                    "max_attempts": 1,
                },
            ),
        )
        graph.add_node("end", NodeConfig(type="end", label="end"))
        graph.add_edge("start", "v1")
        graph.add_edge("v1", "end")

        ctx = AgentContext()
        async for _ in runtime.execute_graph(graph, context=ctx):
            pass

        assert runtime.variables.get("verify_passed") is False
        assert runtime.variables.get("verify_score") == 0.4
