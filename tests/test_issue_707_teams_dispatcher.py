# Issue #707 (T3): teams.* dispatcher over fusion-harness 0.12.0 TeamRuntime.
# Covers: register/add_task/board transitions, run_member via kernel with
# pre-flight rejections (brief fields, tool allowlist, budget cap, lifecycle),
# mailbox send/broadcast with disk-backed inboxes, namespace-scoped team
# memory, and bus event emission.
from __future__ import annotations

import json

import pytest

from agent_runtime.daemon_server import DaemonServer
from agent_runtime.kernel_wiring import build_kernel_orchestrator
from agent_runtime.rpc_auth import PRE_AUTH_ALLOWLIST
from tests.test_issue_341_subagent import _llm_response, _make_parent
from tools.base import BaseTool
from tools.registry import ToolRegistry


class _StubTool(BaseTool):
    def __init__(self, name: str):
        self.name = name
        self.description = f"stub {name}"
        self.parameters = {"tag": {"type": "string"}}

    async def execute(self, tag="", **kwargs) -> str:
        return f"{self.name}:{tag}"


def _stub_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for n in ("file_read", "file_write", "terminal_exec"):
        reg.register(_StubTool(n))
    return reg


async def _run(daemon, method, params=None):
    handler = daemon._get_handler(method)
    assert handler is not None, f"No handler for {method}"
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


def _definition():
    return {
        "name": "pair",
        "namespace_key": "team:pair",
        "lead_budget_tokens": 100000,
        "members": [
            {
                "name": "worker-a",
                "role": "does A",
                "skills": ["summarize"],
                "tool_allowlist": ["file_read", "file_write"],
                "budget_tokens": 40000,
            },
            {
                "name": "worker-b",
                "role": "does B",
                "tool_allowlist": ["file_read"],
                "budget_tokens": 30000,
            },
        ],
    }


def _brief(**overrides):
    b = {
        "objective": "produce the pair report",
        "output_format": "report",
        "tool_guidance": "allowlisted tools: file_read, file_write",
        "boundaries": "stay within the stated task scope",
        "token_budget": 5000,
        "concurrency": 1,
        "depth": 1,
    }
    b.update(overrides)
    return b


