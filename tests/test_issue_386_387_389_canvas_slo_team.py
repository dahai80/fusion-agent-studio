import json
import os
from pathlib import Path

import pytest

from agent_runtime.dispatchers.infra import InfraDispatcher
from agent_runtime.dispatchers.team import TeamDispatcher
from agent_runtime.durable_roster import DurableRoster, Mailbox
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.subagent import MAX_SUBAGENT_DEPTH, SubAgentRunner, SubAgentSpec
from agent_runtime.telemetry import TelemetryEngine

ROOT = Path(__file__).resolve().parents[1]


def _graph():
    graph = AgentGraph(id="g1", name="demo")
    for node_id in ("start", "a", "b", "c"):
        graph.add_node(node_id, NodeConfig(type="llm", label=node_id))
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("a", "c")
    graph.agent_id = "agent-1"
    graph.plan_mode = True
    graph.stop_on_tool_error = True
    return graph


def test_downstream_subgraph_keeps_reachable_nodes():
    sub = _graph().downstream_subgraph("a")
    assert sub is not None
    assert set(sub.nodes) == {"a", "b", "c"}
    assert sub.start_node_id == "a"
    assert sub.id == "g1"
    assert sub.agent_id == "agent-1"
    assert sub.plan_mode is True
    assert sub.stop_on_tool_error is True
    pairs = {(e.source_id, e.target_id) for e in sub.edges}
    assert pairs == {("a", "b"), ("b", "c"), ("a", "c")}


def test_downstream_subgraph_unknown_node():
    assert _graph().downstream_subgraph("ghost") is None


def test_workflows_page_has_no_json_topology_editor():
    source = (ROOT / "web" / "src" / "pages" / "WorkflowsPage.tsx").read_text()
    assert "JsonEditor" not in source
    assert "openCanvas" in source
    assert "#/canvas" in source


def test_canvas_page_has_debug_toolkit():
    source = (ROOT / "web" / "src" / "pages" / "studio" / "WorkflowCanvasPage.tsx").read_text()
    for marker in (
        "pushHistory",
        "onConnectEnd",
        "from_node",
        "stop_after",
        "safety.approve",
        "breakpoint",
        "pinned",
    ):
        assert marker in source


def test_sse_route_accepts_downstream_params():
    source = (ROOT / "agent_runtime" / "api_server.py").read_text()
    assert "from_node: str = \"\"" in source
    assert "stop_after: str = \"\"" in source
    assert "downstream_subgraph(from_node)" in source


def test_export_slo_writes_jsonl(tmp_path):
    engine = TelemetryEngine()
    target = tmp_path / "slo.jsonl"
    first = engine.export_slo(str(target))
    second = engine.export_slo(str(target))
    assert first["path"] == str(target)
    assert second["record"]["schema"] == 1
    lines = target.read_text().strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["schema"] == 1
    assert "compactions_total" in rows[0]
    assert rows[0]["ts"] <= rows[1]["ts"]


def test_export_slo_env_path(tmp_path, monkeypatch):
    target = tmp_path / "env-slo.jsonl"
    monkeypatch.setenv("FUSION_SLO_BASELINE_PATH", str(target))
    engine = TelemetryEngine()
    out = engine.export_slo()
    assert out["path"] == str(target)
    assert target.is_file()


class _TelemetryDaemon:
    def __init__(self, engine):
        self.engine = engine

    def _get_telemetry_engine(self):
        return self.engine


async def test_session_slo_export_rpc(tmp_path):
    engine = TelemetryEngine()
    dispatcher = InfraDispatcher(_TelemetryDaemon(engine))
    out = await dispatcher.get_handlers()["telemetry.session_slo_export"](
        {"path": str(tmp_path / "rpc.jsonl")}
    )
    assert out["record"]["schema"] == 1
    assert os.path.isfile(out["path"])


class _ParentRuntime:
    def __init__(self, depth=0):
        self._sub_graph_depth = depth
        self.llm_gateway = None
        self.tools = None
        self.safety_gateway = None


