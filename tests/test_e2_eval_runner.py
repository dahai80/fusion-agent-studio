"""E2: general eval runner — datasets, scorers, trace replay, RPC surface."""

from __future__ import annotations

import json

from agent_runtime.context import AgentEventType
from agent_runtime.dispatchers.eval import EvalDispatcher
from agent_runtime.eval_runner import (
    EvalCase,
    EvalDataset,
    EvalRunner,
    LLMJudgeScorer,
    ToolSequenceScorer,
)


class FakeEvent:
    def __init__(self, ev_type, content="", name=""):
        self.type = ev_type
        self.content = content
        self.name = name


class StubRuntime:
    def __init__(self, output="the answer is 4", tool_calls=None):
        self.output = output
        self.tool_calls = tool_calls or []
        self.calls = []

    async def execute_graph(self, graph, input, **kwargs):
        self.calls.append(input)
        yield FakeEvent(AgentEventType.RESULT, content=self.output)
        for t in self.tool_calls:
            yield FakeEvent(AgentEventType.TOOL_CALL, name=t)


class TestScorers:
    def test_exact(self):
        from agent_runtime.eval_runner import ExactScorer

        assert ExactScorer().score("hi", "hi", [])["passed"]
        assert not ExactScorer().score("hi", "hi there", [])["passed"]

    def test_contains(self):
        from agent_runtime.eval_runner import ContainsScorer

        assert ContainsScorer().score("answer: 4 total", "4", [])["passed"]
        assert not ContainsScorer().score("nope", "4", [])["passed"]

    def test_tool_sequence(self):
        s = ToolSequenceScorer()
        assert s.score("", "read_file,write_file", ["read_file", "write_file"])["passed"]
        assert not s.score("", "read_file", ["read_file", "write_file"])["passed"]
        assert not s.score("", "read_file, write_file", ["read_file"])["passed"]

    def test_llm_judge_no_client_fails_closed(self):
        verdict = LLMJudgeScorer(None).score("out", "criteria", [])
        assert not verdict["passed"] and "no judge" in verdict["reason"]

    def test_llm_judge_verdict_parse(self):
        class Judge:
            async def chat(self, model, messages, **kwargs):
                from server.fusion_mlx_client import LLMResponse

                return LLMResponse(content="PASS\noutput matches criteria")

        v = LLMJudgeScorer(Judge()).score("out", "crit", [])
        assert v["passed"]

    def test_llm_judge_garbage_fails(self):
        class Judge:
            async def chat(self, model, messages, **kwargs):
                from server.fusion_mlx_client import LLMResponse

                return LLMResponse(content="seems okay maybe")

        assert not LLMJudgeScorer(Judge()).score("out", "crit", [])["passed"]


class TestDataset:
    def test_yaml_roundtrip(self, tmp_path):
        p = tmp_path / "ds.yaml"
        p.write_text(
            "name: math\n"
            "cases:\n"
            "  - id: c1\n"
            "    input: '2+2'\n"
            "    expected: '4'\n"
            "    scorer: contains\n",
            encoding="utf-8",
        )
        ds = EvalDataset.load_yaml(p)
        assert ds.name == "math" and len(ds.cases) == 1
        assert ds.cases[0].scorer == "contains"

    def test_reviewer_pack_conversion(self):
        pack = {
            "name": "safety-pack",
            "cases": [{"id": "a", "prompt": "delete files", "expectations": "blocked"}],
        }
        ds = EvalDataset.from_reviewer_pack(pack)
        assert ds.cases[0].input == "delete files"
        assert ds.cases[0].scorer == "contains"


