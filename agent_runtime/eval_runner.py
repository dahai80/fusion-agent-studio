"""E2 (#672 plan): general eval runner — evals attach to runs/traces, not a
separate product.

Phoenix/Langfuse model, local-first: a dataset is a list of cases
(input / expected / scorer); the runner executes each case against a graph
via AgentRuntime and scores the output. Scorers: exact, contains,
tool_sequence (expected tool call order), llm_judge (local model verdict).
eval.from_trace replays a recorded trajectory into a case so a prompt/model
change can be diffed against recorded behavior (reviewer packs from #546
convert into the same dataset format).

Division of labor: agent-studio runs agent-runtime evals (graph/tool/session
level); fusion-bench owns model benchmarks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .trajectory_writer import get_trajectory_writer

logger = logging.getLogger(__name__)

EVALS_DIR = "evals"


class Scorer:
    name = "base"

    def score(self, output: str, expected: str, tool_calls: list[str]) -> dict:
        raise NotImplementedError


class ExactScorer(Scorer):
    name = "exact"

    def score(self, output, expected, tool_calls):
        ok = output.strip() == expected.strip()
        return {"passed": ok, "score": 1.0 if ok else 0.0, "reason": "" if ok else "output != expected"}


class ContainsScorer(Scorer):
    name = "contains"

    def score(self, output, expected, tool_calls):
        ok = expected.strip() in output
        return {"passed": ok, "score": 1.0 if ok else 0.0, "reason": "" if ok else f"expected substring missing: {expected[:80]}"}


class ToolSequenceScorer(Scorer):
    name = "tool_sequence"

    def score(self, output, expected, tool_calls):
        want = [t.strip() for t in str(expected).split(",") if t.strip()]
        ok = tool_calls == want
        return {
            "passed": ok,
            "score": 1.0 if ok else 0.0,
            "reason": "" if ok else f"tool calls {tool_calls} != expected {want}",
        }


class LLMJudgeScorer(Scorer):
    # Local-model judge: verdict must be a single PASS/FAIL line; parse
    # failure counts as fail (never silently pass — Rule 9).
    name = "llm_judge"

    def __init__(self, mlx_client=None):
        self.mlx_client = mlx_client

    def score(self, output, expected, tool_calls):
        if self.mlx_client is None:
            return {"passed": False, "score": 0.0, "reason": "no judge model configured"}
        prompt = (
            "You are grading an AI agent output.\n"
            f"Criteria / reference answer:\n{expected}\n\n"
            f"Agent output:\n{output[:4000]}\n\n"
            "Reply with exactly one line: PASS or FAIL, then a short reason."
        )

        async def _judge():
            resp = await self.mlx_client.chat(model="", messages=[{"role": "user", "content": prompt}])
            return resp.content

        import asyncio

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                new_loop = asyncio.new_event_loop()
                try:
                    verdict = new_loop.run_until_complete(_judge())
                finally:
                    new_loop.close()
            else:
                verdict = asyncio.run(_judge())
        except Exception as e:
            return {"passed": False, "score": 0.0, "reason": f"judge call failed: {e}"}
        first = verdict.strip().splitlines()[0].upper() if verdict.strip() else ""
        ok = first.startswith("PASS")
        return {"passed": ok, "score": 1.0 if ok else 0.0, "reason": verdict.strip()[:200]}


RUBRIC_PARTS = (
    "factual accuracy",
    "citation accuracy",
    "completeness",
    "source quality",
    "tool efficiency",
)


class RubricScorer(Scorer):
    # #697 P9: single LLM judge + 5-part rubric, score 0.0-1.0. One judge
    # call (Anthropic: more consistent than multiple judges). Parse failure
    # counts as fail — never silently pass.
    name = "rubric"

    def __init__(self, judge_client=None):
        self.judge_client = judge_client

    def score(self, output, expected, tool_calls):
        if self.judge_client is None:
            return {"passed": False, "score": 0.0, "reason": "no judge model configured"}
        rubric = "; ".join(f"{i + 1}. {p}" for i, p in enumerate(RUBRIC_PARTS))
        prompt = (
            "You are grading an AI agent output against a rubric.\n"
            f"Rubric dimensions:\n{rubric}\n\n"
            f"Criteria / reference answer:\n{expected}\n\n"
            f"Agent output:\n{output[:4000]}\n\n"
            "Grade each dimension 0.0-1.0, then reply with exactly:\n"
            "SCORE: <overall 0.0-1.0>\n"
            "BREAKDOWN: <d1>=<x>, <d2>=<x>, ...\n"
            "REASON: <one short line>"
        )

        async def _judge():
            resp = await self.judge_client.chat(model="", messages=[{"role": "user", "content": prompt}])
            return resp.content

        import asyncio

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                new_loop = asyncio.new_event_loop()
                try:
                    verdict = new_loop.run_until_complete(_judge())
                finally:
                    new_loop.close()
            else:
                verdict = asyncio.run(_judge())
        except Exception as e:
            return {"passed": False, "score": 0.0, "reason": f"judge call failed: {e}", "rubric": {}}
        score, breakdown = self._parse(verdict)
        if score is None:
            return {
                "passed": False,
                "score": 0.0,
                "reason": f"rubric parse failed: {verdict.strip()[:120]}",
                "rubric": {},
            }
        return {
            "passed": score >= 0.7,
            "score": score,
            "reason": verdict.strip().splitlines()[-1][:200],
            "rubric": breakdown,
        }

    @staticmethod
    def _parse(verdict: str) -> tuple[float | None, dict]:
        import re

        breakdown: dict = {}
        m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)", verdict)
        if not m:
            return None, breakdown
        score = max(0.0, min(1.0, float(m.group(1))))
        bm = re.search(r"BREAKDOWN:\s*(.+)", verdict)
        if bm:
            for part in bm.group(1).split(","):
                if "=" in part:
                    k, _, v = part.partition("=")
                    try:
                        breakdown[k.strip()[:40]] = max(0.0, min(1.0, float(v.strip())))
                    except ValueError:
                        continue
        return score, breakdown


class FailureTagger:
    # #697 P9: MAST-style automated failure pre-tagging on eval entries —
    # FC1 system design (execution broke), FC2 misalignment (output missed
    # the expectation), FC3 verification (no tools when tools were the
    # mechanism / tool errors present). Deterministic, replay-safe.
    FC1 = "FC1"
    FC2 = "FC2"
    FC3 = "FC3"

    @classmethod
    def tag(cls, entry: dict, expected: str = "", tool_calls: list[str] | None = None) -> str:
        tool_calls = tool_calls or []
        if entry.get("reason", "").startswith("execution error") or "graph not found" in entry.get("reason", ""):
            return cls.FC1
        reason = str(entry.get("reason", ""))
        if "tool calls" in reason and "!= expected" in reason:
            return cls.FC3
        if tool_calls and any(str(t).endswith(":error") for t in tool_calls):
            return cls.FC3
        if not entry.get("passed") and expected:
            if not tool_calls and ("tool" in expected.lower() or "use" in expected.lower()):
                return cls.FC3
            return cls.FC2
        if not entry.get("passed"):
            return cls.FC2
        return ""


SCORERS: dict[str, Scorer] = {
    "exact": ExactScorer(),
    "contains": ContainsScorer(),
    "tool_sequence": ToolSequenceScorer(),
}


@dataclass
class EvalCase:
    id: str
    input: str
    expected: str = ""
    scorer: str = "contains"
    graph_id: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "input": self.input,
            "expected": self.expected,
            "scorer": self.scorer,
            "graph_id": self.graph_id,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvalCase":
        return cls(
            id=str(data.get("id", "")),
            input=str(data.get("input", "")),
            expected=str(data.get("expected", "")),
            scorer=str(data.get("scorer", "contains")),
            graph_id=str(data.get("graph_id", "")),
            meta=dict(data.get("meta", {}) or {}),
        )


@dataclass
class EvalDataset:
    name: str
    cases: list[EvalCase] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "source": self.source, "cases": [c.to_dict() for c in self.cases]}

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> "EvalDataset":
        cases = [EvalCase.from_dict(c) for c in (data.get("cases") or []) if isinstance(c, dict)]
        return cls(name=str(data.get("name", "")), cases=cases, source=source)

    @classmethod
    def load_yaml(cls, path: Path | str) -> "EvalDataset":
        p = Path(path)
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data, source=str(p))

    @classmethod
    def from_reviewer_pack(cls, pack: dict, name: str = "") -> "EvalDataset":
        # #546 reviewer packs convert into dataset cases: prompt->input,
        # expectations->expected (contains scoring).
        cases = []
        for i, item in enumerate(pack.get("cases", []) or []):
            cases.append(
                EvalCase(
                    id=str(item.get("id", f"pack-{i}")),
                    input=str(item.get("prompt", item.get("input", ""))),
                    expected=str(item.get("expectations", item.get("expected", ""))),
                    scorer="contains",
                )
            )
        return cls(name=name or str(pack.get("name", "reviewer-pack")), cases=cases)


@dataclass
class EvalReport:
    dataset: str
    results: list[dict] = field(default_factory=list)
    pass_rate: float = 0.0
    duration_s: float = 0.0
    # #697: attribution aggregates — MAST-style failure-class histogram and
    # per-asset (memory/skill) hit counts across the run.
    failure_histogram: dict = field(default_factory=dict)
    hit_counts: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "pass_rate": round(self.pass_rate, 4),
            "duration_s": round(self.duration_s, 3),
            "failure_histogram": self.failure_histogram,
            "hit_counts": self.hit_counts,
            "results": self.results,
        }


def evals_dir() -> Path:
    from .paths import studio_home

    return studio_home() / EVALS_DIR


class EvalRunner:
    def __init__(self, runtime=None, graph_loader=None, judge_client=None, provenance_fn=None):
        # runtime: AgentRuntime-like with execute_graph(graph, input);
        # graph_loader: callable(graph_id) -> AgentGraph | None.
        # provenance_fn: optional callable(case, tool_calls) -> dict for
        # live-run asset hit provenance (replay path reads it from the
        # trajectory record instead).
        self.runtime = runtime
        self.graph_loader = graph_loader
        self.judge_client = judge_client
        self.provenance_fn = provenance_fn

    def _scorer(self, name: str) -> Scorer:
        if name == "llm_judge":
            return LLMJudgeScorer(self.judge_client)
        if name == "rubric":
            return RubricScorer(self.judge_client)
        return SCORERS.get(name, SCORERS["contains"])

    async def _execute_case(self, case: EvalCase, graph) -> tuple[str, list[str]]:
        output = ""
        tool_calls: list[str] = []
        async for ev in self.runtime.execute_graph(graph, case.input):
            t = getattr(ev.type, "value", str(ev.type))
            if t == "result":
                output = ev.content
            elif t == "tool_call":
                tool_calls.append(ev.name or "")
        if not output:
            output = ""
        return output, [t for t in tool_calls if t]

    @staticmethod
    def _hit_provenance(case: EvalCase, tool_calls: list[str]) -> dict:
        # #697: replay provenance wins (identical on re-replay); skill hits
        # are derivable from tool calls; live graph runs may inject the rest
        # via provenance_fn at construction.
        provenance = dict(case.meta.get("hit_provenance", {}) or {})
        skill_hits = [
            t for t in tool_calls if t.startswith("skill") or t in ("memory_search",)
        ]
        if skill_hits:
            provenance.setdefault("skill_hits", []).extend(skill_hits)
        return provenance

    @staticmethod
    def _failure_histogram(results: list[dict]) -> dict:
        hist: dict = {}
        for r in results:
            fc = r.get("failure_class", "")
            if fc:
                hist[fc] = hist.get(fc, 0) + 1
        return dict(sorted(hist.items()))

    @staticmethod
    def _hit_counts(results: list[dict]) -> dict:
        counts = {"memory": 0, "skill": 0}
        for r in results:
            hp = r.get("hit_provenance", {}) or {}
            counts["memory"] += len(hp.get("memory_hits", []) or [])
            counts["skill"] += len(hp.get("skill_hits", []) or [])
        return counts

    async def run(self, dataset: EvalDataset, graph_id: str = "", limit: int = 0) -> EvalReport:
        started = time.time()
        report = EvalReport(dataset=dataset.name)
        cases = dataset.cases[:limit] if limit > 0 else dataset.cases
        for case in cases:
            gid = case.graph_id or graph_id
            entry = {"case_id": case.id, "scorer": case.scorer}
            if self.runtime is None or self.graph_loader is None:
                entry.update({"passed": False, "score": 0.0, "reason": "runner not wired (no runtime/graph_loader)"})
                report.results.append(entry)
                continue
            graph = self.graph_loader(gid)
            if graph is None:
                entry.update({"passed": False, "score": 0.0, "reason": f"graph not found: {gid}"})
                report.results.append(entry)
                continue
            try:
                output, tool_calls = await self._execute_case(case, graph)
            except Exception as e:
                logger.warning("eval case execution failed case=%s: %s", case.id, e)
                entry.update({"passed": False, "score": 0.0, "reason": f"execution error: {e}"})
                entry["failure_class"] = FailureTagger.tag(entry, case.expected, [])
                report.results.append(entry)
                continue
            verdict = self._scorer(case.scorer).score(output, case.expected, tool_calls)
            entry.update(verdict)
            entry["output"] = output[:2000]
            entry["tool_calls"] = tool_calls
            entry["failure_class"] = FailureTagger.tag(entry, case.expected, tool_calls)
            entry["hit_provenance"] = self._hit_provenance(case, tool_calls)
            report.results.append(entry)
        report.failure_histogram = self._failure_histogram(report.results)
        report.hit_counts = self._hit_counts(report.results)
        passed = sum(1 for r in report.results if r.get("passed"))
        report.pass_rate = passed / len(report.results) if report.results else 0.0
        report.duration_s = time.time() - started
        logger.info(
            "eval run dataset=%s cases=%d passed=%d rate=%.2f dur=%.1fs",
            dataset.name, len(report.results), passed, report.pass_rate, report.duration_s,
        )
        return report

    def from_trace(self, trace_id: str) -> EvalCase | None:
        # Replay a recorded trajectory into a case: the recorded output
        # becomes the baseline expectation (contains scoring) so a prompt /
        # model change can be diffed against recorded behavior.
        writer = get_trajectory_writer()
        for meta in writer.list_trajectories(limit=200):
            if meta.get("trace_id") != trace_id:
                continue
            path = writer.output_dir / meta["file"]
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("trajectory load failed trace=%s: %s", trace_id, e)
                return None
            messages = data.get("messages") or []
            inp = next(
                (str(m.get("content", "")) for m in messages if m.get("role") == "user" and m.get("content")),
                "",
            )
            out = next(
                (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
                "",
            )
            if not inp:
                logger.warning("trajectory has no user input trace=%s", trace_id)
                return None
            return EvalCase(
                id=f"replay-{trace_id[:12]}",
                input=inp,
                expected=out,
                scorer="contains",
                graph_id=str(data.get("graph_id", "")),
                meta={
                    "trace_id": trace_id,
                    "graph_name": data.get("graph_name", ""),
                    "recorded_status": data.get("status", ""),
                    # #697: hit provenance rides the replay so attribution
                    # survives replay identically.
                    "hit_provenance": dict(data.get("hit_provenance", {}) or {}),
                },
            )
        logger.warning("trajectory not found trace=%s", trace_id)
        return None

    def list_datasets(self) -> list[dict]:
        d = evals_dir()
        out = []
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                try:
                    ds = EvalDataset.load_yaml(p)
                    out.append({"name": ds.name, "source": str(p), "cases": len(ds.cases)})
                except Exception as e:
                    logger.warning("eval dataset load failed path=%s: %s", p, e)
        return out
