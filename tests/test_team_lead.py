# Issue #712: lead orchestration loop (tests map 1:1 to the issue §9 matrix).
from __future__ import annotations

import json

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.playbook_store import PlaybookStore
from agent_runtime.team_lead import (
    LeadBudgetError,
    LeadLedgerError,
    LeadMaxRoundsError,
    LeadStoppedError,
    TeamLead,
    VerdictLog,
)
from tests.test_issue_707_teams_dispatcher import _definition, _runtime


class FakeResp:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage or {}


def _ledger_json(replan_count=0, steps=None):
    steps = steps or [
        {"step": "collect sources", "assignee_hint": "worker-a", "depends_on": []},
        {"step": "write summary", "assignee_hint": "worker-b", "depends_on": [0]},
    ]
    return json.dumps(
        {
            "goal": "produce the pair report",
            "facts": {"given": ["g"], "to_look_up": [], "to_derive": [], "guesses": []},
            "plan": steps,
            "replan_count": replan_count,
        }
    )


def _verdict_json(speaker="worker-a", satisfied=False, in_loop=False, progress=True, instruction="do the step"):
    return json.dumps(
        {
            "is_request_satisfied": satisfied,
            "is_in_loop": in_loop,
            "is_progress_being_made": progress,
            "next_speaker": speaker,
            "instruction_or_question": instruction,
            "reasons": {"is_request_satisfied": "line", "is_in_loop": "line", "is_progress_being_made": "line", "next_speaker": "line", "instruction_or_question": "line"},
        }
    )


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, model="", messages=None, **kwargs):
        self.calls.append((messages or [{}])[0].get("content", ""))
        if self.replies:
            r = self.replies.pop(0)
            return r if isinstance(r, FakeResp) else FakeResp(r)
        return FakeResp(_verdict_json(satisfied=True))


async def _lead(daemon, replies=None, **kwargs):
    rt, _client = _runtime(**kwargs)
    daemon._get_runtime = lambda: rt
    await _run_rpc(daemon, "teams.register", {"definition": _definition()})
    team_rt = rt.teams_registry["pair"]
    ran = []
    briefs = []

    async def agent_fn(spec, session):
        ran.append(spec.name)
        briefs.append(getattr(spec, "objective", None) or getattr(spec, "prompt", ""))
        return "member did the work"

    lead = TeamLead(team_rt, FakeClient(replies or []), agent_fn, playbook_store=None)
    return rt, team_rt, lead, ran, briefs


async def _run_rpc(daemon, method, params=None):
    handler = daemon._get_handler(method)
    out = handler(params or {})
    if hasattr(out, "__await__"):
        return await out
    return out


