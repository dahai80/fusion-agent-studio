"""Issue #646: parked-run recovery on quota/budget exhaustion.

t3code UsageLimitRecoveryWorker semantics on the managed SSE run path:
- budget exhaustion parks the run (usage_limit), never fails it as content
- parked run persists usage_limit_reset_at + resumes automatically when
  the window resets (fake-clock integration test)
- 24h park cap fails honestly — no zombie parking
- content failures still fail immediately (no conflation)
- TokenBudget.next_day_reset_at() gives the day-cap reset window

Runner: pytest tests/test_issue_646_parked_runs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.parked_runs import (
    PARK_CAP_SECONDS,
    ParkedRunRecovery,
    ParkedRunStore,
    compute_reset_at,
)
from agent_runtime.run_registry import ManagedRun, RunRegistry
from agent_runtime.token_budget import TokenBudget


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class _ResumeSpy:
    def __init__(self):
        self.resumed: list[str] = []

    def __call__(self, run):
        self.resumed.append(run.run_id)


def _parked_run(registry: RunRegistry, reset_at: float, clock: FakeClock) -> ManagedRun:
    run = registry.create("g1")
    run.park(reset_at, "usage_limit", {"graph_id": "g1", "input": "x"})
    return run


class TestParkAndPersist:
    def test_park_sets_state_and_persists(self, tmp_path: Path):
        store = ParkedRunStore(str(tmp_path / "parked.json"))
        registry = RunRegistry()
        run = registry.create("g1")
        run.park(2000.0, "usage_limit", {"graph_id": "g1", "input": "x"})
        store.upsert(
            {
                "run_id": run.run_id,
                "graph_id": "g1",
                "parked_at": run.parked_at,
                "parked_until": 2000.0,
                "park_reason": "usage_limit",
            }
        )
        loaded = store.load()
        assert loaded[run.run_id]["parked_until"] == 2000.0
        status = run.park_status()
        assert status["parked"] is True
        assert status["resumes_at"] == 2000.0

    def test_compute_reset_at_uses_day_roll(self):
        budget = TokenBudget(day_max_tokens=100, day_spent_tokens=100)
        reset = compute_reset_at(budget, 1000.0)
        assert reset > 1000.0, "day-cap reset must be a future timestamp"

    def test_compute_reset_at_default_window_without_budget(self):
        assert compute_reset_at(None, 1000.0) == 1000.0 + 3600


class TestRecoveryWorker:
    async def test_parked_run_resumes_on_reset(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FUSION_PARKED_RUNS_PATH", str(tmp_path / "parked.json"))
        clock = FakeClock()
        registry = RunRegistry()
        spy = _ResumeSpy()
        run = _parked_run(registry, reset_at=1100.0, clock=clock)
        recovery = ParkedRunRecovery(registry, spy, clock=clock, interval=1.0)

        clock.advance(50)
        summary = await recovery.tick()
        assert summary["resumed"] == [], "reset window not reached yet"
        assert spy.resumed == []

        clock.advance(60)  # now 1110 >= 1100
        summary = await recovery.tick()
        assert summary["resumed"] == [run.run_id]
        assert spy.resumed == [run.run_id]
        assert run.parked is False
        assert run.park_status()["resumes_at"] == 0

        # resumed run no longer touched by recovery
        again = await recovery.tick()
        assert again["resumed"] == []

    async def test_park_cap_fails_honestly(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FUSION_PARKED_RUNS_PATH", str(tmp_path / "parked.json"))
        clock = FakeClock()
        registry = RunRegistry()
        spy = _ResumeSpy()
        run = _parked_run(registry, reset_at=clock.now + 10**9, clock=clock)
        # park() stamps real wall time; align with the fake clock so the
        # 24h cap arithmetic is deterministic
        run.parked_at = clock.now
        recovery = ParkedRunRecovery(registry, spy, clock=clock, interval=1.0)

        clock.advance(PARK_CAP_SECONDS + 1)
        summary = await recovery.tick()
        assert summary["failed"] == [run.run_id]
        assert spy.resumed == [], "capped run must not resume"
        assert run.done is True
        types = [f["type"] for f in run.buffer]
        assert "error" in types and types[-1] == "done"
        assert any("24h cap" in f.get("message", "") for f in run.buffer if f["type"] == "error")

    async def test_content_failure_never_parks(self):
        # Conflation guard: error frames without a usage_limit marker leave
        # the run failed/done — recovery must not touch it.
        registry = RunRegistry()
        spy = _ResumeSpy()
        run = registry.create("g1")
        run.publish({"type": "error", "message": "content failure"})
        run.done = True
        recovery = ParkedRunRecovery(registry, spy, clock=FakeClock(), interval=1.0)
        summary = await recovery.tick()
        assert summary == {"resumed": [], "failed": []}
        assert run.done is True and not run.parked

    async def test_store_persists_across_worker_instances(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FUSION_PARKED_RUNS_PATH", str(tmp_path / "parked.json"))
        store = ParkedRunStore(str(tmp_path / "parked.json"))
        store.upsert(
            {
                "run_id": "run-abc",
                "graph_id": "g1",
                "parked_until": 5000.0,
                "park_reason": "usage_limit",
            }
        )
        fresh = ParkedRunStore(str(tmp_path / "parked.json"))
        data = fresh.load()
        assert data["run-abc"]["parked_until"] == 5000.0
        fresh.remove("run-abc")
        assert ParkedRunStore(str(tmp_path / "parked.json")).load() == {}

    def test_park_record_roundtrip_json(self, tmp_path: Path):
        p = tmp_path / "parked.json"
        store = ParkedRunStore(str(p))
        store.upsert({"run_id": "r1", "parked_until": 1.0})
        raw = json.loads(p.read_text())
        assert raw["r1"]["parked_until"] == 1.0