def _runtime(**kwargs):
    kwargs.setdefault("responses", [])
    kwargs.setdefault("tools", _stub_registry())
    rt, client = _make_parent(**kwargs)
    rt.kernel = {"orchestrator": build_kernel_orchestrator(rt)}
    return rt, client


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestRegisterAndBoard:
    async def test_register_activates_and_anchors_memory(self, daemon, _home):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        res = await _run(daemon, "teams.register", {"definition": _definition()})
        assert res["status"] == "ok"
        assert res["members"] == ["worker-a", "worker-b"]
        assert res["memory_anchor"] == {"tier": "team", "key": "team:pair"}
        assert (_home / "teams" / "pair").is_dir()
        listing = await _run(daemon, "teams.list")
        assert [t["name"] for t in listing["teams"]] == ["pair"]

    async def test_register_duplicate_rejected(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        first = await _run(daemon, "teams.register", {"definition": _definition()})
        assert first["status"] == "ok"
        again = await _run(daemon, "teams.register", {"definition": _definition()})
        assert again["status"] == "error"

    async def test_register_invalid_definition_typed_error(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        bad = _definition()
        bad["members"][1]["budget_tokens"] = 500000  # above lead budget
        res = await _run(daemon, "teams.register", {"definition": bad})
        assert res["status"] == "error"
        assert "exceeds lead budget" in res["message"]

    async def test_add_task_and_status_board(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        added = await _run(
            daemon, "teams.add_task",
            {"team": "pair", "title": "collect sources", "episodes": ["trace-1"]},
        )
        assert added["status"] == "ok"
        tid = added["task_id"]
        st = await _run(daemon, "teams.status", {"team": "pair"})
        assert st["lifecycle"] == "ACTIVE"
        assert st["tasks"][tid]["status"] == "todo"
        assert st["tasks"][tid]["episodes"] == ["trace-1"]
        tr = await _run(daemon, "teams.transition", {"team": "pair", "task_id": tid, "to": "in_progress"})
        assert tr["status"] == "ok"
        claim = await _run(daemon, "teams.claim_task", {"team": "pair", "task_id": tid, "member": "worker-a"})
        assert claim["won"] is True
        lost = await _run(daemon, "teams.claim_task", {"team": "pair", "task_id": tid, "member": "worker-b"})
        assert lost["won"] is False

    async def test_illegal_transition_rejected(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
        res = await _run(daemon, "teams.transition", {"team": "pair", "task_id": added["task_id"], "to": "done"})
        assert res["status"] == "error"
        assert "illegal task transition" in res["message"]

    async def test_blocked_requires_blocked_by(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
        res = await _run(daemon, "teams.transition", {"team": "pair", "task_id": added["task_id"], "to": "blocked"})
        assert res["status"] == "error"
        ok = await _run(
            daemon, "teams.transition",
            {"team": "pair", "task_id": added["task_id"], "to": "blocked", "blocked_by": ["pair-t1"]},
        )
        assert ok["status"] == "ok"


class TestRunMember:
    async def _setup(self, daemon):
        rt, client = _runtime(responses=[_llm_response(content="member final report")])
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "do the work"})
        return rt, added["task_id"]

    async def test_run_member_executes_via_kernel(self, daemon, tmp_path, _home):
        rt, tid = await self._setup(daemon)
        kernel_runner = rt.kernel["orchestrator"]["runner"]
        res = await _run(
            daemon, "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": _brief()},
        )
        assert res["status"] == "ok"
        assert res["result"]["via"] == "kernel-team-runtime"
        assert res["result"]["status"] == "completed"
        assert res["result"]["summary"] == "member final report"
        assert kernel_runner._counter == 1, "member run must flow through the kernel runner"
        st = await _run(daemon, "teams.status", {"team": "pair"})
        results = st["tasks"][tid]["results"]
        assert results and results[0]["member"] == "worker-a"
        # Studio SubAgentRunner delivery hook (#705) writes the worker artifact.
        assert (_home / "worker_artifacts").is_dir()

    async def test_incomplete_brief_rejected_before_worker(self, daemon):
        rt, tid = await self._setup(daemon)
        before = rt.kernel["orchestrator"]["runner"]._counter
        res = await _run(
            daemon, "teams.run_member",
            {"team": "pair", "member": "worker-a", "task_id": tid, "brief": {"objective": "half"}},
        )
        assert res["status"] == "error"
        assert "missing fields" in res["message"]
        assert rt.kernel["orchestrator"]["runner"]._counter == before

    async def test_tool_outside_allowlist_rejected(self, daemon):
        rt, tid = await self._setup(daemon)
        before = rt.kernel["orchestrator"]["runner"]._counter
        res = await _run(
            daemon, "teams.run_member",
            {"team": "pair", "member": "worker-b", "task_id": tid, "brief": _brief(),
             "tools": ["file_read", "terminal_exec"]},
        )
        assert res["status"] == "error"
        assert "outside member allowlist" in res["message"]
        assert rt.kernel["orchestrator"]["runner"]._counter == before

    async def test_budget_above_member_cap_rejected(self, daemon):
        rt, tid = await self._setup(daemon)
        before = rt.kernel["orchestrator"]["runner"]._counter
        res = await _run(
            daemon, "teams.run_member",
            {"team": "pair", "member": "worker-b", "task_id": tid,
             "brief": _brief(token_budget=35000)},
        )
        assert res["status"] == "error"
        assert "exceeds member cap" in res["message"]
        assert rt.kernel["orchestrator"]["runner"]._counter == before

    async def test_unknown_member_rejected(self, daemon):
        rt, tid = await self._setup(daemon)
        res = await _run(
            daemon, "teams.run_member",
            {"team": "pair", "member": "ghost", "task_id": tid, "brief": _brief()},
        )
        assert res["status"] == "error"
        assert "unknown member" in res["message"]


class TestLifecycle:
    async def _active(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        return rt

    async def test_operations_rejected_when_paused(self, daemon):
        await self._active(daemon)
        paused = await _run(daemon, "teams.pause", {"team": "pair"})
        assert paused["lifecycle"] == "PAUSED"
        res = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
        assert res["status"] == "error"
        assert "not ACTIVE" in res["message"]
        resumed = await _run(daemon, "teams.resume", {"team": "pair"})
        assert resumed["lifecycle"] == "ACTIVE"
        ok = await _run(daemon, "teams.add_task", {"team": "pair", "title": "t"})
        assert ok["status"] == "ok"

    async def test_disband_cleans_inboxes_and_registry(self, daemon, _home):
        await self._active(daemon)
        inbox_file = _home / "teams" / "pair" / "inboxes" / "worker-b.json"
        await _run(daemon, "teams.send", {"team": "pair", "from": "worker-a", "to": "worker-b", "body": "hi"})
        assert inbox_file.is_file()
        done = await _run(daemon, "teams.disband", {"team": "pair"})
        assert done["lifecycle"] == "DISBANDED"
        assert not inbox_file.exists()
        listing = await _run(daemon, "teams.list")
        assert listing["teams"] == []


class TestMailbox:
    async def _active(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        return rt

    async def test_send_writes_disk_and_inbox(self, daemon, _home):
        await self._active(daemon)
        res = await _run(
            daemon, "teams.send",
            {"team": "pair", "from": "worker-a", "to": "worker-b", "body": "handoff notes"},
        )
        assert res["status"] == "ok"
        inbox_file = _home / "teams" / "pair" / "inboxes" / "worker-b.json"
        assert inbox_file.is_file(), "mailbox delivery must be a disk write"
        on_disk = json.loads(inbox_file.read_text(encoding="utf-8"))
        assert on_disk[0]["body"] == "handoff notes"
        inbox = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-b"})
        assert inbox["messages"][0]["body"] == "handoff notes"
        empty = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-a"})
        assert empty["messages"] == []

    async def test_send_unknown_member_rejected(self, daemon):
        await self._active(daemon)
        res = await _run(daemon, "teams.send", {"team": "pair", "from": "worker-a", "to": "ghost", "body": "x"})
        assert res["status"] == "error"

    async def test_broadcast_reaches_all_but_sender(self, daemon):
        await self._active(daemon)
        res = await _run(daemon, "teams.broadcast", {"team": "pair", "from": "worker-a", "body": "all hands"})
        assert res["delivered"] == 1
        a = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-a"})
        b = await _run(daemon, "teams.inbox", {"team": "pair", "member": "worker-b"})
        assert a["messages"] == []
        assert b["messages"][0]["body"] == "all hands"

    async def test_message_type_validated(self, daemon):
        await self._active(daemon)
        res = await _run(daemon, "teams.send", {"team": "pair", "from": "worker-a", "to": "worker-b",
                                                "type": "gossip", "body": "x"})
        assert res["status"] == "error"
        assert "unknown message type" in res["message"]


class TestTeamMemory:
    async def test_namespace_scoped_isolation(self, daemon):
        rt, _client = _runtime()
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        other = _definition()
        other["name"] = "pair2"
        other["namespace_key"] = "team:pair2"
        await _run(daemon, "teams.register", {"definition": other})
        w = await _run(
            daemon, "teams.memory_write",
            {"team": "pair", "content": "the quarterly numbers live in notes/q4.md"},
        )
        assert w["status"] == "ok"
        mine = await _run(
            daemon, "teams.memory_read",
            {"team": "pair", "query": "quarterly numbers"},
        )
        assert any("q4.md" in e["content"] for e in mine["entries"])
        theirs = await _run(
            daemon, "teams.memory_read",
            {"team": "pair2", "query": "quarterly numbers"},
        )
        assert theirs["entries"] == [], "cross-team memory must be invisible"


class TestAuthLevel:
    def test_teams_not_pre_auth(self):
        assert not any(m.startswith("teams.") for m in PRE_AUTH_ALLOWLIST)

    def test_teams_handlers_registered(self):
        daemon = DaemonServer(socket_path="/tmp/x.sock", store_path="/tmp/x.db")
        methods = set(daemon._dispatch_map) if hasattr(daemon, "_dispatch_map") else set()
        for sd in daemon._sub_dispatchers:
            methods.update(sd.get_handlers())
        expected = {
            "teams.register", "teams.add_task", "teams.transition", "teams.claim_task",
            "teams.run_member", "teams.send", "teams.broadcast", "teams.inbox",
            "teams.status", "teams.pause", "teams.resume", "teams.disband",
            "teams.memory_write", "teams.memory_read", "teams.list",
        }
        assert expected <= methods


class TestLeadTools:
    async def _setup(self, daemon, **kwargs):
        rt, _client = _runtime(**kwargs)
        daemon._get_runtime = lambda: rt
        await _run(daemon, "teams.register", {"definition": _definition()})
        added = await _run(daemon, "teams.add_task", {"team": "pair", "title": "do the work"})
        return rt, added["task_id"]

    def _tool(self, rt, cls, *extra):
        return cls(lambda: rt.teams_registry, *extra)

    async def test_board_read(self, daemon):
        rt, tid = await self._setup(daemon)
        from tools.team_lead_tools import TeamBoardTool

        out = json.loads(await self._tool(rt, TeamBoardTool).execute(team="pair"))
        assert out["lifecycle"] == "ACTIVE"
        assert out["tasks"][0]["id"] == tid
        assert out["members"] == ["worker-a", "worker-b"]

    async def test_assign_add_and_transition(self, daemon):
        rt, _tid = await self._setup(daemon)
        from tools.team_lead_tools import TeamAssignTool

        tool = self._tool(rt, TeamAssignTool)
        added = json.loads(
            await tool.execute(team="pair", title="write summary", assignee="worker-b")
        )
        assert added["status"] == "ok" and added["assignee"] == "worker-b"
        moved = json.loads(
            await tool.execute(team="pair", task_id=added["task_id"], to="in_progress")
        )
        assert moved["status"] == "ok"
        fresh = json.loads(await tool.execute(team="pair", title="fresh"))
        illegal = await tool.execute(team="pair", task_id=fresh["task_id"], to="done")
        assert illegal.startswith("Error:")

    async def test_dispatch_runs_member_and_rejects_bad_brief(self, daemon):
        rt, tid = await self._setup(daemon, responses=[_llm_response(content="member final report")])
        from tools.team_lead_tools import TeamDispatchTool

        tool = TeamDispatchTool(lambda: rt.teams_registry, rt._team_agent_fn)
        ok = json.loads(await tool.execute(team="pair", member="worker-a", task_id=tid, brief=_brief()))
        assert ok["status"] == "ok" and ok["summary"] == "member final report"
        bad = await tool.execute(
            team="pair", member="worker-a", task_id=tid, brief={"objective": "half"}
        )
        assert bad.startswith("Error:") and "missing fields" in bad
        outside = await tool.execute(
            team="pair", member="worker-b", task_id=tid, brief=_brief(), tools=["terminal_exec"]
        )
        assert outside.startswith("Error:") and "outside member allowlist" in outside

    async def test_message_send_and_broadcast(self, daemon, _home):
        rt, _tid = await self._setup(daemon)
        from tools.team_lead_tools import TeamMessageTool

        tool = self._tool(rt, TeamMessageTool)
        ok = json.loads(await tool.execute(team="pair", **{"from": "worker-a"}, to="worker-b", body="n1"))
        assert ok["delivered"] == 1
        assert (_home / "teams" / "pair" / "inboxes" / "worker-b.json").is_file()
        bc = json.loads(await tool.execute(team="pair", **{"from": "worker-a"}, body="all"))
        assert bc["delivered"] == 1

    async def test_tools_not_registered_when_env_off(self, daemon, monkeypatch):
        monkeypatch.setenv("FUSION_TEAM_TOOLS", "0")
        rt, _client = _runtime()
        rt._register_team_lead_tools()
        assert not rt.tools.has("team_board")

    async def test_tools_registered_default(self, daemon):
        rt, _client = _runtime()
        rt._register_team_lead_tools()
        for n in ("team_board", "team_assign", "team_dispatch", "team_message"):
            assert rt.tools.has(n)

    def test_persona_bridge_skills_allowlist(self):
        from fusion_harness.team import TeamMember

        from agent_runtime.dispatchers.teams import member_persona_gate

        m = TeamMember(name="w", role="r", skills=["summarize"])
        gate = member_persona_gate(m)
        assert gate.skill_allowed("summarize") is True
        assert gate.skill_allowed("terminal_hardening") is False
        open_gate = member_persona_gate(TeamMember(name="w2", role="r"))
        assert open_gate.skill_allowed("anything") is True