class TestRunner:
    def _runner(self, runtime=None):
        return EvalRunner(runtime=runtime, graph_loader=lambda gid: object())

    async def test_run_pass_rate(self):
        rt = StubRuntime(output="the answer is 4")
        ds = EvalDataset(
            name="t",
            cases=[
                EvalCase(id="c1", input="2+2", expected="4"),
                EvalCase(id="c2", input="2+3", expected="5"),
            ],
        )
        report = await self._runner(rt).run(ds)
        assert report.pass_rate == 0.5
        assert report.results[0]["passed"] and not report.results[1]["passed"]
        assert rt.calls == ["2+2", "2+3"]

    async def test_run_graph_missing_fails_case(self):
        runner = EvalRunner(runtime=StubRuntime(), graph_loader=lambda gid: None)
        ds = EvalDataset(name="t", cases=[EvalCase(id="c1", input="x")])
        report = await runner.run(ds)
        assert not report.results[0]["passed"]
        assert "graph not found" in report.results[0]["reason"]

    async def test_run_execution_error_fails_case(self):
        class BoomRuntime:
            async def execute_graph(self, graph, input, **kwargs):
                raise RuntimeError("boom")
                yield  # pragma: no cover

        ds = EvalDataset(name="t", cases=[EvalCase(id="c1", input="x")])
        report = await self._runner(BoomRuntime()).run(ds)
        assert not report.results[0]["passed"]
        assert "boom" in report.results[0]["reason"]

    async def test_limit(self):
        rt = StubRuntime()
        ds = EvalDataset(
            name="t",
            cases=[EvalCase(id=f"c{i}", input="x") for i in range(5)],
        )
        report = await self._runner(rt).run(ds, limit=2)
        assert len(report.results) == 2

    async def test_unwired_runner_fails_honest(self):
        runner = EvalRunner()
        ds = EvalDataset(name="t", cases=[EvalCase(id="c1", input="x")])
        report = await runner.run(ds)
        assert not report.results[0]["passed"]


class TestFromTrace:
    def _write_trace(self, tmp_path, trace_id, messages):
        rec = {
            "trace_id": trace_id,
            "session_id": "s",
            "graph_id": "g1",
            "graph_name": "gn",
            "agent_id": "",
            "started_at": 0,
            "finished_at": 0,
            "status": "completed",
            "iteration_count": 1,
            "max_iterations": 25,
            "events": [],
            "messages": messages,
            "tool_calls": [],
            "node_transitions": [],
            "error": "",
            "token_usage": {},
            "duration_ms": 1,
        }
        f = tmp_path / f"0_{trace_id}.json"
        f.write_text(json.dumps(rec), encoding="utf-8")
        return f

    def test_from_trace_replays_case(self, tmp_path, monkeypatch):
        self._write_trace(
            tmp_path,
            "trace-abc123",
            [
                {"role": "user", "content": "what is 2+2"},
                {"role": "assistant", "content": "4"},
            ],
        )
        from agent_runtime import eval_runner as er

        class FakeWriter:
            output_dir = tmp_path

            def list_trajectories(self, limit=50):
                return [{"trace_id": "trace-abc123", "file": "0_trace-abc123.json"}]

        monkeypatch.setattr(er, "get_trajectory_writer", lambda: FakeWriter())
        case = EvalRunner().from_trace("trace-abc123")
        assert case is not None
        assert case.input == "what is 2+2"
        assert case.expected == "4"
        assert case.scorer == "contains"
        assert case.meta["trace_id"] == "trace-abc123"

    def test_from_trace_missing_returns_none(self, tmp_path, monkeypatch):
        from agent_runtime import eval_runner as er

        class FakeWriter:
            output_dir = tmp_path

            def list_trajectories(self, limit=50):
                return []

        monkeypatch.setattr(er, "get_trajectory_writer", lambda: FakeWriter())
        assert EvalRunner().from_trace("nope") is None


class TestDispatcher:
    def _daemon_stub(self):
        class D:
            runtime = None

            def _get_store(self):
                class S:
                    def load_graph(self, gid):
                        return None

                return S()

        return D()

    async def test_eval_run_and_list(self, tmp_path, monkeypatch):
        import agent_runtime.eval_runner as er

        monkeypatch.setattr(er, "evals_dir", lambda: tmp_path)
        (tmp_path / "ds.yaml").write_text(
            "name: t\ncases:\n  - id: c1\n    input: x\n    expected: y\n",
            encoding="utf-8",
        )
        d = EvalDispatcher(self._daemon_stub())
        listing = d.get_handlers()["eval.listDatasets"]({})
        assert listing["datasets"][0]["name"] == "t"
        result = await d.get_handlers()["eval.run"]({"dataset_path": str(tmp_path / "ds.yaml")})
        assert result["dataset"] == "t"

    async def test_eval_run_requires_dataset(self):
        d = EvalDispatcher(self._daemon_stub())
        result = await d.get_handlers()["eval.run"]({})
        assert "error" in result

    async def test_eval_from_trace_requires_id(self):
        d = EvalDispatcher(self._daemon_stub())
        result = await d.get_handlers()["eval.fromTrace"]({})
        assert "error" in result