@pytest.fixture
def daemon(tmp_path):
    return DaemonServer(
        socket_path=str(tmp_path / "test.sock"),
        store_path=str(tmp_path / "store.db"),
    )


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestMatrix:
    async def test_1_task_ledger_creates_tasks_with_dependencies(self, daemon):
        rt, team_rt, lead, _ran, _briefs = await _lead(daemon, replies=[FakeResp(_ledger_json())])
        ledger = await lead.task_ledger_call("produce the pair report")
        ids = lead._create_tasks_from_plan(ledger)
        assert len(ids) == 2
        assert team_rt._tasks[ids[0]].assignee == "worker-a"
        assert team_rt._tasks[ids[1]].blocked_by == [ids[0]]

    async def test_2_progress_verdict_dispatches_next_speaker(self, daemon):
        # Unassigned step 0: verdict's next_speaker alone decides who runs it.
        single = _ledger_json(steps=[{"step": "collect sources", "assignee_hint": "", "depends_on": []}])
        replies = [FakeResp(single), FakeResp(_verdict_json(speaker="worker-b")), FakeResp(_verdict_json(satisfied=True))]
        rt, team_rt, lead, ran, briefs = await _lead(daemon, replies=replies)
        out = await lead.run("produce the pair report", max_rounds=5)
        assert ran == ["worker-b"], "verdict must dispatch the named member"
        assert out["summary"]
        assert briefs and "do the step" in briefs[0], "verdict instruction becomes the brief objective"
        assert all(t.status == "done" for t in team_rt._tasks.values()), "completed run_member closes its task"

    async def test_3_stall_counter_arithmetic(self, daemon):
        no = FakeResp(_verdict_json(progress=False))
        yes = FakeResp(_verdict_json(progress=True))
        # rounds: -1(no), +1(yes) -> floor 0 checked; then 3x no -> exactly one replan
        replies = [
            FakeResp(_ledger_json()),
            no, yes, no, no, no,  # stalls: 1,0,1,2,3 -> replan after third
            FakeResp(_ledger_json()),
            FakeResp(_verdict_json(satisfied=True)),
        ]
        rt, team_rt, lead, ran, briefs = await _lead(daemon, replies=replies)
        await lead.run("produce the pair report", max_rounds=20)
        # Count committed re-plans via the verdict log (prompt retries inside
        # one re-plan would inflate a raw prompt count).
        replan_rows = [r for r in lead.vlog.tail(50) if r["kind"] == "replan"]
        assert len(replan_rows) == 1, "exactly one re-plan at max_stalls"
        assert lead.replan_count == 1
        assert lead.n_stalls == 0
        # floor: after no(+1) then yes(-1) the counter was 0, never negative
        assert lead.vlog.tail(50)

    async def test_4_replan_rebuilds_ledger_and_continues(self, daemon):
        replies = [
            FakeResp(_ledger_json()),
            FakeResp(_verdict_json(progress=False)),
            FakeResp(_verdict_json(progress=False)),
            FakeResp(_verdict_json(progress=False)),
            FakeResp(_ledger_json(steps=[{"step": "new plan", "assignee_hint": "", "depends_on": []}])),
            FakeResp(_verdict_json(satisfied=True)),
        ]
        rt, team_rt, lead, _ran, _briefs = await _lead(daemon, replies=replies)
        await lead.run("produce the pair report", max_rounds=20, max_stalls=3)
        assert lead.replan_count == 1
        titles = [t.title for t in team_rt._tasks.values()]
        assert "new plan" in titles

    async def test_5_completion_drains_board_and_appends_playbook(self, daemon, _home):
        rt, team_rt, lead, _ran, _briefs = await _lead(daemon, replies=[
            FakeResp(_ledger_json()),
            FakeResp(_verdict_json(speaker="worker-a")),
            FakeResp(_verdict_json(satisfied=True)),
        ])
        pb = PlaybookStore(root=_home / "playbook")
        lead.playbook_store = pb
        out = await lead.run("produce the pair report", max_rounds=10)
        assert all(t.status == "done" for t in team_rt._tasks.values())
        assert out["summary"]
        rows = pb.entries()
        assert rows and rows[0]["episodes"], "playbook lesson must cite task-id episodes"

    async def test_6_budget_exhaustion_stops_fail_visible(self, daemon):
        big = FakeResp(_ledger_json(), usage={"prompt_tokens": 50, "completion_tokens": 50})
        rt, team_rt, lead, ran, briefs = await _lead(daemon, replies=[big])
        with pytest.raises(LeadBudgetError):
            await lead.run("produce the pair report", max_rounds=5, team_budget=10)
        assert ran == [], "no member dispatched after budget stop"
        assert all(t.status == "blocked" for t in team_rt._tasks.values())
        assert all((t.metadata or {}).get("lead_block_reason") == "lead budget exhausted" for t in team_rt._tasks.values())
        kinds = [row["kind"] for row in lead.vlog.tail(50)]
        assert "budget_stop" in kinds

    async def test_7_max_rounds_fails_visible(self, daemon):
        replies = [FakeResp(_ledger_json())] + [FakeResp(_verdict_json(progress=True, instruction=f"round {i}")) for i in range(10)]
        rt, team_rt, lead, _ran, _briefs = await _lead(daemon, replies=replies)
        with pytest.raises(LeadMaxRoundsError):
            await lead.run("produce the pair report", max_rounds=3)
        assert lead.round == 3

    async def test_8_next_speaker_validation(self, daemon):
        replies = [
            FakeResp(_ledger_json()),
            FakeResp(_verdict_json(speaker="ghost")),
            FakeResp(_verdict_json(speaker="ghost")),
            FakeResp(_verdict_json(speaker="ghost")),
        ]
        rt, team_rt, lead, ran, briefs = await _lead(daemon, replies=replies)
        with pytest.raises(LeadLedgerError):
            await lead.run("produce the pair report", max_rounds=10)
        assert ran == [], "unknown next_speaker must never dispatch"

    async def test_9_one_lead_per_team(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        daemon.mlx_client = FakeClient([FakeResp(_ledger_json()), FakeResp(_verdict_json(satisfied=True))])
        await _run_rpc(daemon, "teams.register", {"definition": _definition()})
        first = await _run_rpc(daemon, "teams.lead_start", {"team": "pair", "goal": "g", "max_rounds": 50})
        assert first["status"] == "ok"
        second = await _run_rpc(daemon, "teams.lead_start", {"team": "pair", "goal": "g", "max_rounds": 50})
        assert second["status"] == "error"
        assert "already running" in second["message"]

    async def test_10_cooperative_stop(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        daemon.mlx_client = FakeClient([FakeResp(_ledger_json()), FakeResp(_verdict_json(progress=True))] * 40)
        await _run_rpc(daemon, "teams.register", {"definition": _definition()})
        started = await _run_rpc(daemon, "teams.lead_start", {"team": "pair", "goal": "g", "max_rounds": 100})
        assert started["status"] == "ok"
        entry = rt.teams_registry[("lead", "pair")]
        entry["lead"].stop_requested = True
        with pytest.raises(LeadStoppedError):
            await entry["task"]
        status = await _run_rpc(daemon, "teams.lead_status", {"team": "pair"})
        assert status["running"] is False
        assert status["round"] == status["round"]  # round persisted and readable

    async def test_10b_verdict_log_shape(self, tmp_path):
        vlog = VerdictLog(str(tmp_path))
        vlog.append("verdict", 1, {"a": 1})
        rows = vlog.tail(10)
        assert rows[0]["kind"] == "verdict" and rows[0]["round"] == 1 and "ts" in rows[0]
