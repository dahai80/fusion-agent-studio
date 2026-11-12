"""E2 proof: eval runner reproducibility + trace replay.

Fixed 20-case dataset (contains scoring) run twice against a deterministic
stub runtime: reports must be byte-identical (same pass rate, same per-case
verdicts) — an eval that is not reproducible gates nothing. Then from_trace
replay: a recorded trajectory converts into a case whose input/expected
match the recording.

Runner: python tests/proof/prove_eval_runner.py
CI: proof job — exit 0 = pass
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.context import AgentEventType  # noqa: E402
from agent_runtime.eval_runner import EvalCase, EvalDataset, EvalRunner  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("proof.eval")


class DeterministicRuntime:
    # Output = f(input): stable across runs so the eval itself must be too.
    async def execute_graph(self, graph, input, **kwargs):
        out = f"answer[{input.strip()[:20]}]"

        class _E:
            def __init__(self, t, c):
                self.type = t
                self.content = c

        yield _E(AgentEventType.RESULT, out)


def _dataset(n: int = 20) -> EvalDataset:
    return EvalDataset(
        name="proof-fixed",
        cases=[
            EvalCase(id=f"c{i:02d}", input=f"question {i}", expected=f"answer[question {i}]")
            for i in range(n)
        ],
    )


def prove_eval_runner() -> int:
    runner = EvalRunner(
        runtime=DeterministicRuntime(), graph_loader=lambda gid: object()
    )
    ds = _dataset()
    r1 = asyncio.run(runner.run(ds))
    r2 = asyncio.run(runner.run(ds))
    if r1.to_dict() != r2.to_dict():
        logger.error("eval not reproducible: run1 != run2")
        return 1
    if r1.pass_rate != 1.0:
        logger.error("expected full pass on deterministic pair: %s", r1.to_dict())
        return 1
    # mutate one expected value -> pass rate must drop to 19/20
    ds_bad = _dataset()
    ds_bad.cases[7].expected = "wrong"
    r3 = asyncio.run(runner.run(ds_bad))
    if abs(r3.pass_rate - 0.95) > 1e-9:
        logger.error("pass rate after 1 corruption != 0.95: %s", r3.pass_rate)
        return 1
    # trace replay round-trip
    rec = {
        "trace_id": "trace-proof-0001", "session_id": "s", "graph_id": "g",
        "graph_name": "n", "agent_id": "", "started_at": 0, "finished_at": 0,
        "status": "completed", "iteration_count": 1, "max_iterations": 25,
        "events": [], "tool_calls": [], "node_transitions": [], "error": "",
        "token_usage": {}, "duration_ms": 1,
        "messages": [
            {"role": "user", "content": "replay question"},
            {"role": "assistant", "content": "replay answer"},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "0_t.json"
        f.write_text(json.dumps(rec), encoding="utf-8")
        from agent_runtime import eval_runner as er

        class FakeWriter:
            output_dir = Path(td)

            def list_trajectories(self, limit=50):
                return [{"trace_id": "trace-proof-0001", "file": f.name}]

        er.get_trajectory_writer = lambda: FakeWriter()
        case = EvalRunner().from_trace("trace-proof-0001")
    if case is None or case.input != "replay question" or case.expected != "replay answer":
        logger.error("trace replay produced wrong case: %s", case)
        return 1
    print(
        f"OK cases={len(r1.results)} pass_rate={r1.pass_rate} "
        f"reproducible=true corrupted_rate={r3.pass_rate} replay_ok=true"
    )
    return 0


if __name__ == "__main__":
    sys.exit(prove_eval_runner())
