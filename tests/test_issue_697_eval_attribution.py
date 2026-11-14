"""#697 P9: eval attribution — rubric judge + MAST failure tagging +
hit provenance (memory/skill) surviving replay."""

from __future__ import annotations

from agent_runtime.context import AgentContext
from agent_runtime.eval_runner import (
    EvalCase,
    EvalDataset,
    EvalRunner,
    FailureTagger,
    RubricScorer,
)
from agent_runtime.trajectory_writer import TrajectoryWriter

# ---------------- RubricScorer ----------------

class _JudgeClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def chat(self, model="", messages=None, **k):
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(content=self.reply)


def test_rubric_scorer_parses_score_and_breakdown():
    client = _JudgeClient(
        "SCORE: 0.8\n"
        "BREAKDOWN: factual accuracy=0.9, citation accuracy=0.8, completeness=0.7, "
        "source quality=0.8, tool efficiency=0.9\n"
        "REASON: mostly accurate, slightly incomplete"
    )
    r = RubricScorer(client).score("out", "expected", ["tool_a"])
    assert r["passed"] is True
    assert r["score"] == 0.8
    assert len(r["rubric"]) == 5
    assert client.calls == 1


def test_rubric_scorer_parse_failure_fails_closed():
    client = _JudgeClient("I think it looks fine")
    r = RubricScorer(client).score("out", "expected", [])
    assert r["passed"] is False and r["score"] == 0.0
    assert "parse failed" in r["reason"]


def test_rubric_scorer_no_client_fails_closed():
    r = RubricScorer(None).score("out", "expected", [])
    assert r["passed"] is False and r["score"] == 0.0


def test_rubric_score_clamped():
    s, b = RubricScorer._parse("SCORE: 1.7\nBREAKDOWN: a=2.0, b=bad")
    assert s == 1.0
    assert b == {"a": 1.0}


# ---------------- FailureTagger ----------------

def test_failure_tagger_classes():
    assert FailureTagger.tag({"passed": False, "reason": "execution error: boom"}) == "FC1"
    assert (
        FailureTagger.tag({"passed": False, "reason": "tool calls ['a'] != expected ['b']"})
        == "FC3"
    )
    assert FailureTagger.tag({"passed": False, "reason": "missing"}, expected="answer it") == "FC2"
    assert (
        FailureTagger.tag({"passed": False, "reason": "missing"}, expected="use the file tool", tool_calls=[])
        == "FC3"
    )
    assert FailureTagger.tag({"passed": True, "reason": ""}) == ""


# ---------------- Hit provenance ----------------

def test_ctx_hit_provenance_field():
    ctx = AgentContext()
    assert ctx.hit_provenance == {}
    ctx.hit_provenance.setdefault("memory_hits", []).append({"query": "q"})
    assert len(ctx.hit_provenance["memory_hits"]) == 1


def test_trajectory_roundtrip_hit_provenance(tmp_path):
    w = TrajectoryWriter(output_dir=tmp_path)
    w.start(session_id="s-hp", graph_name="g")
    w.record_hit_provenance(
        "s-hp",
        {"memory_hits": [{"query": "q", "chars": 50}], "skill_hits": ["skill_search"]},
    )
    path = w.flush("s-hp", status="completed")
    assert path
    import json

    data = json.load(open(path))
    assert data["hit_provenance"]["memory_hits"] == [{"query": "q", "chars": 50}]
    assert data["hit_provenance"]["skill_hits"] == ["skill_search"]


def test_from_trace_carries_provenance(tmp_path, monkeypatch):
    import agent_runtime.trajectory_writer as tw

    w = TrajectoryWriter(output_dir=tmp_path)
    monkeypatch.setattr(tw, "_writer", w)
    w.start(session_id="s-rp", graph_name="g")
    w.record_messages(
        "s-rp",
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "recorded answer"},
        ],
    )
    w.record_hit_provenance("s-rp", {"memory_hits": [{"query": "question", "chars": 9}]})
    w.flush("s-rp", status="completed")

    import json
    from pathlib import Path

    files = sorted(Path(tmp_path).glob("*.json"))
    data = json.load(open(files[-1]))
    runner = EvalRunner()
    case = runner.from_trace(data["trace_id"])
    assert case is not None
    assert case.meta["hit_provenance"]["memory_hits"] == [{"query": "question", "chars": 9}]


def test_hit_provenance_derives_skill_hits_from_tool_calls():
    case = EvalCase(id="c1", input="i", meta={})
    p = EvalRunner._hit_provenance(case, ["file_read", "skill_search", "memory_search"])
    assert p["skill_hits"] == ["skill_search", "memory_search"]


def test_replay_provenance_identical_on_second_replay(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    import agent_runtime.trajectory_writer as tw

    w = TrajectoryWriter(output_dir=tmp_path)
    monkeypatch.setattr(tw, "_writer", w)
    w.start(session_id="s-rr", graph_name="g")
    w.record_messages(
        "s-rr",
        [
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ],
    )
    w.record_hit_provenance(
        "s-rr", {"memory_hits": [{"query": "q2", "chars": 2}], "skill_hits": ["skill_search"]}
    )
    w.flush("s-rr", status="completed")
    data = json.load(open(sorted(Path(tmp_path).glob("*.json"))[-1]))

    runner = EvalRunner()
    c1 = runner.from_trace(data["trace_id"])
    c2 = runner.from_trace(data["trace_id"])
    assert c1.meta["hit_provenance"] == c2.meta["hit_provenance"]


# ---------------- Report aggregation ----------------

def test_report_histogram_and_hit_counts():
    runner = EvalRunner()
    ds = EvalDataset(
        name="d",
        cases=[
            EvalCase(id="c1", input="i", expected="e", scorer="contains"),
        ],
    )

    class _Rt:
        async def execute_graph(self, graph, inp):
            raise RuntimeError("model exploded")

    class _Loader:
        def __call__(self, gid):
            return object()

    runner.runtime = _Rt()
    runner.graph_loader = _Loader()
    import asyncio

    report = asyncio.run(runner.run(ds, graph_id="g"))
    assert report.failure_histogram == {"FC1": 1}
    assert report.hit_counts == {"memory": 0, "skill": 0}
    d = report.to_dict()
    assert "failure_histogram" in d and "hit_counts" in d


def test_report_hit_counts_aggregate():
    results = [
        {"hit_provenance": {"memory_hits": [1, 2], "skill_hits": ["s"]}},
        {"hit_provenance": {"skill_hits": ["s2"]}},
        {"hit_provenance": {}},
    ]
    assert EvalRunner._hit_counts(results) == {"memory": 2, "skill": 2}
    assert EvalRunner._failure_histogram(
        [{"failure_class": "FC2"}, {"failure_class": "FC2"}, {"failure_class": ""}]
    ) == {"FC2": 2}