async def test_subagent_depth_guard():
    runner = SubAgentRunner(_ParentRuntime(depth=MAX_SUBAGENT_DEPTH))
    report = await runner.run(SubAgentSpec(name="nested", task="x"))
    assert report.ok is False
    assert "MAX_SUBAGENT_DEPTH" in report.error


@pytest.fixture()
def team_db(tmp_path, monkeypatch):
    db = tmp_path / "team.db"
    monkeypatch.setenv("FUSION_TEAM_DB", str(db))
    return db


def test_durable_roster_roundtrip(team_db):
    roster = DurableRoster()
    roster.register("a1", name="Alpha", team="ops", capabilities=["read", "grep"])
    roster.register("a2", name="Beta", team="ops")
    roster.register("a3", name="Gamma", team="other")
    updated = roster.register("a1", name="Alpha2", team="ops", status="busy")
    assert updated["name"] == "Alpha2"
    assert updated["status"] == "busy"
    assert [r["agent_id"] for r in roster.list("ops")] == ["a1", "a2"]
    assert roster.set_status("a2", "idle") is True
    assert roster.get("a2")["status"] == "idle"
    assert roster.remove("a3") is True
    assert roster.get("a3") is None


def test_mailbox_send_inbox_ack(team_db):
    roster = DurableRoster()
    roster.register("a1", team="ops")
    roster.register("a2", team="ops")
    box = Mailbox()
    sent = box.send("a1", "a2", "hello", kind="task", team="ops")
    assert sent["message_id"]
    inbox = box.inbox("a2")
    assert [m["message_id"] for m in inbox] == [sent["message_id"]]
    assert box.ack("a2", sent["message_id"]) is True
    assert box.inbox("a2") == []
    assert box.inbox("a2", unread_only=False)[0]["acked_at"] > 0
    assert box.stats() == {"total": 1, "pending": 0}


def test_mailbox_broadcast(team_db):
    roster = DurableRoster()
    for agent_id in ("a1", "a2", "a3"):
        roster.register(agent_id, team="ops")
    box = Mailbox()
    msgs = box.broadcast("a1", "ops", "standup")
    assert {m["recipient"] for m in msgs} == {"a2", "a3"}
    assert len(box.inbox("a2")) == 1
    assert box.inbox("a1") == []


class _TeamDaemon:
    def __init__(self):
        self._hooks = None


async def test_team_dispatcher_roster_mailbox(team_db):
    dispatcher = TeamDispatcher(_TeamDaemon())
    handlers = dispatcher.get_handlers()
    reg = await handlers["team.roster_register"](
        {"agent_id": "a1", "name": "Alpha", "team": "ops"}
    )
    assert reg["agent_id"] == "a1"
    await handlers["team.roster_register"]({"agent_id": "a2", "team": "ops"})
    listed = await handlers["team.roster_list"]({"team": "ops"})
    assert len(listed["agents"]) == 2
    await handlers["team.roster_status"]({"agent_id": "a2", "status": "busy"})
    sent = await handlers["team.mailbox_send"](
        {"sender": "a1", "recipient": "a2", "body": "ping", "team": "ops"}
    )
    message_id = sent["message"]["message_id"]
    inbox = await handlers["team.mailbox_inbox"]({"recipient": "a2"})
    assert inbox["count"] == 1
    ack = await handlers["team.mailbox_ack"]({"recipient": "a2", "message_id": message_id})
    assert ack["ok"] is True
    stats = await handlers["team.mailbox_stats"]({})
    assert stats["pending"] == 0


def test_team_rpc_allowlist():
    from agent_runtime.rpc_bridge import WEB_RPC_ALLOWLIST

    for method in (
        "team.roster_register",
        "team.roster_list",
        "team.roster_status",
        "team.mailbox_send",
        "team.mailbox_broadcast",
        "team.mailbox_inbox",
        "team.mailbox_ack",
        "team.mailbox_stats",
        "telemetry.session_slo_export",
    ):
        assert method in WEB_RPC_ALLOWLIST
